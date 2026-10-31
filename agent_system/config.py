from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunConfig:
    rule_version: str = "temporary-v1"
    prompt_pack_version: str = "red-dot-v0.2-modular"
    rubric_version: str = "red-dot-review-v2-uncalibrated"
    memory_version: str = "dev-empty-v1"
    target_count: int = 3
    max_chars: int = 20
    schema_retry_limit: int = 1
    max_rewrite_rounds: int = 2
    max_rewrite_candidates: int = 3
    rewrite_candidate_count: int = 2
    memory_call_limit: int = 6
    memory_calls_per_round: int = 2
    reference_time: str = ""
    ocr_mode: str = "filtered"
    model_provider: str = "mock"
    base_url: str = ""
    api_key_env: str = ""
    reasoning_effort: str = ""
    generation_model: str = "mock"
    review_model: str = "mock"
    asset_root: str = "/Users/wanting/program/新项目/复杂agent/recent-portrait-100"
    output_dir: str = "runtime"
    # 独立案例库路径；空 = 与任务库同库（旧行为）。配置后案例跨实验目录共享。
    case_bank: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # 外部实时核查配置（P2）：哪些品类核查、refuted 阈值、单视频核查条数上限
    verification_enabled: bool = False
    verifiable_domains: list[str] = field(default_factory=lambda: ["时事社会"])
    excluded_domains: list[str] = field(default_factory=list)
    verifiable_claim_types: list[str] = field(default_factory=lambda: ["observable", "source_claim"])
    max_verification_claims: int = 3
    refute_min_sources: int = 2
    refute_confidence_threshold: float = 0.8
    verification_provider: str = "none"  # none / wsa
    verification_secret_id_env: str = "WSA_SECRET_ID"
    verification_secret_key_env: str = "WSA_SECRET_KEY"

    @classmethod
    def from_json(cls, path: str | Path) -> "RunConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def apply_env(self) -> "RunConfig":
        for key, value in {
            "verification_enabled": os.getenv("AGENT_VERIFICATION_ENABLED"),
            "verification_provider": os.getenv("AGENT_VERIFICATION_PROVIDER"),
            "verification_secret_id_env": os.getenv("AGENT_VERIFICATION_SECRET_ID_ENV"),
            "verification_secret_key_env": os.getenv("AGENT_VERIFICATION_SECRET_KEY_ENV"),
        }.items():
            if value is not None:
                setattr(self, key, value.strip().lower() in {"1", "true", "yes", "on"} if key == "verification_enabled" else value)
        for key, value in {
            "asset_root": os.getenv("AGENT_ASSET_ROOT"),
            "model_provider": os.getenv("AGENT_MODEL_PROVIDER"),
            "generation_model": os.getenv("AGENT_GENERATION_MODEL"),
            "base_url": os.getenv("AGENT_BASE_URL"),
            "api_key_env": os.getenv("AGENT_API_KEY_ENV"),
            "review_model": os.getenv("AGENT_REVIEW_MODEL"),
            "output_dir": os.getenv("AGENT_OUTPUT_DIR"),
        }.items():
            if value:
                setattr(self, key, value)
        return self

