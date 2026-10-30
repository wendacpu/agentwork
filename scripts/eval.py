#!/usr/bin/env python3
"""离线评测：读人工金标准，算四个数。

用法：
    python scripts/eval.py                      # 默认读 templates/gold.csv，扫 runtime 下所有 agent.db
    python scripts/eval.py --db runtime/agent.db
    python scripts/eval.py --gold templates/gold.csv --max-chars 20

只依赖标准库。没有填金标准时输出"有效样本 0 条"，这是预期行为。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 「可用」两档：正常通过 + 勉强通过（有瑕疵但可放行）。需改写算不可用。
USABLE_VERDICTS = {"可用", "勉强通过"}


def g1_criteria() -> set[str]:
    """G1 类判据从 criteria.csv 的「归属 Gate」列取，避免新增判据（如 C19）漏算。"""
    source = ROOT / "templates" / "criteria.csv"
    if not source.exists():
        return {"C01", "C02", "C03", "C04", "C05", "C06"}
    with source.open(encoding="utf-8-sig", newline="") as f:
        return {(r.get("判据ID") or "").strip() for r in csv.DictReader(f) if (r.get("归属 Gate") or "").strip().startswith("G1")}


G1_CRITERIA = g1_criteria()


def col(row: dict[str, str], prefix: str) -> str:
    """按列名前缀取值，容忍表头里的括号说明。"""
    for key, value in row.items():
        if key and str(key).startswith(prefix):
            return (value or "").strip()
    return ""


def load_gold(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if col(r, "人工判定")]


def index_tasks(dbs: list[str]) -> dict[str, list[dict]]:
    """video_id -> 该视频的全部任务（按时间倒序）。"""
    out: dict[str, list[dict]] = {}
    for db in dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT task_id, video_id, status, config_json, state_json, created_at FROM tasks"):
                state = json.loads(row["state_json"] or "{}")
                cfg = json.loads(row["config_json"] or "{}")
                out.setdefault(row["video_id"], []).append({
                    "task_id": row["task_id"], "status": row["status"],
                    "provider": cfg.get("model_provider") or "?",
                    "rule_version": cfg.get("prompt_pack_version") or cfg.get("rule_version") or "?",
                    "state": state, "db": db, "created_at": row["created_at"],
                })
            conn.close()
        except sqlite3.Error:
            continue
    for items in out.values():
        items.sort(key=lambda t: str(t["created_at"]), reverse=True)
    return out


def pick_task(candidates: list[dict], wanted_task_id: str) -> tuple[dict | None, str]:
    """返回 (任务, 原因)。原因 ""=匹配成功，not_found=指定了 task_id 但找不到，
    ambiguous=没指定 task_id 且该视频有多个带候选的任务版本。

    不猜：同一视频被不同版本代码跑过 10 几次时，"取最新"会把旧 prompt、mock 或旧
    rubric 的候选和人工判定配成一对，算出一个看着合法、实际错配的数。
    """
    if wanted_task_id:
        hit = next((t for t in candidates if t["task_id"] == wanted_task_id), None)
        return (hit, "" if hit else "not_found")
    with_candidates = [t for t in candidates if (t["state"].get("candidates") or [])]
    if len(with_candidates) == 1:
        return (with_candidates[0], "")
    if len(with_candidates) > 1:
        return (None, "ambiguous")
    return ((candidates or [None])[0], "")


def rewrite_stats(tasks: dict[str, list[dict]]) -> dict:
    """汇总 rewrite_outcomes：重写到底修掉了什么、又犯了什么。

    只统计「有 parent_issue_codes」的轮次——因为 Q 分低但没出 issue 的重写，
    根本没法判断修掉了什么，混进来会让挽救率虚高。
    """
    outcomes = [o for items in tasks.values() for t in items for o in (t["state"].get("rewrite_outcomes") or [])]
    comparable = [o for o in outcomes if o.get("parent_issue_codes")]
    fully = [o for o in comparable if not (o.get("repeated") or [])]
    partial = [o for o in comparable if (o.get("resolved") or []) and (o.get("repeated") or [])]
    none_fixed = [o for o in comparable if not (o.get("resolved") or [])]
    repeated_codes: dict[str, int] = {}
    for o in outcomes:
        for code in o.get("repeated") or []:
            repeated_codes[code] = repeated_codes.get(code, 0) + 1
    modes: dict[str, int] = {}
    for o in outcomes:
        key = str(o.get("mode") or "未知")
        modes[key] = modes.get(key, 0) + 1
    return {
        "total": len(outcomes), "comparable": len(comparable),
        "fully": len(fully), "partial": len(partial), "none_fixed": len(none_fixed),
        "uncomparable": len(outcomes) - len(comparable),
        "repeated_codes": sorted(repeated_codes.items(), key=lambda kv: -kv[1])[:3],
        "modes": modes,
    }


def review_gates(state: dict, candidate_id: str) -> dict[str, str]:
    for review in state.get("reviews") or []:
        if review.get("candidate_id") == candidate_id:
            return {k: str((v or {}).get("status", "")) for k, v in (review.get("gates") or {}).items()}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(ROOT / "templates" / "gold.csv"))
    ap.add_argument("--db", action="append", help="可多次；缺省扫 runtime/**/agent.db")
    ap.add_argument("--max-chars", type=int, default=20)
    ap.add_argument("--out", default=str(ROOT / "runtime" / "eval_report.md"))
    args = ap.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"找不到金标准文件：{gold_path}")
        return 1
    gold = load_gold(gold_path)
    if not gold:
        print("有效样本 0 条：gold.csv 里还没有填「人工判定」的行。先判 15 条再跑。")
        return 0

    dbs = args.db or sorted(glob.glob(str(ROOT / "runtime" / "**" / "agent.db"), recursive=True))
    tasks = index_tasks(dbs)

    rows, missing, ambiguous = [], [], []
    for g in gold:
        vid = (g.get("video_id") or "").strip()
        if not vid:
            continue
        task, why = pick_task(tasks.get(vid, []), col(g, "task_id"))
        if task is None:
            if why == "ambiguous":
                ambiguous.append((vid, tasks.get(vid, [])))
            else:
                missing.append(vid)
            continue
        state = task["state"]
        candidates = state.get("candidates") or []
        by_id = {str(c.get("candidate_id")): c for c in candidates}
        chosen = (g.get("candidate_id") or "").strip()
        verdict = col(g, "人工判定")
        criteria = [x.strip() for x in col(g, "命中的判据ID").split(";") if x.strip()]
        rec = (state.get("route") or {}).get("recommended_candidate_id")
        rec_titles = [c.get("headline", "") for c in candidates]
        rows.append({
            "video_id": vid, "task_id": task["task_id"], "status": task["status"],
            "provider": task.get("provider", "?"), "rule_version": task.get("rule_version", "?"),
            "candidate_id": chosen, "verdict": verdict, "criteria": criteria,
            "system_recommended": rec, "chosen_headline": (by_id.get(chosen) or {}).get("headline", ""),
            "g1_status": review_gates(state, chosen).get("G1", ""),
            "title_lengths": [len(str(t)) for t in rec_titles if t],
        })

    # 一次通过率：同一条视频至少 1 个候选被判「可用」
    by_video: dict[str, list[dict]] = {}
    for r in rows:
        by_video.setdefault(r["video_id"], []).append(r)
    # 严格通过只数「可用」；含灰度过率把「勉强通过」也算上。
    # 两档分开报：合并会让"把不可用改成勉强通过"看起来像质量提升。
    pass_videos = [v for v, rs in by_video.items() if any(r["verdict"] in USABLE_VERDICTS for r in rs)]
    pass_rate = len(pass_videos) / len(by_video) if by_video else 0.0
    loose_videos = [v for v, rs in by_video.items() if any(r["verdict"] in {"可用", "勉强通过"} for r in rs)]
    loose_rate = len(loose_videos) / len(by_video) if by_video else 0.0

    # 人工一致率：系统推荐的那条，是否被判可用
    judged = [r for r in rows if r["system_recommended"] and r["candidate_id"]]
    agree = [r for r in judged if r["candidate_id"] == r["system_recommended"] and r["verdict"] in USABLE_VERDICTS]
    agree_rate = len(agree) / len(judged) if judged else None

    # 长度合格率：被评估候选里 ≤ max_chars 的比例
    all_lens = [n for r in rows for n in r["title_lengths"]]
    len_rate = sum(1 for n in all_lens if n <= args.max_chars) / len(all_lens) if all_lens else None

    # G1 漏拦率：人工判为 G1 类错误、系统 G1 却是 pass
    g1_ids = g1_criteria()
    g1_cases = [r for r in rows if set(r["criteria"]) & g1_ids]
    g1_miss = [r for r in g1_cases if r["g1_status"] == "pass"]
    g1_rate = len(g1_miss) / len(g1_cases) if g1_cases else None

    rw = rewrite_stats(tasks)
    rescue = (rw["fully"] / rw["comparable"]) if rw["comparable"] else None

    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    lines = [
        "# 离线评测报告",
        "",
        f"- 金标准文件：`{gold_path}`",
        f"- 有效判定：{len(rows)} 条候选 / {len(by_video)} 条视频",
        f"- 长度上限：{args.max_chars} 字",
        "",
        "| 指标 | 数值 | 样本量 | 说明 |",
        "|---|---|---|---|",
        f"| 一次通过率（严格） | {pct(pass_rate)} | {len(by_video)} 条视频 | 同一视频至少 1 条候选被判「可用」 |",
        f"| 一次通过率（含勉强通过） | {pct(loose_rate)} | {len(by_video)} 条视频 | 「可用」或「勉强通过」都算 |",
        f"| 人工一致率 | {pct(agree_rate)} | {len(judged)} 条可比 | 系统推荐的那条与人工选择一致 |",
        f"| 长度合格率 | {pct(len_rate)} | {len(all_lens)} 条候选 | 候选标题 ≤ {args.max_chars} 字 |",
        f"| G1 漏拦率 | {pct(g1_rate)} | {len(g1_cases)} 条 G1 问题 | 人工判 G1 有问题但系统 G1=pass |",
        "",
    ]
    lines += [
        "## 重写有效性",
        "",
        "> 只统计本轮任务库里真实发生过的重写（`rewrite_outcomes`），与 gold.csv 无关。",
        "",
        "| 口径 | 数值 | 说明 |",
        "|---|---|---|",
        f"| 重写轮次 | {rw['total']} | 发生过的重写次数 |",
        f"| 可比轮次 | {rw['comparable']} | 有明确 issue 可对照的轮次 |",
        f"| 完全解决 | {rw['fully']}（{pct(rescue)}） | 上一轮的问题这轮没再出现 |",
        f"| 部分解决 | {rw['partial']} | 修掉一些、又犯了一些 |",
        f"| 无改善 | {rw['none_fixed']} | 上一轮的 issue 全部再现 |",
        f"| 不可比 | {rw['uncomparable']} | 因 Q 分低触发，没有 issue 可对照 |",
        "",
    ]
    if rw["modes"]:
        lines.append("重写模式分布：" + "，".join(f"{k} {v} 次" for k, v in rw["modes"].items()))
        lines.append("")
    if rw["repeated_codes"]:
        lines.append("反复出现的 issue（Top 3）：" + "，".join(f"`{c}` ×{n}" for c, n in rw["repeated_codes"]))
        lines.append("")
    if rw["comparable"] == 0:
        lines.append("⚠️ 还没有可比的重写轮次：需要一次「评审报了 issue → 重写 → 再评审」的完整记录。")
        lines.append("")

    if rows:
        lines += [
            "## 参与统计的样本",
            "",
            "| 视频 | 任务 | provider | 规则版本 | 候选 | 人工判定 | 系统推荐 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(f"| {r['video_id']} | `{r['task_id']}` | {r['provider']} | {r['rule_version']} | "
                         f"`{r['candidate_id'] or '（空）'}` | {r['verdict']} | {r['system_recommended'] or '—'} |")
        lines.append("")
    if ambiguous:
        lines += [
            "## ⚠️ 这些样本没算进去：同一视频有多个任务版本，gold 里没指定 task_id",
            "",
            "不指定就把不同 prompt / mock / 旧 rubric 的候选和你的判定配对，会算出看着合法、实际错配的数。",
            "从下面挑一条填进 gold.csv 的 `task_id` 列，再重跑。",
            "",
        ]
        for vid, items in ambiguous:
            with_c = [t for t in items if (t["state"].get("candidates") or [])]
            lines.append(f"**{vid}** — {len(with_c)} 个带候选的任务版本，最近的 5 个：")
            lines.append("")
            lines.append("| task_id | provider | 规则版本 | 状态 | 首条候选 |")
            lines.append("|---|---|---|---|---|")
            for t in with_c[:5]:
                head = ((t["state"].get("candidates") or [{}])[0].get("headline") or "")[:28]
                lines.append(f"| `{t['task_id']}` | {t.get('provider', '?')} | {t.get('rule_version', '?')} | {t['status']} | {head} |")
            lines.append("")
    if missing:
        lines.append(f"⚠️ 以下视频在本机任务库里找不到可用任务：{', '.join(missing)}")
    if not judged:
        lines.append("⚠️ 人工一致率无法计算：没有「系统有推荐且人工也判了」的样本。")
    if not g1_cases:
        lines.append("⚠️ G1 漏拦率无法计算：还没有任何候选命中 C01–C06。")
    lines.append("")
    lines.append("> 这些是**替代指标**：离线阶段拿不到 CTR/线索，人工一致率不等于线上效果。")

    report = "\n".join(lines)
    print(report)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
