#!/usr/bin/env python3
"""跑基线：大模型直出 + 基础审核，输出可对比的结果。

用法：
    python3 scripts/baseline_run.py                 # 读默认 config（mock）
    python3 scripts/baseline_run.py --config config.deepseek.json
    python3 scripts/baseline_run.py --mock          # 强制 mock，不花额度
    python3 scripts/baseline_run.py --manifest templates/eval_manifest.json --output runtime/baseline/eval-v1.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.baseline import run_baseline
from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, OpenAICompatibleAdapter

TABLE = ROOT / "data" / "source" / "video_dataset.json"
PROVIDER_DEFAULTS = {
    "ollama": ("http://localhost:11434/v1", "OLLAMA_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    # 对照实验要求两臂用同一个模型：系统臂走 juflow，基线臂也必须走 juflow，
    # 否则两臂差的不只是链路，还差一个供应商。
    "juflow": ("https://juflowai.com/v1", "JUFLOW_API_KEY"),
}


def build_adapter(config: RunConfig, force_mock: bool):
    if force_mock or config.model_provider == "mock":
        return MockAdapter()
    defaults = PROVIDER_DEFAULTS.get(config.model_provider)
    if not defaults:
        raise SystemExit(f"unknown model_provider: {config.model_provider}")
    base_url = config.base_url or defaults[0]
    key_env = config.api_key_env or defaults[1]
    return OpenAICompatibleAdapter(base_url, key_env, config.generation_model, reasoning_effort=config.reasoning_effort)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="")
    ap.add_argument("--manifest", default=str(ROOT / "templates" / "eval_manifest.json"))
    ap.add_argument("--output", default=str(ROOT / "runtime" / "baseline" / "eval-v1.json"))
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()
    dataset = Dataset.from_paths(config.table or TABLE, config.asset_root, config.ocr_mode)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    adapter = build_adapter(config, args.mock)

    samples = manifest.get("samples", [])
    if args.limit:
        samples = samples[: args.limit]
    results, errors = [], []
    for sample in samples:
        vid = sample["video_id"]
        try:
            video = dataset.get(vid)
            row = run_baseline(video, adapter)
            row["domain_1"] = sample.get("domain_1")
            row["domain_2"] = sample.get("domain_2")
            row["form"] = sample.get("form")
            results.append(row)
        except Exception as exc:  # 单条失败不拖垮整批
            errors.append({"video_id": vid, "error": str(exc)})

    out = {
        "version": manifest.get("version"),
        "model_provider": config.model_provider,
        "generation_model": config.generation_model,
        "sample_size": manifest.get("sample_size"),
        "results": results,
        "errors": errors,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 进了 results 不等于成功：审核技术异常的原稿也会保留（否则已生成标题会丢），
    # 但它不是「写出来了」，更不是「审核通过」，必须分开报数。
    status_counts: dict[str, int] = {}
    for r in results:
        key = f"{r.get('generate_status', 'legacy')}/{r.get('audit_status', 'legacy_unknown')}"
        status_counts[key] = status_counts.get(key, 0) + 1
    print(f"样本 {len(manifest.get('samples', []))} 条，有结果 {len(results)}，未执行 {len(errors)}")
    print(f"生成/审核状态：{json.dumps(status_counts, ensure_ascii=False)}")
    for r in results[:5]:
        print(f"  {r['video_id']}  {r['title']}  审核={r.get('audit_status', 'legacy_unknown')}")
    if errors:
        print("失败明细:", json.dumps(errors, ensure_ascii=False)[:500])
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
