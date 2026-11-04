#!/usr/bin/env python3
"""续跑技术失败的当前系统任务（只动 technical_failed）。"""
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

    run = json.loads((ROOT / args.run).read_text(encoding="utf-8"))
    ok, fail = 0, 0
    for r in run.get("results", []):
        if r.get("status") != "technical_failed":
            continue
        try:
            pipeline.resume(r["task_id"])
            state = json.loads(storage.get_task(r["task_id"])["state_json"] or "{}")
            cands = len(state.get("candidates") or [])
            print(f"resumed {r['video_id']} {r['task_id']} -> candidates={cands} status={storage.get_task(r['task_id'])['status']}")
            ok += 1
        except Exception as exc:
            print(f"resume FAILED {r['video_id']} {r['task_id']}: {type(exc).__name__}: {str(exc)[:200]}")
            fail += 1
    print(f"续跑成功 {ok}，失败 {fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
