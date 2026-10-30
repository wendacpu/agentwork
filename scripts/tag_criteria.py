#!/usr/bin/env python3
"""把"驳回理由"归类到 criteria.csv 的判据上。

评审台上的操作者只该做两件事：选驳回类型、写清原因。判据有 19 条、彼此边界接近，
让人现场选必然选错（已经发生过一次：把"信源无足轻重"选成 C04 断言夸大）。

用法：
    python scripts/tag_criteria.py --list                # 列出所有驳回理由 + 当前归到的判据
    python scripts/tag_criteria.py --list --pending      # 只看还没归类的
    python scripts/tag_criteria.py --apply mapping.json  # {"cand-1": "C19"} 写回 gold.csv 与任务库
    python scripts/tag_criteria.py --refs                # 打印全部判据的 ID/名称/Gate，供归类时对照

mapping.json 也可以带理由：{"cand-1": {"criteria_id": "C19", "why": "..."}}
只依赖标准库。
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system import goldsheet  # noqa: E402  （先定位仓库根，再导入包）
GOLD = ROOT / "templates" / "gold.csv"


def dbs() -> list[str]:
    return [d for d in sorted(glob.glob(str(ROOT / "runtime" / "**" / "agent.db"), recursive=True)) if "_legacy" not in d]


def collect_rejections() -> list[dict]:
    """扫全部任务库，取出带理由的驳回记录。"""
    out = []
    for db in dbs():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            task_rows = conn.execute("SELECT task_id, video_id, state_json FROM tasks").fetchall()
            video_of = {r["task_id"]: r["video_id"] for r in task_rows}
            heads: dict[tuple[str, str], str] = {}
            for r in task_rows:
                state = json.loads(r["state_json"] or "{}")
                for c in state.get("candidates") or []:
                    heads[(r["task_id"], str(c.get("candidate_id")))] = str(c.get("headline") or "")
            for r in conn.execute("SELECT task_id, payload_json FROM labels"):
                payload = json.loads(r["payload_json"] or "{}")
                if payload.get("kind") != "human_title_decision":
                    continue
                if payload.get("decision") != "rejected":
                    continue
                cid = str(payload.get("candidate_id"))
                out.append({
                    "video_id": video_of.get(r["task_id"], ""),
                    "task_id": r["task_id"],
                    "candidate_id": cid,
                    "headline": heads.get((r["task_id"], cid), ""),
                    "reason_type": payload.get("reason_type") or "",
                    "criteria_id": payload.get("criteria_id") or "",
                    "reason": (payload.get("reason") or "").strip(),
                })
        finally:
            conn.close()
    # 同一 candidate 多条时保留最后一条
    latest = {}
    for row in out:
        latest[(row["task_id"], row["candidate_id"])] = row
    return list(latest.values())


def gold_criteria() -> dict[tuple[str, str], str]:
    """gold.csv 里已有的判据，用于比对"库里"和"表里"是否一致。"""
    if not GOLD.exists():
        return {}
    out = {}
    with GOLD.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key_cand = next((k for k in row if k.startswith("candidate_id")), "candidate_id")
            key_task = next((k for k in row if k.startswith("task_id")), "task_id")
            key_crit = next((k for k in row if k.startswith("命中的判据ID")), "命中的判据ID")
            out[((row.get(key_task) or "").strip(), (row.get(key_cand) or "").strip())] = (row.get(key_crit) or "").strip()
    return out


def cmd_refs() -> int:
    for c in goldsheet.read_criteria():
        print(f"{c['id']:4s} {c['gate']:2s} {c['name']:10s} {c['verdict'][:20]:22s} {c['condition'][:60]}")
    return 0


def cmd_list(pending_only: bool) -> int:
    rows = collect_rejections()
    existing = gold_criteria()
    shown = 0
    for row in rows:
        if pending_only and row["criteria_id"]:
            continue
        in_gold = existing.get((row["task_id"], row["candidate_id"]), "")
        flag = "" if (row["criteria_id"] or "") == in_gold else f"  ⚠️ gold.csv 写的是 {in_gold or '空'}"
        print(f"\n[{row['candidate_id']}] {row['headline']}")
        print(f"  视频 {row['video_id']} · 任务 {row['task_id']} · 类型 {row['reason_type'] or '—'}")
        print(f"  当前判据：{row['criteria_id'] or '未归类'}{flag}")
        print(f"  驳回理由：{row['reason'] or '（空）'}")
        shown += 1
    print(f"\n共 {shown} 条" + (f"（全部驳回 {len(rows)} 条，未归类 {sum(1 for r in rows if not r['criteria_id'])} 条）" if pending_only else ""))
    return 0


def cmd_apply(mapping_path: str) -> int:
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    known = {c["id"] for c in goldsheet.read_criteria()}
    rows = collect_rejections()
    index = {(r["task_id"], r["candidate_id"]): r for r in rows}
    applied = []

    # 逐任务写回 labels 表，再同步 gold.csv（两处必须一致，否则报表和台账会对不上）
    for db in dbs():
        conn = sqlite3.connect(db)
        try:
            for r in conn.execute("SELECT id, task_id, payload_json FROM labels").fetchall():
                payload = json.loads(r[2] or "{}")
                if payload.get("kind") != "human_title_decision":
                    continue
                cid = str(payload.get("candidate_id"))
                entry = mapping.get(cid)
                if entry is None:
                    continue
                criteria_id = entry.get("criteria_id") if isinstance(entry, dict) else entry
                why = entry.get("why", "") if isinstance(entry, dict) else ""
                unknown = [x for x in str(criteria_id).split(";") if x and x not in known]
                if unknown:
                    raise SystemExit(f"未知判据 {unknown}（候选 {cid}）；用 --refs 看全部判据 ID")
                payload["criteria_id"] = criteria_id
                if why:
                    payload["criteria_why"] = why
                conn.execute("UPDATE labels SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), r[0]))
                conn.commit()
                rec = index.get((r[1], cid)) or {}
                if rec.get("video_id"):
                    goldsheet.record(
                        video_id=rec["video_id"], task_id=r[1], candidate_id=cid,
                        headline=rec.get("headline", ""), verdict="不可用", final_headline=None,
                        criteria_id=criteria_id, fallback_node="", reason=rec.get("reason", ""),
                        reviewer="tiffany",
                    )
                applied.append((cid, criteria_id))
        finally:
            conn.close()
    print(f"已归类 {len(applied)} 条：" + ", ".join(f"{c}→{k}" for c, k in applied))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pending", action="store_true", help="只列未归类的")
    ap.add_argument("--apply", metavar="MAPPING_JSON")
    ap.add_argument("--refs", action="store_true")
    args = ap.parse_args()
    if args.refs:
        return cmd_refs()
    if args.apply:
        return cmd_apply(args.apply)
    return cmd_list(args.pending)


if __name__ == "__main__":
    raise SystemExit(main())
