#!/usr/bin/env python3
"""双标注里的「模型副标注」：对一个候选独立判三档，输出 labels.json。

用法：
    python3 scripts/model_review.py --gold runtime/gold/eval-v1.csv --system current --mock
    python3 scripts/model_review.py --gold runtime/gold/eval-v1.csv --system current --config config.deepseek.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, OpenAICompatibleAdapter

TABLE = ROOT / "data" / "source" / "video_dataset.json"
PROVIDER_DEFAULTS = {
    "ollama": ("http://localhost:11434/v1", "OLLAMA_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
}

SYSTEM = """你是短视频标题评审。只看标题和必要的视频材料，判这条标题能不能用：
- 可用：准确、清楚，能兑现视频主要内容，不用改一处；
- 勉强通过：基本可用但有明确瑕疵，只差局部一处；
- 不可用：要换看点/补事实/改方向，一处修改救不回来。
只输出 JSON：{"verdict": "可用" 或 "勉强通过" 或 "不可用", "reason": "一句话"}"""


def col(row, prefix):
    for k, v in row.items():
        if k and k.startswith(prefix):
            return (v or "").strip()
    return ""


def build_adapter(config, force_mock):
    if force_mock or config.model_provider == "mock":
        return MockAdapter()
    defaults = PROVIDER_DEFAULTS.get(config.model_provider)
    if not defaults:
        raise SystemExit(f"unknown model_provider: {config.model_provider}")
    return OpenAICompatibleAdapter(config.base_url or defaults[0], config.api_key_env or defaults[1], config.generation_model, reasoning_effort=config.reasoning_effort)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--system", default="current", help="baseline / current")
    ap.add_argument("--config", default="")
    ap.add_argument("--output", default=str(ROOT / "runtime" / "review" / "labels.json"))
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()
    dataset = Dataset.from_paths(config.table or TABLE, config.asset_root, config.ocr_mode)
    adapter = build_adapter(config, args.mock)

    labels, errors = [], []
    with open(args.gold, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            vid = col(row, "video_id")
            cid = col(row, "candidate_id")
            title = col(row, "标题")
            if not vid or not title:
                continue
            try:
                video = dataset.get(vid)
                payload = {
                    "title": title,
                    "video": {
                        "title": video.title,
                        "description": video.description,
                        "caption": video.caption,
                        "asr": [str(s.get("text", "")) for s in (video.asr_segments or [])][:20],
                        "ocr": [str(s.get("text", "")) for s in (video.ocr_segments or [])][:20],
                    },
                }
                reply = adapter.complete(task="model_review", system=SYSTEM, user=payload, schema="review")
                data = reply.data or {}
                labels.append({"video_id": vid, "candidate_id": cid, "title": title,
                               "verdict": str(data.get("verdict") or ""), "reason": str(data.get("reason") or "")})
            except Exception as exc:
                errors.append({"video_id": vid, "candidate_id": cid, "error": str(exc)})

    out = {"system": args.system, "model": config.generation_model, "labels": labels, "errors": errors}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"标注 {len(labels)} 条，失败 {len(errors)} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
