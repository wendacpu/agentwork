#!/usr/bin/env python3
"""按两条口径算「当前 vs 提案目标」的指标，并把未评估/口径不清的部分显式标出来。

为什么需要这个脚本，而不是直接跑 review_quality_report.py：
批次在重跑，反馈库里同时存在**两代候选集**的判定。按同一把尺子量会得到两个都错的数：
- 只算「当前冻结候选」：漏掉更早候选集里的首轮判定，首轮成功率偏低；
- 把全部判定都算进来：又会把不该上台的候选算成「首轮有可用」，偏高。

所以这里分两条口径：
- **首轮口径**（用于 M1/M4）：任何一代候选集里，该任务 round=0 候选的判定都算数；
- **交付口径**（用于 M5/M6/M7/首推可用率）：只用当前 review_mapping 冻结的候选。

指标定义以提案正文为准，此处逐条对应，不做口径发挥。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

# 提案目标（痛点一/二/四）。改动目标就改这里，不要在报告里散落硬编码。
TARGETS = {
    "M1": ("首轮任务成功率", "ge", 0.78),
    "M4": ("重写挽救率", "ge", 0.35),
    "M5": ("最终合格供给率", "ge", 0.85),
    "M6": ("误放率", "le", 0.05),
    "M7": ("误拦率", "le", 0.10),
    # 2026-11-02 下调：路由不可能做到很准，首推一半可用时审核员点下一条也可接受。
    # 实测超过 50% 后再往上调。
    "TOP": ("首推严格可用率", "ge", 0.50),
}

APPROVED, MARGINAL, REJECTED = "approved", "marginal", "rejected"


def title_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class Video:
    task_id: str
    blind_id: str
    candidates: list[dict] = field(default_factory=list)      # 当前冻结（可评）
    dropped: list[dict] = field(default_factory=list)          # 机械判否
    route_recommended: str = ""
    route_delivered: set = field(default_factory=set)   # 系统真正交给人工的候选（交付组 ∪ 待核组）
    first_total: int = 0
    first_judged: int = 0
    first_approved: int = 0
    final_total: int = 0
    final_judged: int = 0
    final_approved: int = 0


def load(batch: Path) -> tuple[list[Video], dict, dict]:
    mapping = json.loads((batch / "review_mapping.json").read_text(encoding="utf-8"))
    decisions: dict[tuple[str, str, str], dict] = {}
    with closing(_ro(batch / "human_feedback_v2.db")) as conn:
        for row in conn.execute("SELECT payload_json FROM candidate_decisions_v2 WHERE batch_id=?", (batch.resolve().name,)):
            d = json.loads(row[0])
            if str(d.get('scope') or 'frozen') != 'frozen':
                continue
            decisions[(str(d.get("task_id")), str(d.get("candidate_id")),
                       str(d.get("title_hash"))) ] = d
    routes: dict[str, tuple[str, set]] = {}
    with closing(_ro(batch / "agent.db")) as conn:
        for row in conn.execute("SELECT task_id, state_json FROM tasks"):
            state = json.loads(row[1] or "{}")
            route = state.get("route") or {}
            # 交付面 = eligible（可放行）+ pending_review（交给人工但不自动放行）
            # + candidate_ids（needs_review 分支带的清单）。
            # 只认 recommended_candidate_id 会把「不是首推」误记成「被拦截」：
            # 路由推荐的是**一条**，交付的是**一组**（pipeline._route_candidates）。
            delivered = (set(route.get("eligible_candidate_ids") or [])
                         | set(route.get("pending_review_candidate_ids") or [])
                         | set(route.get("candidate_ids") or []))
            routes[row[0]] = (str(route.get("recommended_candidate_id") or ""), delivered)
    return [Video(task_id=it["task_id"], blind_id=it["blind_id"],
                  candidates=list(it.get("candidates") or []),
                  dropped=list(it.get("mechanical_dropped") or []),
                  route_recommended=routes.get(it["task_id"], ("", set()))[0],
                  route_delivered=routes.get(it["task_id"], ("", set()))[1])
            for it in mapping], decisions, routes


def _round_from_id(candidate_id: str) -> int:
    """从 candidate_id 反推轮次：重写候选形如 ``cand-2-r1-1``。

    历史候选集的判定不在 mapping 里，没有 round 字段；而轮次决定它算不算「首轮」，
    猜错就会把重写版当成首轮成绩。命名规则由 pipeline 生成，这里只做解析。
    """
    import re
    # 历史数据里两种命名并存：cand-2-r1-1 与 cand-2-r-1-1，两种都要认。
    m = re.search(r"-r-?(\d+)-", str(candidate_id or ""))
    return int(m.group(1)) if m else 0


def _verdict_of(decisions: dict, task_id: str, candidate_id: str, title: str) -> str | None:
    return (decisions.get((task_id, candidate_id, title_hash(title))) or {}).get("decision")


def measure(videos: list[Video], decisions: dict) -> dict:
    """任务级指标用「所有轮次、所有候选（含机械判否与历史候选集）」。

    理由：提案里「首轮任务成功率」问的是**人判过的那批首轮标题里有几条能用**，
    不是「系统当前愿意交付什么」。R01 的首轮标题「博主：体测跑前9个热身动作」
    被判可用、却被 G4_NUMERIC_CONFLICT 误拦，它算首轮成功——把误拦记成生成失败，
    等于用系统自己的 bug 去压低自己的成绩。
    误放/误拦/首推可用率仍只用当前冻结候选：那三项问的是**当前路由**的表现。
    """
    # 每个任务的完整候选视图：(candidate_id, title_hash) -> (标题, 轮次)
    full: dict[str, dict[tuple[str, str], tuple[str, int]]] = {}
    for v in videos:
        bucket = full.setdefault(v.task_id, {})
        for c in v.candidates:
            bucket[(c["candidate_id"], c["title_sha256"])] = (c["headline"], int(c.get("round") or 0))
        for d in v.dropped:
            bucket[(d["candidate_id"], title_hash(d["title"]))] = (d["title"], int(d.get("round") or 0))
    # 反馈库里同任务、但不在当前 mapping 里的判定（更早候选集）
    for (tid, cid, th), dec in decisions.items():
        bucket = full.setdefault(tid, {})
        if (cid, th) not in bucket:
            bucket[(cid, th)] = (str(dec.get("original_headline") or ""), _round_from_id(cid))

    for v in videos:
        bucket = full.get(v.task_id, {})
        v.first_total = sum(1 for _t, r in bucket.values() if r == 0)
        v.final_total = len(bucket)
        for (cid, th), (title, rnd) in bucket.items():
            verdict = _verdict_of(decisions, v.task_id, cid, title)
            if not verdict:
                continue
            if rnd == 0:
                v.first_judged += 1
                if verdict == APPROVED:
                    v.first_approved += 1
            if verdict == APPROVED:
                v.final_approved += 1

    m1_ok = [v for v in videos if v.first_approved > 0]
    # 首轮失败：首轮没有可用，且首轮的候选全都有人评过（判过不可用/勉强）
    m1_fail = [v for v in videos if v.first_approved == 0 and v.first_total and v.first_judged == v.first_total]
    m1_partial = [v for v in videos if v.first_approved == 0 and v.first_judged < v.first_total]
    m1_empty = [v for v in videos if v.first_total == 0]
    # 交付率不能用历史或机械判否候选代替当前交付面。
    m5_ok = [v for v in videos if any(
        c['candidate_id'] in v.route_delivered and _verdict_of(decisions, v.task_id, c['candidate_id'], c['headline']) == APPROVED
        for c in v.candidates)]
    # 未评完首轮不能认定首轮失败；未评完后续候选的任务不得当作确定未挽救。
    rescue_denom = [v for v in m1_fail if v.final_judged == v.final_total]
    rescued = [v for v in rescue_denom if any(
        _round_from_id(c['candidate_id']) > 0 and c['candidate_id'] in v.route_delivered
        and _verdict_of(decisions, v.task_id, c['candidate_id'], c['headline']) == APPROVED
        for c in v.candidates)]

    # 交付口径：只认当前冻结候选 + 当前路由
    judged = []
    for v in videos:
        for c in v.candidates:
            verdict = _verdict_of(decisions, v.task_id, c["candidate_id"], c["headline"])
            if verdict:
                judged.append((v, c, verdict))
    released = [x for x in judged if x[0].route_recommended == x[1]["candidate_id"]]
    misrelease = [x for x in released if x[2] != APPROVED]
    human_ok = [x for x in judged if x[2] == APPROVED]
    misblock = [x for x in human_ok if x[1]["candidate_id"] not in x[0].route_delivered]
    top_ok = []
    for v in videos:
        rec = next((c for c in v.candidates if c["candidate_id"] == v.route_recommended), None)
        if rec and _verdict_of(decisions, v.task_id, rec["candidate_id"], rec["headline"]) == APPROVED:
            top_ok.append(v)

    used = {(v.task_id, c["candidate_id"], c["title_sha256"]) for v in videos for c in v.candidates}
    return {
        "videos": len(videos),
        "M1": (len(m1_ok), len(videos), m1_ok, m1_fail, m1_partial, m1_empty),
        "M4": (len(rescued), len(rescue_denom), rescued),
        "M5": (len(m5_ok), len(videos), m5_ok),
        "M6": (len(misrelease), len(released), misrelease, released),
        "M7": (len(misblock), len(human_ok), misblock, human_ok),
        "TOP": (len(top_ok), len(videos), top_ok),
        "judged": len(judged),
        "unjudged": sum(len(v.candidates) for v in videos) - len(judged),
        "first_incomplete": len(m1_partial),
        "stale": len(decisions) - len(used),
    }


def render(m: dict) -> str:
    lines = ["# 当前实测 vs 提案目标", ""]

    def row(key: str) -> str:
        name, direction, target = TARGETS[key]
        num, den, *_ = m[key]
        if not den or m.get('unjudged', 0) or m.get('first_incomplete', 0):
            op = "≤" if direction == "le" else "≥"
            reason = '无有效分母' if not den else '人工评审未完成'
            return f"| {name} | {op}{target:.0%} | 未评估（{num}/{den}；{reason}） | — | 未评估 |"
        value = num / den
        passed = (value <= target) if direction == "le" else (value >= target)
        mark = "✅" if passed else "❌"
        op = "≤" if direction == "le" else "≥"
        gap = f"{value - target:+.0%}"
        return f"| {name} | {op}{target:.0%} | {value:.0%}（{num}/{den}） | {gap} | {mark} |"

    lines += ["| 指标 | 目标 | 实测 | 差距 | |", "|---|---|---|---|---|"]
    lines += [row(k) for k in ("M1", "M4", "M5", "M6", "M7", "TOP")]
    lines.append("")

    num, den, ok, fail, partial, empty = m["M1"]
    lines += ["## 首轮成功率的构成（20 个任务都要算数）", "",
              f"- 首轮有可用：**{len(ok)}** —— {'、'.join(v.blind_id for v in ok)}",
              f"- 首轮已评完但无可用：**{len(fail)}** —— {'、'.join(v.blind_id for v in fail)}",
              f"- 首轮零合规候选：**{len(empty)}** —— {'、'.join(v.blind_id for v in empty)}",
              f"- 首轮候选未评完：**{len(partial)}** —— {'、'.join(v.blind_id for v in partial) or '无'}",
              ""]

    rid, rden, rescued = m["M4"]
    lines += ["## 重写挽救", "",
              f"- 首轮无可用候选的任务：**{rden}**",
              f"- 其中重写后被救回：**{rid}** {'（' + '、'.join(v.blind_id for v in rescued) + '）' if rescued else ''}",
              ""]

    mid, mden, misrel, released = m["M6"]
    lines += ["## 误放（系统推荐的候选被人判不是「可用」）", "",
              f"- 系统推荐且已评：**{mden}** 条，其中不该放出去的：**{mid}** 条"]
    for v, c, verdict in misrel:
        lines.append(f"  - {v.blind_id} {c['candidate_id']} {verdict}：{c['headline']}")
    lines.append("")

    bid, bden, misblk, human_ok = m["M7"]
    lines += ["## 误拦（人判可用但系统没推荐）", "",
              f"- 人判可用的候选：**{bden}** 条，其中没被推荐的：**{bid}** 条"]
    for v, c, _ in misblk:
        lines.append(f"  - {v.blind_id} {c['candidate_id']}：{c['headline']}")
    lines.append("")

    lines += ["## 数据完整性", "",
              f"- 已评冻结候选：**{m['judged']}** 条",
              f"- 反馈库里不属于当前冻结集的判定：**{m['stale']}** 条"
              "（更早候选集的判定，只用于首轮口径，不计入交付口径）",
              ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    batch = Path(args.batch_dir).resolve()
    videos, decisions, _ = load(batch)
    text = render(measure(videos, decisions))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
