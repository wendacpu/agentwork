#!/usr/bin/env python3
"""A/B 验证：外部核查的「弱反驳」约束是否真的改变了标题生成。

同一视频跑两遍真实模型：
  A = 核查关闭（基线）
  B = 核查开启 + 固定一条「单源反驳」（证据不足，应降级为限定表达）

B 的 verify 结果被固定，其余 understand/boundary/highlight/generate/review 全部走真实模型，
这样两边的差异只来自核查约束本身。搜索走真实 WSA。

用法：
    set -a; source .env; set +a
    python3 scripts/verify_constraint_ab.py --video BV11Ssf6gEUV
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import ModelAdapter, ModelReply, OpenAICompatibleAdapter
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage
from agent_system.verification.search import WsaSearchProvider

TABLE = ROOT / "data" / "source" / "video_dataset.json"
PROVIDER_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
}


class FixedVerifyAdapter:
    """除 verify 之外全部转发给真实模型；verify 固定成指定结论，隔离变量。"""

    def __init__(self, inner: ModelAdapter, verdict: str, sources: list[str], confidence: float) -> None:
        self.inner = inner
        self.verdict = verdict
        self.sources = sources
        self.confidence = confidence
        self.model = getattr(inner, "model", "unknown")

    def complete(self, *, task: str, system: str, user: dict, schema: str, attempt: int = 0) -> ModelReply:
        if task == "verify":
            claim = user.get("claim", {})
            return ModelReply("ok", {
                "claim_id": claim.get("claim_id", ""),
                "verdict": self.verdict,
                "sources": self.sources,
                "confidence": self.confidence,
                "query": claim.get("text", ""),
            })
        return self.inner.complete(task=task, system=system, user=user, schema=schema, attempt=attempt)


def run_case(args, *, verification_enabled: bool, fixed: tuple[str, list[str], float] | None) -> dict:
    base, key_env = PROVIDER_DEFAULTS[args.provider]
    adapter = OpenAICompatibleAdapter(base, key_env, args.model, timeout=args.timeout)
    review: ModelAdapter = adapter if fixed is None else FixedVerifyAdapter(adapter, *fixed)
    tmp = tempfile.TemporaryDirectory()
    config = RunConfig(
        asset_root=args.asset_root,
        output_dir=tmp.name,
        verification_enabled=verification_enabled,
        verification_provider="wsa",
        refute_min_sources=2,
        refute_confidence_threshold=0.8,
        # 约束 A/B 是对照实验：显式隔离案例库，避免历史案例混进任一臂。
        case_bank_isolated=True,
    )
    dataset = Dataset.from_paths(TABLE, Path(args.asset_root), "filtered")
    # 故意不接 resolve_case_bank：见上方 case_bank_isolated=True。
    storage = Storage(Path(tmp.name) / "agent.db")
    pipeline = Pipeline(dataset, storage, adapter, config, review, WsaSearchProvider())
    task_id = pipeline.run(args.video)
    row = storage.get_task(task_id)
    state = json.loads(row["state_json"])
    generate_payload = ""
    for event in storage.events(task_id):
        if event["event_type"] == "prompt_snapshot" and event["payload"].get("task") == "generate":
            generate_payload = json.dumps(event["payload"].get("user_payload"), ensure_ascii=False)
    decisions = (state.get("boundary") or {}).get("decisions") or []
    return {
        "status": row["status"],
        "headlines": [c.get("rendered_title") or c.get("headline") for c in (state.get("candidates") or [])],
        "verdicts": [row.get("verdict") for row in (state.get("verification") or [])],
        "risk": (state.get("verification_risk") or {}).get("level"),
        "limited_claims": state.get("verification_limited_claims") or [],
        "boundary": [{"claim_id": d.get("claim_id"), "decision": d.get("decision"), "limits": d.get("required_limits") or []} for d in decisions],
        "generate_sees_limit": "证据不足" in generate_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", default="BV11Ssf6gEUV")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--asset-root", default="/Users/wanting/program/新项目/复杂agent/recent-portrait-100")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--refute-source", default="https://news.cn/a")
    args = parser.parse_args()

    print("=== A 基线（核查关闭）===", flush=True)
    baseline = run_case(args, verification_enabled=False, fixed=None)
    print(json.dumps(baseline, ensure_ascii=False, indent=2), flush=True)

    print("\n=== B 核查开启 + 单源反驳（弱反驳）===", flush=True)
    constrained = run_case(
        args, verification_enabled=True,
        fixed=("refuted", [args.refute_source], 0.95),
    )
    print(json.dumps(constrained, ensure_ascii=False, indent=2), flush=True)

    print("\n=== 对比 ===", flush=True)
    same = baseline["headlines"] == constrained["headlines"]
    print(json.dumps({
        "headline_changed": not same,
        "baseline": baseline["headlines"],
        "constrained": constrained["headlines"],
        "constraint_reached_generate": constrained["generate_sees_limit"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
