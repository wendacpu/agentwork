#!/usr/bin/env python3
"""从「跑批结果」生成待标注的金标准 CSV 骨架（判定列留空）。

用法：
    python3 scripts/gold_skeleton.py --run runtime/current/eval-v1.json --output runtime/gold/eval-v1.csv
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

HEADER = [
    "video_id", "task_id（可留空，脚本按 video 找最新任务）", "candidate_id", "标题",
    "系统路由", "系统推荐", "人工判定（可用/勉强通过/不可用）", "人工最终标题",
    "命中的判据ID（多个用;分隔）", "建议回退节点（D08）", "一句话理由", "判定人", "判定日期", "驳回原因",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    rows = []
    # 基线结果的标题在审核异常或审核拒绝时也会保留（不丢原稿），
    # 但那不是可交付候选，不能混进金标准待标注表冒充「写出来了」。
    skipped_baseline = {"rejected": 0, "error": 0, "legacy_unknown": 0}
    for r in run.get("results", []):
        route = r.get("route") or {}
        recommended = str(route.get("recommended_candidate_id") or "")
        raw_cands = r.get("candidates") or []
        eligible = set(route.get("eligible_candidate_ids") or [])
        if raw_cands:
            # 只判「交付层」候选（eligible）；被重写/驳回的中间候选不进人工评审
            cands = [c for c in raw_cands if c.get("candidate_id") in eligible]
        elif r.get("title"):
            audit_status = r.get("audit_status")
            if audit_status is None and r.get("error"):
                skipped_baseline["error"] += 1
                continue
            if isinstance(audit_status, str) and audit_status in skipped_baseline:
                skipped_baseline[audit_status] += 1
                continue
            cands = [{"candidate_id": "baseline", "title": r.get("title")}]
        for c in cands:
            rows.append({
                "video_id": r["video_id"],
                "task_id（可留空，脚本按 video 找最新任务）": r.get("task_id") or "",
                "candidate_id": c.get("candidate_id") or "",
                "标题": c.get("title") or "",
                "系统路由": str(route.get("action") or ""),
                "系统推荐": "是" if recommended and c.get("candidate_id") == recommended else "",
                "人工判定（可用/勉强通过/不可用）": "",
                "人工最终标题": "",
                "命中的判据ID（多个用;分隔）": "",
                "建议回退节点（D08）": "",
                "一句话理由": "",
                "判定人": "",
                "判定日期": "",
                "驳回原因": "",
            })

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    print(f"已生成 {len(rows)} 行待标注 -> {path}")
    if any(skipped_baseline.values()):
        print(f"未纳入待标注的基线标题（非可交付）：{json.dumps(skipped_baseline, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
