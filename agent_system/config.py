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
    generation_model: str = "mock"
    review_model: str = "mock"
    asset_root: str = "/Users/wanting/program/新项目/复杂agent/recent-portrait-100"
    output_dir: str = "runtime"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: str | Path) -> "RunConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def apply_env(self) -> "RunConfig":
        for key, value in {
            "asset_root": os.getenv("AGENT_ASSET_ROOT"),
            "model_provider": os.getenv("AGENT_MODEL_PROVIDER"),
            "generation_model": os.getenv("AGENT_GENERATION_MODEL"),
            "review_model": os.getenv("AGENT_REVIEW_MODEL"),
            "output_dir": os.getenv("AGENT_OUTPUT_DIR"),
        }.items():
            if value:
                setattr(self, key, value)
        return self

