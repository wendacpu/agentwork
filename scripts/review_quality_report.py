#!/usr/bin/env python3
"""批次人工评审质量报告：一次算出首轮成功率、误拦、误放与状态区分度。

为什么需要它：上一轮验收走了「每视频只评一条」的抽样，「三条候选里至少一条可用」
这个核心指标一直是 null；改成评完首轮全部候选后，指标又只能靠临时脚本手算。
这个工具把那次手工分析固定下来，任何批次目录都能直接出结论，下一轮验收不必重做。

口径固定，不随调用变化：

1. 分母 = 批次清单里的全部视频。未产出计失败，不缩分母，避免"没标题的视频悄悄消失"。
2. 严格「可用」不含「勉强通过」。两档分开报——合并会让改判成"勉强通过"看起来像质量提升。
3. 误拦率与误放率必须同时报。只压一头，另一头一定会顶上来。

用法：

    python scripts/review_quality_report.py --batch-dir runtime/acceptance_firstround_20261102
    python scripts/review_quality_report.py --batch-dir runtime/xxx --out report.md
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.batch_review import BatchReviewStore  # noqa: E402

VERDICTS = ("approved", "marginal", "rejected")
VERDICT_LABEL = {"approved": "可用", "marginal": "勉强通过", "rejected": "不可用"}
STATUS_LABEL = {
    "recommended": "系统推荐（已交付）",
    "ready_for_human_review": "系统通过",
    "needs_review": "待核",
    "rewrite": "需重写",
    "reject": "拒绝",
    "unknown": "状态未知",
}

# 误拦归因：把系统给出的 reason 归到"人能不能修"这一层。
MISBLOCK_BUCKETS = collections.OrderedDict([
    ("评审输出结构失败", ("REVIEW_SCHEMA_INVALID",)),
    ("系统判通过但路由未推荐", ("ALL_GATES_PASS_AND_QUALITY_MEETS_MINIMUM",)),
    ("路由未产出结论", ("", "unknown")),
    ("内容层要求改写", ("REPAIRABLE_GATE_OR_QUALITY_ISSUE", "G4_REPAIRABLE_WITH_INVALID_REVIEW")),
    ("信号语义未验证", ("SIGNAL_SEMANTICS_UNVERIFIED",)),
])


def load(batch_dir: Path) -> tuple[BatchReviewStore, dict[str, dict]]:
    mapping = json.loads((batch_dir / "review_mapping.json").read_text(encoding="utf-8"))
    if any("arm" in candidate for item in mapping for candidate in item.get("candidates", [])):
        raise ValueError("对照盲评不可使用单臂质量报告：须按实验臂与真实路由分别统计")
    store = BatchReviewStore(batch_dir)
    source = batch_dir / "agent.db"
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        states = {row[0]: json.loads(row[1] or "{}") for row in conn.execute("SELECT task_id, state_json FROM tasks")}
    finally:
        conn.close()
    return store, states


def collect(store: BatchReviewStore, states: dict[str, dict]) -> list[dict]:
    """把每个视频的候选摊平成记录，带上人工判定与系统判定。"""
    basis = {}
    mapping_path = store.batch_dir / "review_mapping.json"
    if mapping_path.is_file():
        for item in json.loads(mapping_path.read_text(encoding="utf-8")):
            basis[item.get("task_id")] = item.get("round_basis")
    rows = []
    for task in store.list_tasks():
        state = states.get(task["task_id"]) or {}
        route = state.get("route") or {}
        # 交付面 = 路由真正交到人眼前的候选组，不只是首推那一条。
        # 只认 recommended 会把「进了待核组」误记成「被系统拦掉」。
        delivered = (set(route.get("eligible_candidate_ids") or [])
                     | set(route.get("pending_review_candidate_ids") or [])
                     | set(route.get("candidate_ids") or []))
        recommended_id = str(route.get("recommended_candidate_id") or "")
        decisions = {str(d.get("candidate_id")): d for d in (state.get("candidate_decisions") or [])}
        detail = store.get_task(task["task_id"])
        candidates = []
        for candidate in detail["state"]["candidates"]:
            human = (candidate.get("decision") or {})
            system = decisions.get(str(candidate["candidate_id"])) or {}
            cid = candidate["candidate_id"]
            candidates.append({
                "candidate_id": cid,
                "title": candidate["rendered_title"],
                "status": candidate["status"],
                "verdict": human.get("decision"),
                "legacy": bool(human.get("legacy")),
                "reason": human.get("reason") or "",
                "system_reason": system.get("reason") or "",
                "gates": {g: v.get("status") for g, v in (system.get("gates") or {}).items()},
                "mechanical_blocked": bool(candidate.get("blocking_reasons")),
                "recommended": cid == recommended_id or candidate["status"] == "recommended",
                "delivered": (cid in delivered) or cid == recommended_id,
            })
        rows.append({
            "task_id": task["task_id"],
            "blind_id": task["blind_id"],
            "category": task.get("category"),
            "round_basis": basis.get(task["task_id"]),
            "candidates": candidates,
        })
    return rows


def bucketize(reason: str) -> str:
    text = str(reason or "")
    if not text:
        return "路由未产出结论"
    for label, keys in MISBLOCK_BUCKETS.items():
        if any(text.startswith(k) or text == k for k in keys if k):
            return label
    return "其他"


def compute(rows: list[dict]) -> dict:
    videos = len(rows)
    with_candidates = [r for r in rows if r["candidates"]]
    candidates = [c for r in rows for c in r["candidates"]]
    judged = [c for c in candidates if c["verdict"] in VERDICTS]
    counts = collections.Counter(c["verdict"] for c in judged)

    approved = [c for c in judged if c["verdict"] == "approved"]
    rejected = [c for c in judged if c["verdict"] == "rejected"]
    # 老调用方（含单测）只给 status；缺字段时回落到「是否被标成 recommended」。
    def _is_recommended(c):
        return bool(c.get("recommended", c["status"] == "recommended"))

    def _is_delivered(c):
        return bool(c.get("delivered", c["status"] == "recommended"))

    recommended = [c for c in candidates if _is_recommended(c)]
    recommended_judged = [c for c in recommended if c["verdict"] in VERDICTS]

    m1 = sum(1 for r in with_candidates if any(c["verdict"] == "approved" for c in r["candidates"]))
    m1_loose = sum(1 for r in with_candidates if any(c["verdict"] in ("approved", "marginal") for c in r["candidates"]))

    # 误拦 = 人判可用却没被交到人眼前；进了待核组也算交付，不算拦掉。
    misblocked = [c for c in approved if not _is_delivered(c)]
    # 误放按严格口径：推荐出去但人判不是「可用」（勉强通过同样不能算放对）。
    # 分母只取已判的首推，未判的单独计数——拿未判当分母会把误放率压低。
    misreleased = [c for c in recommended_judged if c["verdict"] != "approved"]
    recommended_unjudged = [c for c in recommended if c["verdict"] not in VERDICTS]

    # 状态区分度：每个系统状态里，人判可用的比例。
    by_status = collections.defaultdict(lambda: collections.Counter())
    for c in judged:
        by_status[c["status"]][c["verdict"]] += 1

    # 轮次基础拆分
    by_basis = collections.defaultdict(lambda: {"videos": 0, "strict": 0, "loose": 0})
    for r in rows:
        key = r["round_basis"] or "unknown"
        cell = by_basis[key]
        cell["videos"] += 1
        if any(c["verdict"] == "approved" for c in r["candidates"]):
            cell["strict"] += 1
        if any(c["verdict"] in ("approved", "marginal") for c in r["candidates"]):
            cell["loose"] += 1

    return {
        "videos": videos,
        "videos_with_candidates": len(with_candidates),
        "videos_no_candidate": videos - len(with_candidates),
        "candidates": len(candidates),
        "judged": len(judged),
        "unjudged": len(candidates) - len(judged),
        "counts": counts,
        "m1": m1,
        "m1_loose": m1_loose,
        "approved": len(approved),
        "recommended": len(recommended),
        "recommended_judged": len(recommended_judged),
        "recommended_unjudged": len(recommended_unjudged),
        "delivered": sum(1 for c in approved if _is_delivered(c)),
        "misblocked": misblocked,
        "misreleased": misreleased,
        "by_status": by_status,
        "by_basis": by_basis,
        "mechanical_blocked": sum(1 for c in candidates if c["mechanical_blocked"]),
        "legacy_judged": sum(1 for c in judged if c["legacy"]),
    }


def pct(num: int, den: int) -> str:
    return f"{num / den:.0%}" if den else "—"


def render(rows: list[dict], m: dict, batch_name: str) -> str:
    lines = [f"# 评审质量报告：{batch_name}", ""]
    lines.append(f"- 视频：{m['videos']}（有候选 {m['videos_with_candidates']}，未产出 {m['videos_no_candidate']}）")
    lines.append(f"- 候选：{m['candidates']}（已判 {m['judged']}，未判 {m['unjudged']}；其中旧库复用 {m['legacy_judged']}）")
    lines.append(f"- 机械检查判否仍进台：{m['mechanical_blocked']}")
    lines.append("")

    lines += ["## 一、总体指标", "", "| 指标 | 数值 |", "|---|---|"]
    if not m["judged"]:
        # 一条都没评时，0% 会被读成「一条都不行」。这是没数据，不是失败。
        lines.append(f"| 首轮任务成功率（≥1 条可用） | **未评估**（{m['videos']} 个视频，{m['candidates']} 条候选全部未判） |")
        lines.append("| 含勉强通过的任务成功率 | 未评估 |")
    else:
        lines.append(f"| 首轮任务成功率（≥1 条可用） | **{pct(m['m1'], m['videos'])}**（{m['m1']}/{m['videos']}） |")
        lines.append(f"| 含勉强通过的任务成功率 | {pct(m['m1_loose'], m['videos'])}（{m['m1_loose']}/{m['videos']}） |")
    for verdict in VERDICTS:
        lines.append(f"| 候选级：{VERDICT_LABEL[verdict]} | {pct(m['counts'][verdict], m['judged'])}（{m['counts'][verdict]}/{m['judged']}） |")
    lines.append("")

    lines += ["## 二、误拦与误放（必须配对读）", "", "| 指标 | 数值 | 含义 |", "|---|---|---|"]
    lines.append(f"| 误拦率 M7 | **{pct(len(m['misblocked']), m['approved'])}**（{len(m['misblocked'])}/{m['approved']}） | 人判可用的候选里，系统没交到人眼前的 |")
    m6_den = m["recommended_judged"]
    if m["recommended_unjudged"] or not m6_den:
        lines.append(f"| 误放率 M6 | **未评估**（已判首推 {m6_den} 条，未判 {m['recommended_unjudged']} 条） | 首推未评完时不出率，避免拿未判当分母压低误放 |")
    else:
        lines.append(f"| 误放率 M6 | **{pct(len(m['misreleased']), m6_den)}**（{len(m['misreleased'])}/{m6_den}） | 已判的首推里，人判不是「可用」的（含勉强通过） |")
    if m["judged"]:
        lines.append(f"| 可交付率 | {pct(m['delivered'], m['videos'])}（{m['delivered']}/{m['videos']}） | 被交到人眼前且人判可用的，占视频数 |")
    else:
        lines.append("| 可交付率 | 未评估 | 一条都未评时不计算 |")
    lines.append("")

    lines += ["## 三、状态区分度", "", "| 系统状态 | 候选 | 可用 | 勉强 | 不可用 | 可用率 |", "|---|---|---|---|---|---|"]
    for status, counter in sorted(m["by_status"].items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counter.values())
        lines.append(f"| {STATUS_LABEL.get(status, status)} | {total} | {counter['approved']} | {counter['marginal']} | {counter['rejected']} | {pct(counter['approved'], total)} |")
    lines.append("")

    if m["by_basis"]:
        lines += ["## 四、按轮次基础拆分", "", "| 轮次基础 | 视频 | 严格 | 含勉强 |", "|---|---|---|---|"]
        for basis, cell in sorted(m["by_basis"].items(), key=lambda kv: -kv[1]["videos"]):
            if not m["judged"]:
                lines.append(f"| {basis} | {cell['videos']} | 未评估 | 未评估 |")
            else:
                lines.append(f"| {basis} | {cell['videos']} | {pct(cell['strict'], cell['videos'])} | {pct(cell['loose'], cell['videos'])} |")
        lines.append("")

    if m["misblocked"]:
        lines += ["## 五、误拦归因", "", "| 系统给出的原因 | 条数 |", "|---|---|"]
        buckets = collections.Counter(bucketize(c["system_reason"]) for c in m["misblocked"])
        for label, count in buckets.most_common():
            lines.append(f"| {label} | {count} |")
        lines.append("")
        lines.append("明细：")
        lines.append("")
        for c in m["misblocked"]:
            fails = [f"{g}:{s}" for g, s in c["gates"].items() if s not in ("pass", None)]
            lines.append(f"- {c['candidate_id']}（{STATUS_LABEL.get(c['status'], c['status'])}）：{c['title'][:40]} — `{c['system_reason'] or '（空）'}` {' '.join(fails)}")
        lines.append("")

    if m["misreleased"]:
        lines += ["## 六、误放归因", ""]
        for c in m["misreleased"]:
            lines.append(f"- {c['candidate_id']}：{c['title'][:40]}")
            lines.append(f"  - 系统：`{c['system_reason'] or '（空）'}` gates={c['gates']}")
            lines.append(f"  - 人工：{c['reason'][:70] or '（无理由）'}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    batch_dir = Path(args.batch_dir).resolve()
    store, states = load(batch_dir)
    rows = collect(store, states)
    report = render(rows, compute(rows), batch_dir.name)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
