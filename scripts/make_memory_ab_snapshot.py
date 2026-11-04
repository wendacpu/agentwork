"""为 Memory A/B 对照实验准备「留一法」案例库快照。

A 臂要回答的是「**别人的**经验能不能帮到当前视频」，不是「能不能抄到自己的答案」。
所以快照必须剔除评测集视频自己的案例。实测 D 集 20 条视频在共享库里贡献了 56 条
案例（占 142 条的 39%）——不剔除，A 臂就是在抄答案，测出来再好看也不算数。

无 ``video_id`` 的案例无法判断归属，一并剔除：宁可少 12 条，不可泄漏。
"""
from __future__ import annotations

import argparse
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
    "CREATE TABLE IF NOT EXISTS snapshot_meta(key TEXT PRIMARY KEY, value TEXT)",
)


def load_excluded(manifest: Path) -> set[str]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    samples = data.get("samples") if isinstance(data, dict) else data
    return {str(s.get("video_id")) for s in (samples or []) if s.get("video_id")}


def build(source: Path, target: Path, excluded: set[str], manifest: Path) -> dict:
    if target.resolve() == source.resolve():
        raise SystemExit("快照不能覆盖源库")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    dst = sqlite3.connect(target)
    try:
        for sql in SCHEMA:
            dst.execute(sql)
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        kept = leaked = orphan = 0
        try:
            rows = src.execute("SELECT case_id, payload, rule_version FROM cases WHERE confirmed=1").fetchall()
        finally:
            src.close()
        for case_id, payload, rule_version in rows:
            video_id = str((json.loads(payload) or {}).get("video_id") or "")
            if not video_id:
                orphan += 1
                continue
            if video_id in excluded:
                leaked += 1
                continue
            dst.execute("INSERT OR IGNORE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,1,?)",
                        (case_id, payload, rule_version))
            kept += 1
        for key, value in {
            "source": str(source.resolve()), "manifest": str(manifest.resolve()),
            "excluded_videos": str(len(excluded)), "excluded_cases": str(leaked),
            "dropped_without_video_id": str(orphan), "kept": str(kept),
        }.items():
            dst.execute("INSERT OR REPLACE INTO snapshot_meta VALUES(?,?)", (key, value))
        dst.commit()
        return {"kept": kept, "excluded_by_manifest": leaked, "dropped_without_video_id": orphan,
                "source_total": len(rows), "target": str(target)}
    finally:
        dst.close()


def verify(target: Path, excluded: set[str], source: Path) -> dict:
    """独立复核：快照里不许出现评测集视频，也不许无归属案例。"""
    conn = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        found: list[str] = []
        orphan = 0
        for (payload,) in conn.execute("SELECT payload FROM cases WHERE confirmed=1"):
            video_id = str((json.loads(payload) or {}).get("video_id") or "")
            if not video_id:
                orphan += 1
            elif video_id in excluded:
                found.append(video_id)
        kept = conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
    finally:
        conn.close()
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        source_total = src.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
    finally:
        src.close()
    return {"leaked_videos": sorted(set(found)), "leaked_cases": len(found),
            "cases_without_video_id": orphan, "kept": kept, "source_total": source_total,
            "ok": not found and orphan == 0}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=str(ROOT / "runtime" / "cases.db"))
    p.add_argument("--target", default=str(ROOT / "runtime" / "cases_memory_ab.db"))
    p.add_argument("--manifest", default=str(ROOT / "templates" / "eval_manifest.json"))
    args = p.parse_args()
    source, target, manifest = Path(args.source), Path(args.target), Path(args.manifest)
    excluded = load_excluded(manifest)
    built = build(source, target, excluded, manifest)
    checked = verify(target, excluded, source)
    print(json.dumps({"build": built, "verify": checked}, ensure_ascii=False, indent=2))
    return 0 if checked["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
