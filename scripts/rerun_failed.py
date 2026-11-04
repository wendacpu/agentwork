#!/usr/bin/env python3
"""重跑当前系统里失败的视频（technical_failed 或 0 候选），并把结果合并回 run map。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.cli import make_pipeline
from agent_system.config import RunConfig, resolve_case_bank
from agent_system.data import Dataset
from agent_system.storage import Storage

TABLE = ROOT / "data" / "source" / "video_dataset.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.deepseek.json")
    ap.add_argument("--run", default="runtime/current/eval-v1.json")
    args = ap.parse_args()

    config = RunConfig.from_json(ROOT / args.config)
    config.apply_env()
    dataset = Dataset.from_paths(TABLE, config.asset_root, config.ocr_mode)
    storage = Storage(Path(config.output_dir) / "agent.db", case_bank_path=(resolve_case_bank(config) or None))
    pipeline = make_pipeline(config, dataset, storage)

    path = ROOT / args.run
    run = json.loads(path.read_text(encoding="utf-8"))
    results = run.get("results", [])
    failed = [r for r in results if r.get("status") != "pending_human" or not r.get("candidates")]
    print(f"待重跑 {len(failed)} 条:", [r["video_id"] for r in failed])

    for r in failed:
        vid = r["video_id"]
        try:
            task_id = pipeline.run(vid)
            state = json.loads(storage.get_task(task_id)["state_json"] or "{}")
            candidates = [
                {"candidate_id": c.get("candidate_id"), "title": c.get("rendered_title") or c.get("headline")}
                for c in (state.get("candidates") or [])
            ]
            r.update({
                "task_id": task_id,
                "status": storage.get_task(task_id)["status"],
                "route": state.get("route") or {},
                "candidates": candidates,
            })
            print(f"  {vid} -> {r['status']} candidates={len(candidates)}")
        except Exception as exc:
            r["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  {vid} FAILED: {exc}")

    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写回 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
