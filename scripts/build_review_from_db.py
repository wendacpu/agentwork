#!/usr/bin/env python3
"""从 batch 目录的 agent.db 直接生成评审清单：每视频冻结首轮**全部**候选。

与 `build_review_mapping.py` 的分工：

- `build_review_mapping.py` 转换**已存在**的单候选清单（旧批次补评时用）；
- 本脚本从零生成（新跑一批时用），不依赖任何历史 mapping。

为什么默认冻结全部首轮候选而不是一条：上一轮验收的清单每视频只冻结一条
（`system_recommended` 或"最后一轮第一条"），于是「三条候选里至少一条可用」
这个核心指标算不出来——只在一条上算通过率，等于把多候选设计的价值丢掉不测。

首轮候选没被保留的任务（首轮全废、只剩重写版），退到最早可得轮次并标
`earliest_available`，不把重写版冒充首轮。完全没产出的任务保留为空清单，
计失败，不补造。

用法：

    python scripts/build_review_from_db.py --batch-dir runtime/acceptance_fixed_20261102
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.batch_review import _blocking_reasons, title_hash  # noqa: E402


# 重写候选由 pipeline 生成成 ``{parent}-r{round}-{index}``；历史数据里两种写法并存：
# ``cand-2-r1-1`` 与 ``cand-2-r-1-1``。两种都要认。
_REWRITE_ID = re.compile(r"-r-?(\d+)-")


def _round_of(candidate) -> int:
    """轮次一律以 candidate_id 为准，不信 generation_round。

    ``generation_round`` 由模型输出、程序未固定（pipeline.py:1068 是
    ``item.get("generation_round", rewrite_round)``）。实测模型会写 -1 或 100：
    - 写成 -1 时，候选被判成「轮次未知」后在有正常轮次的批次里**被静默丢弃**
      （R18 的 cand-3-r-1-2 机械检查干净、人判可用，就这样没上台）；
    - 写成 1 时，首轮候选被当成重写轮，首轮桶空 → 误判「零合规候选」
      （R15 三条首轮候选全被 G1_MISSING_LIMIT 判否，编成重写轮后整批消失）。

    candidate_id 是程序拼的，可靠：没有重写后缀就是首轮。
    """
    match = _REWRITE_ID.search(str(candidate.get("candidate_id") or ""))
    return int(match.group(1)) if match else 0


def earliest_compliant(state: dict) -> tuple[list[tuple[str, str, dict, int]], int | None, list[dict]]:
    """按轮次从早到晚，取第一批**通过机械检查**的候选。

    为什么按「合规」而不是「首轮」筛：硬约束没守住的标题不是可交付标题——它连机械
    检查都过不了，人评只能再判一次「违规」，不该占用评审额度。而首轮全部违规时若
    直接判零候选，又会把系统重写救回来的候选一起丢掉，可重写正是硬约束该起作用的地方。

    返回 ([(candidate_id, 标题, 原始候选)], 采用轮次, 被剔除的候选清单)。
    """
    pool = (
        (state.get("delivery_candidates") or [])
        + (state.get("candidates") or [])
        + (state.get("pending_review_candidates") or [])
    )
    if not pool:
        return [], None, []

    buckets: dict[int, list[dict]] = {}
    unknown: list[dict] = []
    for candidate in pool:
        round_no = _round_of(candidate)
        (unknown if round_no is None else buckets.setdefault(round_no, [])).append(candidate)
    if not buckets:  # 轮次全写坏时退回「按出现顺序」
        buckets, unknown = {0: unknown}, []

    dropped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    eligible_ids = {str(x) for x in ((state.get("route") or {}).get("eligible_candidate_ids") or [])}
    eligible_ids |= {str((state.get("route") or {}).get("recommended_candidate_id") or "")}
    picked: list[tuple[str, str, dict, int]] = []
    adopted: int | None = None
    for round_no in sorted(buckets):
        kept: list[tuple[str, str, dict, int]] = []
        for candidate in buckets[round_no]:
            cid = str(candidate.get("candidate_id") or "")
            title = str(candidate.get("rendered_title") or candidate.get("headline") or "")
            if not cid or not title:
                continue
            key = (cid, title_hash(title))
            if key in seen:
                continue
            seen.add(key)
            reasons = _blocking_reasons(candidate)
            # 只有 G1 级别（信源/限定待核）和「待核」的候选是可以给人看的：
            # 它们没被程序判否，只是机械层判不了。G4 硬约束（超长、引用错、数字冲突、
            # 结构坏）仍然不进人评，留在只读失败清单里。
            reviewable_failure = bool(reasons) and all(
                str(r.get("code", "")).startswith("G1") or str(r.get("code", "")) == "G1_LIMIT_PENDING"
                for r in reasons
            )
            if reasons and not reviewable_failure:
                dropped.append({
                    "candidate_id": cid,
                    "slot_id": str(candidate.get("slot_id") or ""),
                    "title": title,
                    "title_length": len(title),
                    "codes": [r["code"] for r in reasons],
                    "round": round_no,
                })
                continue
            # 首轮（或最早一批）以外的候选只收系统明确交付/可交付的那几条：
            # 中间压缩稿不是独立待评对象，铺上去只会多占评审注意力。
            if round_no > 0 and adopted is not None and cid not in eligible_ids:
                continue
            kept.append((cid, title, candidate, round_no))
        if not kept:
            continue
        if adopted is None:
            adopted = round_no
        picked.extend(kept)
    return picked, adopted, dropped


def read_states(db_path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            row[0]: {"status": row[1] or "", "state": json.loads(row[2] or "{}")}
            for row in conn.execute("SELECT task_id, status, state_json FROM tasks")
        }
    finally:
        conn.close()


def build(batch_dir: Path, dry_run: bool = False) -> dict:
    batch_dir = batch_dir.resolve()
    manifest_path = batch_dir / "manifest.json"
    db_path = batch_dir / "agent.db"
    if not manifest_path.is_file():
        raise SystemExit(f"缺少 manifest.json：{manifest_path}")
    if not db_path.is_file():
        raise SystemExit(f"缺少 agent.db：{db_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    states = read_states(db_path)
    mapping, basis_counts, total = [], {}, 0
    for sample in manifest["samples"]:
        task_id = sample["task_id"]
        row = states.get(task_id) or {}
        state = row.get("state") or {}
        task_status = row.get("status") or ""
        picked, base, dropped = earliest_compliant(state)
        route = state.get("route") or {}
        decisions = {str(d.get("candidate_id")): d for d in (state.get("candidate_decisions") or [])}
        candidates = []
        for cid, rendered, _raw, round_no in picked:
            decision = decisions.get(cid) or {}
            recommended = str(route.get("recommended_candidate_id") or "") == cid
            candidates.append({
                "candidate_id": cid,
                "title_sha256": title_hash(rendered),
                "headline": rendered,
                "status": "recommended" if recommended else str(decision.get("status") or "unknown"),
                # 显式轮次：mapping 原先只有 round_basis 记「采用了哪一轮」，
                # 单条候选没有轮次，指标计算只能靠 candidate_id 猜。
                "round": round_no,
            })

        if base is None:
            basis = "no_compliant_candidate"
        elif base == 0:
            basis = "first_round"
        else:
            basis = "rewritten"
        basis_counts[basis] = basis_counts.get(basis, 0) + 1
        total += len(candidates)
        head = candidates[0] if candidates else {}
        mapping.append({
            "task_id": task_id,
            "blind_id": f"R{sample['index']:02d}",
            "category": sample.get("category"),
            "video_id": sample.get("video_id"),
            "source_title": sample.get("source_title"),
            "input_quality": sample.get("input_quality"),
            "task_status": task_status,
            "route_action": route.get("action"),
            "stop_reason": state.get("stop_reason"),
            "candidates": candidates,
            "mechanical_dropped": dropped,
            "all_dropped": bool(dropped) and not candidates,
            "frozen_candidate_count": len(candidates),
            "all_candidate_count": len(
                (state.get("delivery_candidates") or [])
                + (state.get("candidates") or [])
                + (state.get("pending_review_candidates") or [])
            ),
            "round_basis": basis,
            "adopted_round": base,
            "candidate_id": head.get("candidate_id"),
            "title_sha256": head.get("title_sha256"),
            "headline": head.get("headline"),
        })

    report = {
        "batch_dir": str(batch_dir),
        "videos": len(mapping),
        "candidates": total,
        "basis": basis_counts,
    }
    if not dry_run:
        target = batch_dir / "review_mapping.json"
        if target.exists():
            raise SystemExit(f"清单已存在，拒绝覆盖：{target}")
        target.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        report["written"] = str(target)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(Path(args.batch_dir), dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
