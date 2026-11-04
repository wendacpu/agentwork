"""把分散在各实验目录的人工确认案例汇入共享案例库（只读源库、幂等）。

背景：案例默认写在 ``<output_dir>/agent.db``，所以每个新实验从零开始积累，
历史人工判定锁死在各自目录里，跨运行检索命中率实测只有 3.7%。
本脚本一次性把已有确认案例搬到 workspace 级共享库 ``runtime/cases.db``。
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, confirmed INTEGER, rule_version TEXT)",
    "CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, case_id TEXT, video_id TEXT, layer TEXT, problem_type TEXT, raw_reason_type TEXT, reason TEXT, check_hint TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS case_sources(case_id TEXT PRIMARY KEY, source_path TEXT, merged_at TEXT)",
)


def merge(target: Path, sources: list[Path]) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(target, timeout=30)
    try:
        for sql in SCHEMA:
            dst.execute(sql)
        added = skipped = updated = 0
        for path in sources:
            if path.resolve() == target.resolve():
                continue
            # 旧目录（_legacy）里的案例属于已废弃规则版本，不并入共享库。
            if "_legacy" in path.parts:
                continue
            try:
                src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                rows = src.execute("SELECT case_id, payload, rule_version FROM cases WHERE confirmed=1").fetchall()
            except sqlite3.Error:
                src.close()
                continue
            try:
                task_video = dict(src.execute("SELECT task_id, video_id FROM tasks").fetchall())
            except sqlite3.Error:
                task_video = {}
            for case_id, payload, rule_version in rows:
                # 补 video_id：同视频检索只看 payload，共享库没有 tasks 表可 JOIN。
                data = json.loads(payload)
                if not data.get("video_id"):
                    data["video_id"] = str(task_video.get(data.get("source_task_id"), "") or "")
                    payload = json.dumps(data, ensure_ascii=False)
                existing = dst.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
                if existing:
                    # 用「字段是否存在」判幂等：空视频ID也是已回填的结果，
                    # 否则每次汇入都会把这批记录重写一遍。
                    if "video_id" in json.loads(existing[0]):
                        skipped += 1
                        continue
                    dst.execute("UPDATE cases SET payload=? WHERE case_id=?", (payload, case_id))
                    dst.execute("INSERT OR REPLACE INTO case_sources VALUES(?,?,datetime('now'))", (case_id, str(path)))
                    updated += 1
                    continue
                # 只补不覆盖：共享库里已有的修订不因再次汇入而被旧数据改写。
                dst.execute(
                    "INSERT OR IGNORE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,1,?)",
                    (case_id, payload, rule_version),
                )
                dst.execute("INSERT OR REPLACE INTO case_sources VALUES(?,?,datetime('now'))", (case_id, str(path)))
                added += 1
            src.close()
        dst.commit()
        total = dst.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
        with_video = sum(1 for (p,) in dst.execute("SELECT payload FROM cases") if json.loads(p).get("video_id"))
        return {"added": added, "backfilled_video_id": updated, "already_present": skipped,
                "shared_total": total, "with_video_id": with_video, "target": str(target)}
    finally:
        dst.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", default=str(ROOT / "runtime" / "cases.db"))
    p.add_argument("--pattern", action="append", default=[], help="源库 glob，缺省扫 runtime/**/agent.db")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    patterns = args.pattern or [str(ROOT / "runtime" / "**" / "agent.db")]
    sources = sorted({Path(x) for pattern in patterns for x in glob.glob(pattern, recursive=True)})
    if args.dry_run:
        print(json.dumps({"sources": [str(s) for s in sources], "target": args.target, "dry_run": True}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(merge(Path(args.target), sources), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
