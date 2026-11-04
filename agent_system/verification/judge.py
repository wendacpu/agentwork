"""核查结果判定与风险汇总（搜索核查 Agent 的第 4 层）。"""
from __future__ import annotations

from typing import Any

from ..types import VerificationResult

VALID_VERDICTS = {"supported", "refuted", "unverifiable"}


def parse_verification(raw: dict[str, Any], claim_id: str) -> VerificationResult:
    verdict = str(raw.get("verdict") or "unverifiable")
    if verdict not in VALID_VERDICTS:
        verdict = "unverifiable"
    sources = [str(x) for x in (raw.get("sources") or []) if x]
    confidence = raw.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    return VerificationResult(
        claim_id=claim_id, verdict=verdict, sources=sources,
        confidence=confidence, query=str(raw.get("query") or ""),
    )


def compute_risk(claims: list[Any], refute_min_sources: int = 2, confidence_threshold: float = 0.8) -> dict[str, Any]:
    """视频内容风险：核心断言被 refuted 且反驳来源达阈值 → high（视频高危）。"""
    refuted = []
    for c in claims:
        v = getattr(c, "verification", None)
        if (
            v is not None and v.verdict == "refuted"
            and not getattr(v, "downgraded", False)
            and len(set(v.sources)) >= refute_min_sources
            and v.confidence >= confidence_threshold
        ): 
            refuted.append(c)
    if refuted:
        return {
            "level": "high",
            "refuted_claim_ids": [c.claim_id for c in refuted],
            "reason": "核心断言被权威来源反驳",
        }
    return {"level": "low", "refuted_claim_ids": [], "reason": ""}
