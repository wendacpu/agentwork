"""案例回流健康度：回答「经验库到底有没有在反哺」这一个问题。

没有观测就没有闭环：检索命中率长期为 0 时，系统看起来一切正常。
只读扫描，不改任何库。
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scan_db(path: Path) -> dict:
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute("SELECT video_id, state_json FROM tasks WHERE state_json LIKE '%memory_trace%'").fetchall()
        states = Counter()
        used = same_video = 0
        for _vid, raw in rows:
            try:
                state = json.loads(raw)
            except ValueError:
                continue
            for trace in state.get("memory_trace") or []:
                status = str(trace.get("status") or "unknown")
                states[status] += 1
                if trace.get("used_case_ids"):
                    used += 1
                    if status == "same_video":
                        same_video += 1
    except sqlite3.Error:
        conn.close()
        return {}
    conn.close()
    total = sum(states.values())
    return {"tasks": len(rows), "lookups": total, "used": used, "same_video": same_video,
            "states": dict(states), "hit_rate": round(used / total, 4) if total else None}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bank", default=str(ROOT / "runtime" / "cases.db"))
    p.add_argument("--pattern", action="append", default=[])
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    bank_path = Path(args.bank)
    bank = {}
    if bank_path.exists():
        conn = sqlite3.connect(bank_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            bank["cases"] = conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
            videos, versions = set(), Counter()
            for (payload,) in conn.execute("SELECT payload FROM cases WHERE confirmed=1"):
                data = json.loads(payload)
                if data.get("video_id"):
                    videos.add(str(data["video_id"]))
                versions[data.get("rule_version") or "?"] += 1
            bank["distinct_videos"] = len(videos)
            bank["rule_versions"] = dict(versions)
            try:
                bank["sources"] = conn.execute("SELECT COUNT(DISTINCT source_path) FROM case_sources").fetchone()[0]
            except sqlite3.Error:
                bank["sources"] = None
        finally:
            conn.close()

    patterns = args.pattern or [str(ROOT / "runtime" / "**" / "agent.db")]
    per_db = {}
    for path in sorted({Path(x) for pattern in patterns for x in glob.glob(pattern, recursive=True)}):
        if "_legacy" in path.parts or path.resolve() == bank_path.resolve():
            continue
        stats = scan_db(path)
        if stats.get("lookups"):
            per_db[str(path.relative_to(ROOT))] = stats

    total_lookups = sum(v["lookups"] for v in per_db.values())
    total_used = sum(v["used"] for v in per_db.values())
    report = {
        "shared_bank": bank,
        "shared_bank_path": str(bank_path),
        "lookups_total": total_lookups,
        "used_total": total_used,
        "hit_rate": round(total_used / total_lookups, 4) if total_lookups else None,
        "same_video_hits": sum(v["same_video"] for v in per_db.values()),
        "per_db": per_db,
        "note": "这些是历史运行留下的记录，多数发生在案例库共享修复之前；判断修复效果要看共享库启用后的新运行。",
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"共享案例库 {bank.get('cases', 0)} 条 / {bank.get('distinct_videos', 0)} 个视频 / 来自 {bank.get('sources', '?')} 个实验目录")
    print(f"历史检索 {total_lookups} 次，命中 {total_used} 次（{report['hit_rate']}），其中同视频命中 {report['same_video_hits']}")
    for name, stats in sorted(per_db.items(), key=lambda kv: -kv[1]["lookups"]):
        print(f"  {name}: 查询{stats['lookups']:>4} 命中{stats['used']:>4} 同视频{stats['same_video']:>3} 状态{dict(stats['states'])}")
    print(report["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
