#!/usr/bin/env python3
"""把冻结批次的独立人评库导出成带身份的金标 CSV。

批次目录里应有 review_mapping.json（含 task_id / candidate_id / 完整标题 / 标题哈希）
与 human_feedback.db（decisions 表）。导出时逐条核对标题哈希，任何不一致直接拒绝，
避免"另一个任务的标签被算进当前批次"的历史污染。

用法：
    python3 scripts/export_batch_review.py --batch runtime/acceptance_xxx --out runtime/gold/batch.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

HEADER = [
    "video_id", "task_id", "candidate_id", "标题", "系统路由", "系统推荐",
    "人工判定（可用/勉强通过/不可用）", "人工最终标题", "命中的判据ID", "建议回退节点",
    "一句话理由", "判定人", "判定日期", "驳回原因",
]
VERDICT = {"approved": "可用", "marginal": "勉强通过", "rejected": "不可用"}


def title_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    batch = Path(args.batch)
    mapping = json.loads((batch / "review_mapping.json").read_text(encoding="utf-8"))
    by_task = {item["task_id"]: item for item in mapping}
    db_path = (batch / "human_feedback.db").resolve()
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    decisions = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM decisions ORDER BY task_id")]
    conn.close()

    rows, rejected = [], []
    for item in decisions:
        task_id = item.get("task_id")
        meta = by_task.get(task_id)
        if meta is None:
            rejected.append(f"{item.get('blind_id')}: 任务不在本批 mapping 中")
            continue
        if item.get("candidate_id") != meta.get("candidate_id"):
            rejected.append(f"{item.get('blind_id')}: 候选与 mapping 不一致")
            continue
        expected = meta.get("title_sha256")
        actual = title_hash(str(item.get("original_headline") or ""))
        if expected and expected != actual:
            rejected.append(f"{item.get('blind_id')}: 标题哈希与冻结快照不一致")
            continue
        verdict = VERDICT.get(str(item.get("decision")))
        if not verdict:
            continue
        route = item.get("original_system_route") or meta.get("route") or {}
        rows.append({
            "video_id": meta["video_id"],
            "task_id": task_id,
            "candidate_id": item["candidate_id"],
            "标题": item.get("original_headline") or "",
            "系统路由": str(route.get("action") or ""),
            "系统推荐": "是" if route.get("recommended_candidate_id") == item["candidate_id"] else "",
            "人工判定（可用/勉强通过/不可用）": verdict,
            "人工最终标题": item.get("final_headline") or "",
            "命中的判据ID": str(item.get("criteria_id") or ""),
            "建议回退节点": "",
            "一句话理由": str(item.get("reason") or "").replace("\n", " "),
            "判定人": str(item.get("reviewer") or "unnamed"),
            "判定日期": str(item.get("updated_at") or "")[:10],
            "驳回原因": str(item.get("reason_type") or ""),
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    print(f"导出 {len(rows)} 条判定 -> {out}")
    if rejected:
        print("拒绝导出（身份不一致）：")
        for line in rejected:
            print("  - " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
