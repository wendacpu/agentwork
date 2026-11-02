#!/usr/bin/env python3
"""把基线臂与系统臂的候选合成一个盲评批次。

对照实验要成立，三条必须同时满足：
1. 两臂看的是同一批视频（同一份 manifest）；
2. 两边由同一个人判，且判的时候不知道哪条来自哪一臂；
3. 记分口径一致——视频级「该视频有没有一条可用标题」。

第 2 条最容易被破坏：基线臂只有一个裸模型直出的标题，系统臂有推荐/待核/重写等状态。
只要界面把系统状态露出来，盲评立刻失效。所以本脚本给两条候选统一 `status`，
并在批次说明里写明「状态在评完后公布」——挡住的是信息泄露，不是透明。

用法：
    python3 scripts/build_compare_review.py \
        --baseline runtime/compare_v2_baseline/eval-v2.json \
        --current-batch runtime/compare_v2_current \
        --manifest templates/eval_manifest_v2.json \
        --out runtime/compare_v2_review
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"


def title_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def load_baseline(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["video_id"]: str(r.get("title") or "") for r in data.get("results", []) if r.get("video_id")}


def _candidate_index(state: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for key in ("candidates", "delivery_candidates", "pending_review_candidates"):
        for candidate in state.get(key) or []:
            cid = str((candidate or {}).get("candidate_id") or "")
            if cid and cid not in index:
                index[cid] = candidate
    return index


def pick_delivery_candidate(state: dict) -> tuple[dict | None, str]:
    """按路由的真实意图挑一条候选，而不是「生成轮最大的那条」。

    优先级：推荐 → 待核 → 可交付 → 兜底。
    旧实现在合并池里按 (generation_round, candidate_id) 取最大，等于绕开路由与机械检查：
    结果 14 条 mechanical=fail 的候选进了盲评，而真正走到 handoff 的候选一条都没进来，
    这不是「系统漏放」，是取样脚本自己没有按交付面取。
    """
    route = state.get("route") or {}
    index = _candidate_index(state)
    recommended = str(route.get("recommended_candidate_id") or "")
    if recommended and recommended in index:
        return index[recommended], "recommended"
    for key, label in (("pending_review_candidate_ids", "pending_review"), ("eligible_candidate_ids", "eligible")):
        for cid in reversed([str(x) for x in (route.get(key) or [])]):
            if cid in index:
                return index[cid], label
    if index:
        latest = max(index.values(),
                     key=lambda c: (int(c.get("generation_round") or 0), str(c.get("candidate_id") or "")))
        return latest, "fallback"
    return None, "none"


def load_current(db_path: Path) -> dict[str, dict]:
    """每个视频取一条交付面候选，并把「怎么挑的」和机械检查状态一并带出来核对。"""
    if not db_path.exists():
        raise SystemExit(f"系统臂任务库不存在（还没跑完？）：{db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT task_id, video_id, status, state_json FROM tasks"):
        state = json.loads(row["state_json"] or "{}")
        route = state.get("route") or {}
        picked, selection = pick_delivery_candidate(state)
        checks = (picked or {}).get("mechanical_checks") or {}
        out[row["video_id"]] = {
            "task_id": row["task_id"], "status": row["status"], "route": route,
            "selection": selection,
            "title": str((picked or {}).get("rendered_title") or (picked or {}).get("headline") or ""),
            "candidate_id": str((picked or {}).get("candidate_id") or ""),
            "mechanical_status": str(checks.get("status") or "unknown"),
            "mechanical_errors": [str(e.get("code") or "") for e in (checks.get("errors") or [])],
        }
    conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="runtime/compare_v2_baseline/eval-v2.json")
    ap.add_argument("--current-batch", default="runtime/compare_v2_current")
    ap.add_argument("--manifest", default="templates/eval_manifest_v2.json")
    ap.add_argument("--out", default="runtime/compare_v2_review")
    ap.add_argument("--seed", type=int, default=20261102)
    args = ap.parse_args()

    baseline = load_baseline(Path(args.baseline))
    current = load_current(Path(args.current_batch) / "agent.db")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rows = json.loads(TABLE.read_text(encoding="utf-8"))
    titles = {r["videoID"]: r.get("title") for r in (rows if isinstance(rows, list) else rows.get("videos") or [])}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "agent.db"
    if db_path.exists():
        db_path.unlink()

    rng = random.Random(args.seed)
    mapping: list[dict] = []
    skipped: list[dict] = []
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT);"
    )

    for sample in manifest["samples"]:
        vid = sample["video_id"]
        system = current.get(vid) or {}
        arm_titles = []
        if baseline.get(vid):
            arm_titles.append(("baseline", baseline[vid]))
        if system.get("title"):
            arm_titles.append(("current", system["title"]))
        if len(arm_titles) < 2:
            skipped.append({
                "video_id": vid,
                "reason": "系统臂无产出标题" if not system.get("title") else "基线臂无产出标题",
                "arms": [a for a, _ in arm_titles],
                # 未产出也要留下可核对的系统身份：是 stop 掉的，还是候选机械检查没过，
                # 两者对交付率的含义完全不同。
                "system_task_status": system.get("status"),
                "system_route_action": (system.get("route") or {}).get("action"),
                "system_stop_reason": (system.get("route") or {}).get("reason"),
                "system_selection": system.get("selection"),
            })
            continue
        rng.shuffle(arm_titles)
        task_id = f"cmp-{vid}"
        candidates, frozen = [], []
        for i, (arm, title) in enumerate(arm_titles, 1):
            cid = f"cand-{i}"
            candidates.append({
                "candidate_id": cid, "rendered_title": title, "headline": title,
                "slot_id": "", "thesis_id": "", "prefix_labels": [],
                "generation_round": 0, "parent_candidate_id": None,
                "mechanical_checks": {}, "programmatic_g1_flags": [],
            })
            # 盲评期间两条候选必须同状态，否则界面一露状态盲评立刻失效；
            # 真实身份只写在 review_mapping.json 里，评完核对时用。
            frozen.append({"candidate_id": cid, "title_sha256": title_hash(title),
                           "headline": title, "status": "ready_for_human_review", "round": 0,
                           "arm": arm})
        state = {
            "video": {"video_id": vid, "title": titles.get(vid)},
            "candidates": candidates, "delivery_candidates": [], "pending_review_candidates": [],
            "candidate_decisions": [], "route": {}, "delivery_slots": {},
        }
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                     (task_id, vid, "pending_human", "{}", json.dumps(state, ensure_ascii=False)))
        mapping.append({
            "task_id": task_id, "blind_id": f"C{sample['index']:02d}", "category": sample["category"],
            "video_id": vid, "source_title": titles.get(vid),
            "input_quality": {},
            # 真实交付身份（评审台不读这些字段，只有本文件有）
            "task_status": system.get("status"), "route_action": (system.get("route") or {}).get("action"),
            "stop_reason": (system.get("route") or {}).get("reason"),
            "selection": system.get("selection"), "system_candidate_id": system.get("candidate_id"),
            "mechanical_status": system.get("mechanical_status"),
            "mechanical_errors": system.get("mechanical_errors") or [],
            "candidates": frozen,
        })
    conn.commit()
    conn.close()

    (out_dir / "review_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    selection_counts: dict[str, int] = {}
    for row in mapping:
        key = str(row.get("selection") or "unknown")
        selection_counts[key] = selection_counts.get(key, 0) + 1
    note = {
        "purpose": "基线臂 vs 系统臂 · 盲评",
        "scoring": "视频级：该视频是否至少有一条被判「可用」",
        "blind": "两条候选来自不同臂，顺序已随机；系统状态在评完后公布",
        "videos": len(mapping), "skipped": skipped,
        # 取样口径：进盲评的系统臂候选按交付面优先级挑（推荐→待核→可交付→兜底），
        # 每一类的条数都要能看到，否则「进盲评的其实全是兜底候选」不会被发现。
        "system_selection_counts": selection_counts,
        "system_status_counts": {
            str(row.get("task_status")): sum(1 for r in mapping if r.get("task_status") == row.get("task_status"))
            for row in mapping
        },
        "mechanical_status_counts": {
            str(row.get("mechanical_status")): sum(1 for r in mapping if r.get("mechanical_status") == row.get("mechanical_status"))
            for row in mapping
        },
    }
    (out_dir / "compare_note.json").write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"对照批次 {len(mapping)} 条视频，{len(mapping) * 2} 条候选 -> {out_dir}")
    print(f"系统臂取样口径：{json.dumps(selection_counts, ensure_ascii=False)}")
    if skipped:
        print(f"跳过 {len(skipped)} 条（某臂无产出）：", json.dumps(skipped, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
