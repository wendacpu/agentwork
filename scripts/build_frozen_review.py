#!/usr/bin/env python3
"""为一个冻结批次生成「多候选」评审映射。

新规则要求评审台展示本批全部待评候选（三槽：原题重述 / 主旨概括 / 核心钩子），
而不是只展示被选中的那一条。本脚本按 task 从 agent.db 只读提取候选，冻结
candidate_id + 标题哈希 + 槽位，写 review_mapping.json，供 BatchReviewStore 使用。

用法：
    python3 scripts/build_frozen_review.py --batch runtime/<批次目录> [--output ...]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sqlite3
from pathlib import Path

SLOT_ORDER = ("source_rewrite", "core_summary", "core_hook")
STATUS_ORDER = ("recommended", "ready_for_human_review", "needs_review", "rewrite", "reject", "unreviewed")


def title_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 机械检查已经判死的标题不需要人工判断：超字数、缺平台前缀、正文预算不足、空标题。
# 把它们送进评审台只会占用人眼，还得不出质量结论（人只能再判一次"超字数"）。
DEAD_MECHANICAL_CODES = {
    "G4_LENGTH_EXCEEDED",
    "G4_EMPTY_TITLE",
    "G4_PREFIX_MISSING",
    "G4_PREFIX_BUDGET_EXHAUSTED",
    "G4_ENDING_QUESTION_MARK",
}


# 两条候选相似度达到这个比例就视为同一句话，只送一条给人评。
SIMILARITY_THRESHOLD = 0.85

# 每个槽位最多送几条。上限要按槽位算：分歧后按复核侧重出时，同一槽位会有两组候选，
# 那是需要人对照的内容，不是重复。
MAX_PER_SLOT = 3


def mechanically_dead(candidate: dict) -> bool:
    mech = candidate.get("mechanical_checks") or {}
    codes = {str(e.get("code") or "") for e in (mech.get("errors") or []) if isinstance(e, dict)}
    return bool(codes & DEAD_MECHANICAL_CODES)


def dropped_mechanical(state: dict) -> list[dict]:
    """列出被机械检查判死、因此没有进入评审台的候选。

    这些候选不送人评是对的（人只能再判一次「超字数」），但**必须让评审者知道它们存在**：
    否则台上只剩一条候选时，看起来像模型只写了一条，而真实情况是模型写了三条、
    另外两条超字数被程序拦下。少一次披露就会让人把「生成能力不足」误判成「程序失败」，
    或者反过来把「程序拦得对」误读成「模型很克制」。
    """
    pool = (state.get("delivery_candidates") or []) + (state.get("candidates") or []) + (state.get("pending_review_candidates") or [])
    out, seen = [], set()
    for candidate in pool:
        cid = str(candidate.get("candidate_id") or "")
        if not cid or cid in seen or not mechanically_dead(candidate):
            continue
        seen.add(cid)
        mech = candidate.get("mechanical_checks") or {}
        codes = sorted({str(e.get("code") or "") for e in (mech.get("errors") or []) if isinstance(e, dict)} & DEAD_MECHANICAL_CODES)
        out.append({
            "candidate_id": cid,
            "slot_id": str(candidate.get("slot_id") or ""),
            "title": str(candidate.get("rendered_title") or candidate.get("headline") or ""),
            "title_length": len(str(candidate.get("rendered_title") or candidate.get("headline") or "")),
            "codes": codes,
        })
    return out


def pick_candidates(state: dict, include_rewrite: bool = False, include_failed: bool = False) -> list[dict]:
    pool = (state.get("delivery_candidates") or []) + (state.get("candidates") or []) + (state.get("pending_review_candidates") or [])
    route = state.get("route") or {}
    rec = route.get("recommended_candidate_id")
    eligible = set(route.get("eligible_candidate_ids") or [])
    status_by_id = {d.get("candidate_id"): d.get("status") for d in (state.get("candidate_decisions") or [])}

    chosen: dict[str, dict] = {}
    _titles: dict[str, str] = {}
    for candidate in reversed(pool):
        cid = str(candidate.get("candidate_id") or "")
        rendered = str(candidate.get("rendered_title") or candidate.get("headline") or "")
        if not cid or not rendered:
            continue
        # 只收本批真正可交付/待审的候选：推荐、已通过、待核；被拒与中间轮不进人工评审
        status = "recommended" if cid == rec else (status_by_id.get(cid) or "")
        deliverable = (include_rewrite or cid == rec or cid in eligible
                       or status in {"ready_for_human_review", "needs_review"})
        # R01 崩掉的任务 route 为空、没有任何人工可用的系统判定。
        # 这些候选是真实生成结果，但要单独标「系统评审未完成」，不能算待审或已交付。
        unreviewed = include_failed and not route and not status
        deliverable = deliverable or unreviewed
        if not deliverable:
            continue
        if mechanically_dead(candidate):
            continue
        _titles[cid] = rendered
        chosen.setdefault(cid, {
            "candidate_id": cid,
            "title_sha256": title_hash(rendered),
            "slot_id": str(candidate.get("slot_id") or ""),
            "status": "unreviewed" if unreviewed else (status or "ready_for_human_review"),
        })

    # 近重复只送一条：三条说的是同一句话时，多送两条不增加信息，只增加人眼负担。
    # 判定用归一化后的字符序列相似度，阈值保守（0.85），避免把真正不同的表达误删。
    # 未完成系统评审的任务例外：那批候选还没被任何规则筛过，替人决定藏哪条不合适。
    kept: list[dict] = []
    for item in sorted(chosen.values(), key=lambda x: (STATUS_ORDER.index(x["status"]) if x["status"] in STATUS_ORDER else 99)):
        title = _titles.get(item["candidate_id"], "")
        if item["status"] != "unreviewed" and any(difflib.SequenceMatcher(None, title, _titles.get(k["candidate_id"], "")).ratio() >= SIMILARITY_THRESHOLD for k in kept):
            continue
        kept.append(item)
    chosen = {item["candidate_id"]: item for item in kept}

    def sort_key(item: dict):
        slot = item["slot_id"] if item["slot_id"] in SLOT_ORDER else "zzz"
        status = STATUS_ORDER.index(item["status"]) if item["status"] in STATUS_ORDER else 99
        return (slot, status)

    # 上限按槽位算，不按总条数算。早先写死 [:3]：三槽各一条时恰好够，可一旦某个槽位
    # 有两组候选（分歧后按复核侧重出就是这样），排在前面的槽位会把后面的整槽挤掉——
    # 实测 v28 交付了 source_rewrite，台上却只剩 core_hook×2 + core_summary×1，
    # 人根本看不到那条真交付的标题。评审台必须能列出本批所有待评候选。
    out: list[dict] = []
    per_slot: dict[str, int] = {}
    for item in sorted(chosen.values(), key=sort_key):
        slot = item["slot_id"] if item["slot_id"] in SLOT_ORDER else "zzz"
        if per_slot.get(slot, 0) >= MAX_PER_SLOT:
            continue
        per_slot[slot] = per_slot.get(slot, 0) + 1
        out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--output", default="")
    ap.add_argument("--include-rewrite", action="store_true",
                    help="把被判「需重写」的候选也放进评审台（标注状态，不冒充交付）")
    ap.add_argument("--include-failed", action="store_true",
                    help="把 R01 未完成、无系统判定的候选也放进来，状态标「系统评审未完成」")
    args = ap.parse_args()

    batch = Path(args.batch)
    db = batch / "agent.db"
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    samples = {s["video_id"]: s for s in manifest.get("samples", [])}

    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    mapping, missing = [], []
    for index, sample in enumerate(manifest.get("samples", []), 1):
        video_id = sample["video_id"]
        row = conn.execute(
            "SELECT task_id, status, state_json FROM tasks WHERE video_id=? ORDER BY created_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        if row is None:
            missing.append(video_id)
            continue
        task_id, status, state_json = row
        state = json.loads(state_json or "{}")
        candidates = pick_candidates(state, args.include_rewrite, args.include_failed)
        entry = {
            "mechanical_dropped": dropped_mechanical(state),
            "blind_id": f"R{index:02d}",
            "index": index,
            "task_id": task_id,
            "video_id": video_id,
            "category": sample.get("category") or f"{sample.get('domain_1','')}-{sample.get('domain_2','')}".strip("-"),
            "source_title": sample.get("title") or (state.get("video") or {}).get("title", ""),
            "task_status": status,
            "route": state.get("route") or {},
            "candidates": candidates,
        }
        if len(candidates) == 1:
            entry["candidate_id"] = candidates[0]["candidate_id"]
            entry["title_sha256"] = candidates[0]["title_sha256"]
        mapping.append(entry)
    conn.close()

    out = Path(args.output) if args.output else batch / "review_mapping.json"
    out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    multi = sum(1 for m in mapping if len(m["candidates"]) > 1)
    none = sum(1 for m in mapping if not m["candidates"])
    print(f"写入 {out}")
    print(f"任务 {len(mapping)}｜多候选 {multi}｜无候选 {none}｜无任务 {len(missing)}")
    if missing:
        print("缺任务视频：" + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
