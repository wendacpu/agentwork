#!/usr/bin/env python3
"""把评审台标记为「精彩」的标题导入共享案例库，作为正面示范。

为什么是独立脚本，而不是评审台里直接写案例库：
- 评审台只读原任务、只写本批隔离反馈库；跨批次的案例学习必须是显式动作，
  否则一次评审会悄悄改变后续所有批次的生成输入。
- 「精彩」是「可用」之上的附加标记，只有人工点过才成立，不能由系统推断。

这四个字段不是可选项，缺一个就取不到（已在临时库实测）：
- ``confirmed=1`` 且 ``rule_version`` 等于生成时的 ``prompt_pack_version``：
  MemoryStore.search 的 SQL 硬过滤，版本对不上一律 0 命中；
- ``scope`` 取「一级 二级 format」，要与检索请求有交集，否则被领域过滤挡掉；
- payload 里的 ``video_id``：MemoryStore.create_draft 不写这个字段，
  缺了它 search_for_video（同视频复用）永远是空。

用法：
    python3 scripts/import_good_cases.py --batch-dir runtime/<批次> --dry-run
    python3 scripts/import_good_cases.py --batch-dir runtime/<批次> --verify
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_system.config import RunConfig
from agent_system.memory import MemoryStore
from agent_system.types import CaseRecord

SOURCE = "human_highlight"


def _ro(path: Path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def read_feedback(fb_db: Path) -> list[dict]:
    with _ro(fb_db) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_decisions_v2'"
        ).fetchone():
            return []
        rows = conn.execute("SELECT payload_json FROM candidate_decisions_v2").fetchall()
    return [json.loads(row[0]) for row in rows]


def read_video(src_db: Path, task_id: str) -> dict:
    with _ro(src_db) as conn:
        row = conn.execute("SELECT state_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return {}
    try:
        state = json.loads(row[0])
    except json.JSONDecodeError:
        return {}
    return state.get("video") or {}


def video_scope(video: dict) -> str:
    """案例的领域串，必须与检索请求（视频的一级/二级/format）能取交集。"""
    domain = video.get("domain") or {}
    parts = [domain.get("一级"), domain.get("二级"), video.get("format")]
    return " ".join(str(p) for p in parts if p)


def build_cases(decisions: list[dict], src_db: Path, version: str):
    by_task: dict[str, list[dict]] = {}
    for d in decisions:
        by_task.setdefault(str(d.get("task_id") or ""), []).append(d)

    cases, skipped = [], []
    for d in decisions:
        if d.get("decision") != "approved" or not d.get("highlight"):
            continue
        task_id = str(d.get("task_id") or "")
        title = str(d.get("final_headline") or d.get("original_headline") or "").strip()
        if not title:
            skipped.append((task_id, "标题为空"))
            continue
        video = read_video(src_db, task_id)
        scope = video_scope(video)
        if not scope:
            # 没有 scope 的案例在任何一次检索里都会被跳过，导进去等于白导。
            skipped.append((task_id, "取不到视频领域，导入后必然检索不到"))
            continue
        peers = [x for x in by_task.get(task_id, []) if x.get("decision") == "rejected"]
        bad = str((peers[0] if peers else {}).get("original_headline") or "").strip()
        reason = str(d.get("reason") or "").strip()
        blind = str(d.get("blind_id") or "")
        case_id = f"human-good-{task_id}-{d.get('candidate_id')}"
        cases.append({
            "case_id": case_id,
            "case": CaseRecord(
                case_id=case_id,
                case_type="good",
                # 填「表达」而不是具体维度码：首次生成的检索 problem_types 就是
                # ["表达", "事实与信源"]，填它才能拿到 type_hit 加分。
                problem_type="表达",
                bad_example=bad,
                good_example=title,
                applicability=reason or f"人工判定精彩（{blind}）：同类题材可直接复用的正面示范",
                source_task_id=task_id,
                rule_version=version,
                confirmed=True,
                compatible_rule_versions=[version],
                case_status="confirmed",
                scope=scope,
                source_evidence=["人工判定精彩"],
            ),
            "video_id": str(d.get("video_id") or video.get("video_id") or ""),
            "video_title": str(video.get("title") or ""),
            "scope": scope,
            "batch": str(d.get("batch") or ""),
            "title": title,
        })
    return cases, skipped


def apply_writes(bank: Path, cases: list[dict], batch_name: str) -> tuple[int, int]:
    conn = sqlite3.connect(bank)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, "
            "confirmed INTEGER, rule_version TEXT)"
        )
        keep = {c["case_id"] for c in cases}
        stale = []
        for (payload,) in conn.execute("SELECT payload FROM cases").fetchall():
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if data.get("source") != SOURCE or data.get("source_batch") != batch_name:
                continue
            if str(data.get("case_id")) not in keep:
                stale.append(str(data.get("case_id")))
        for cid in stale:
            conn.execute("DELETE FROM cases WHERE case_id=?", (cid,))
        for c in cases:
            case: CaseRecord = c["case"]
            payload = asdict(case)
            payload.update({
                "source": SOURCE,
                "source_batch": batch_name,
                # create_draft 不写 video_id，这里必须自己补，否则同视频检索永远空。
                "video_id": c["video_id"],
                "video_title": c["video_title"],
                "polarity": "positive",
                "trigger_issue_codes": [],
                "confirmed": True,
                "case_status": "confirmed",
            })
            conn.execute(
                "INSERT OR REPLACE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,?,?)",
                (case.case_id, json.dumps(payload, ensure_ascii=False), 1, case.rule_version),
            )
        conn.commit()
    finally:
        conn.close()
    return len(cases), len(stale)


def verify(bank: Path, cases: list[dict], version: str) -> int:
    """按生成时的真实 query 形态回查：证明确实能被检索到，而不是只写进了表。"""
    conn = sqlite3.connect(bank)
    try:
        store = MemoryStore(conn)
        ok = 0
        for c in cases:
            query = c["video_title"] or c["title"]
            scope = [x for x in c["scope"].split() if x]
            hits = store.search(query, ["表达", "事实与信源"], version, limit=5, scope=scope)
            hit = c["case_id"] in [h.get("case_id") for h in hits]
            ok += hit
            mark = "✓" if hit else "✗"
            print(f"  {mark} {c['case_id']}  query=「{query[:28]}」")
        return ok
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="把「精彩」标题导入共享案例库")
    parser.add_argument("--batch-dir", required=True, help="批次目录（含 human_feedback_v2.db 与 agent.db）")
    parser.add_argument("--case-bank", default="runtime/cases.db", help="共享案例库路径")
    parser.add_argument("--rule-version", default="", help="覆盖规则包版本，默认取 RunConfig.prompt_pack_version")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要写入什么，不动库")
    parser.add_argument("--verify", action="store_true", help="写入后用真实 query 回查能否检索到")
    args = parser.parse_args()

    batch = Path(args.batch_dir).resolve()
    fb_db = batch / "human_feedback_v2.db"
    src_db = batch / "agent.db"
    for path in (fb_db, src_db):
        if not path.exists():
            print(f"缺少文件：{path}", file=sys.stderr)
            return 2
    bank = Path(args.case_bank).resolve()
    version = args.rule_version or RunConfig().prompt_pack_version

    decisions = read_feedback(fb_db)
    if not decisions:
        print(f"{batch.name}：还没有任何评审记录。")
        return 0
    cases, skipped = build_cases(decisions, src_db, version)

    print(f"批次 {batch.name}：{len(decisions)} 条判定，其中精彩 {len(cases)} 条")
    print(f"目标案例库：{bank}（规则包版本 {version}）")
    for c in cases:
        print(f"  · {c['case_id']}")
        print(f"    标题：{c['title']}")
        print(f"    领域：{c['scope']}")
        if c["case"].bad_example:
            print(f"    对照（同视频被判不可用）：{c['case'].bad_example}")
    for task_id, why in skipped:
        print(f"  ! 跳过 {task_id}：{why}")
    if not cases:
        print("没有可导入的精彩标题。")
        return 0

    if args.dry_run:
        print("\n[dry-run] 未写入。")
        return 0

    if not bank.exists():
        print(f"案例库不存在：{bank}（请先跑一次生成以初始化）", file=sys.stderr)
        return 2
    written, removed = apply_writes(bank, cases, batch.name)
    print(f"\n已写入 {written} 条，清理失效 {removed} 条。")

    if args.verify:
        print("\n回查（用该视频自己的标题当 query）：")
        ok = verify(bank, cases, version)
        print(f"\n检索验证：{ok}/{len(cases)} 条命中。")
        if ok != len(cases):
            print("有案例写进去了却检索不到，检查 scope 与 rule_version。", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
