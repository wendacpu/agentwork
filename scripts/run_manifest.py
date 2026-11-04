#!/usr/bin/env python3
"""按 manifest 跑当前系统，产出「视频 → task_id + 候选」清单，供后续回填/标注/对比。

用法：
    python3 scripts/run_manifest.py --mock                 # 不花额度，验证管道
    python3 scripts/run_manifest.py --config config.deepseek.json   # 真跑
    python3 scripts/run_manifest.py --limit 5              # 先试 5 条
"""
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
    ap.add_argument("--config", default="")
    ap.add_argument("--manifest", default=str(ROOT / "templates" / "eval_manifest.json"))
    ap.add_argument("--output", default=str(ROOT / "runtime" / "current" / "eval-v1.json"))
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()
    if args.mock:
        config.model_provider = "mock"
    dataset = Dataset.from_paths(config.table or TABLE, config.asset_root, config.ocr_mode)
    storage = Storage(Path(config.output_dir) / "agent.db", case_bank_path=(resolve_case_bank(config) or None))
    pipeline = make_pipeline(config, dataset, storage)

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    samples = manifest.get("samples", [])
    if args.limit:
        samples = samples[: args.limit]

    results, errors = [], []
    for sample in samples:
        vid = sample["video_id"]
        try:
            task_id = pipeline.run(vid)
            state = json.loads(storage.get_task(task_id)["state_json"] or "{}")
            candidates = [
                {"candidate_id": c.get("candidate_id"), "title": c.get("rendered_title") or c.get("headline")}
                for c in (state.get("candidates") or [])
            ]
            results.append({
                "video_id": vid,
                "task_id": task_id,
                "status": storage.get_task(task_id)["status"],
                "route": state.get("route") or {},
                "candidates": candidates,
                "domain_1": sample.get("domain_1"),
                "domain_2": sample.get("domain_2"),
                "form": sample.get("form"),
            })
        except Exception as exc:
            errors.append({"video_id": vid, "error": str(exc)})

    out = {
        "version": manifest.get("version"),
        "model_provider": config.model_provider,
        "generation_model": config.generation_model,
        "review_model": config.review_model,
        "sample_size": len(samples),
        "results": results,
        "errors": errors,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"样本 {len(samples)} 条，成功 {len(results)}，失败 {len(errors)}")
    for r in results[:5]:
        print(f"  {r['video_id']}  task={r['task_id']}  status={r['status']}  候选={len(r['candidates'])}")
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
