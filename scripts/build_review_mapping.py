#!/usr/bin/env python3
"""把「每视频只冻结一条候选」的批次清单，重建为「首轮全部候选」的清单。

为什么需要它：评审台（`batch_review_page.py`）本来就支持逐条评三候选，但
`review_mapping.json` 若是旧的单候选格式，`BatchReviewStore._candidates()`
会走单候选回退分支，一个视频只显示一条。而「首轮候选里至少一条可用」这个
指标只在评完全部首轮候选时才算得出来——只在一条上算通过率，等于把多候选
设计的价值丢掉不测。

做两件事：

1. 每个任务取 `generation_round` 最小的一批候选（通常就是首轮 3 条）。
   首轮候选没被保留下来的任务，退到「可得的最早一轮」，并在 `round_basis`
   里标明，避免把重写版冒充首轮。
2. 源批次只读，产物写到新目录（`agent.db` 用符号链接），诊断存档不受影响。

用法：

    python scripts/build_review_mapping.py \
        --source-dir runtime/acceptance_deepseek_20261101_114430 \
        --out-dir runtime/acceptance_firstround_20261102
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.batch_review import title_hash  # noqa: E402


def earliest_round_candidates(state: dict) -> tuple[list[tuple[str, str, dict]], int | None]:
    """取 generation_round 最小的一批候选（去重后）。

    返回 ([(candidate_id, rendered_title, raw), ...], base_round)。
    没有任何候选时返回 ([], None)。
    """
    pool = (
        (state.get("delivery_candidates") or [])
        + (state.get("candidates") or [])
        + (state.get("pending_review_candidates") or [])
    )
    if not pool:
        return [], None
    rounds = [int(c.get("generation_round") or 0) for c in pool]
    base = min(rounds)
    picked: list[tuple[str, str, dict]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in pool:
        if int(candidate.get("generation_round") or 0) != base:
            continue
        cid = str(candidate.get("candidate_id") or "")
        rendered = str(candidate.get("rendered_title") or candidate.get("headline") or "")
        if not cid or not rendered:
            continue
        key = (cid, title_hash(rendered))
        if key in seen:
            continue
        seen.add(key)
        picked.append((cid, rendered, candidate))
    return picked, base


def build(source_dir: Path, out_dir: Path, dry_run: bool = False) -> dict:
    source_dir = source_dir.resolve()
    out_dir = out_dir.resolve()
    mapping_path = source_dir / "review_mapping.json"
    source_db = source_dir / "agent.db"
    if not mapping_path.is_file():
        raise SystemExit(f"源批次缺少 review_mapping.json：{mapping_path}")
    if not source_db.is_file():
        raise SystemExit(f"源批次缺少 agent.db：{source_db}")
    if out_dir == source_dir:
        raise SystemExit("输出目录不能等于源目录：原批次的诊断存档必须保持只读")

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        states = {
            row[0]: json.loads(row[1] or "{}")
            for row in conn.execute("SELECT task_id, state_json FROM tasks")
        }
    finally:
        conn.close()

    rebuilt, basis_counts, total = [], {}, 0
    for item in mapping:
        state = states.get(item.get("task_id")) or {}
        picked, base = earliest_round_candidates(state)
        route = state.get("route") or {}
        decisions = {
            str(d.get("candidate_id")): d
            for d in (state.get("candidate_decisions") or [])
        }
        candidates = []
        for cid, rendered, _raw in picked:
            decision = decisions.get(cid) or {}
            recommended = str(route.get("recommended_candidate_id") or "") == cid
            candidates.append({
                "candidate_id": cid,
                "title_sha256": title_hash(rendered),
                "headline": rendered,
                "status": "recommended" if recommended else str(decision.get("status") or "unknown"),
            })

        new_item = dict(item)
        new_item["candidates"] = candidates
        new_item["frozen_candidate_count"] = len(candidates)
        if base is None:
            new_item["round_basis"] = "no_candidate"
        elif base == 0:
            new_item["round_basis"] = "first_round"
        else:
            new_item["round_basis"] = "earliest_available"
        new_item["earliest_round"] = base
        if candidates:
            new_item["candidate_id"] = candidates[0]["candidate_id"]
            new_item["title_sha256"] = candidates[0]["title_sha256"]
            new_item["headline"] = candidates[0]["headline"]

        basis_counts[new_item["round_basis"]] = basis_counts.get(new_item["round_basis"], 0) + 1
        total += len(candidates)
        rebuilt.append(new_item)

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "review_mapping.json").write_text(
            json.dumps(rebuilt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        link = out_dir / "agent.db"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(source_db)
        (out_dir / "SOURCE.json").write_text(
            json.dumps({
                "rebuilt_from": str(source_dir),
                "source_mapping": str(mapping_path),
                "rule": "每任务取 generation_round 最小的一批候选；源批次只读",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "out_dir": str(out_dir),
        "videos": len(rebuilt),
        "candidates": total,
        "basis": basis_counts,
        "written": not dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = build(Path(args.source_dir), Path(args.out_dir), dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
