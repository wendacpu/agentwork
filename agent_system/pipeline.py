from __future__ import annotations

import json
import hashlib
import platform
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import RunConfig
from .data import Dataset
from .memory import MemoryStore
from .model import ModelAdapter, ModelError
from .rules import load_common_rule_ids, load_rule_bundle, load_rules, rubric
from .prompts import prompt_for
from .schemas import GATE_IDS, QUALITY_IDS, validate_review
from .storage import Storage
from .types import BoundarySet, Claim, Highlight, SemanticUnit, Understanding, UnitDecision, to_dict


def _validate_ids(items: list[dict[str, Any]], key: str, allowed: set[str]) -> list[str]:
    errors = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        for ref in item.get(key, []):
            if ref not in allowed: errors.append(f"{i}.{key}: illegal reference {ref}")
        ident = item.get("claim_id") or item.get("highlight_id") or item.get("candidate_id")
        if ident in seen: errors.append(f"duplicate id: {ident}")
        seen.add(ident)
    return errors


def _claim_text(raw: dict[str, Any]) -> str:
    if raw.get("text") or raw.get("statement") or raw.get("claim_text"):
        return str(raw.get("text") or raw.get("statement") or raw.get("claim_text"))
    parts = [raw.get("subject"), raw.get("action"), raw.get("result")]
    return "；".join(str(x) for x in parts if x and x != "unknown") or "未命名陈述"


def _legacy_assertion_permission(value: Any) -> str:
    raw = str(value or "")
    mapped = {
        "direct_fact": "direct", "source_asserted": "attributed",
        "uncertain": "qualified", "unsupported": "forbidden",
    }.get(raw)
    if mapped:
        return mapped
    lowered = raw.lower()
    if "do_not_assert" in lowered or "forbidden" in lowered or "unsupported" in lowered:
        return "forbidden"
    if any(token in raw for token in ("仅可", "仅能", "不得据此确认", "不得独立确认")):
        return "qualified"
    if any(token in lowered for token in ("text_reference_only", "ocr_text_only", "description_text_only", "low_calibration")):
        return "qualified"
    if "inference_only" in lowered or "forbidden" in lowered:
        return "forbidden"
    if any(token in raw for token in ("转述", "报道称", "来源", "引用", "表态归属")):
        return "attributed"
    if "attribut" in lowered or "with_attribution" in lowered or "attributed_only" in lowered:
        return "attributed"
    if "attribut" in lowered or "reported" in lowered or "source_level" in lowered:
        return "attributed"
    if "qualified" in lowered or "low_calibration" in lowered or "not_independent_fact" in lowered:
        return "qualified"
    if lowered in {"direct", "allowed_as_fact", "supported_as_fact"}:
        return "direct"
    return "unknown"


def _legacy_evidence_status(value: Any) -> str:
    raw = str(value or "")
    mapped = {
        "direct_fact": "supported", "source_asserted": "limited",
        "uncertain": "unknown", "unsupported": "unsupported",
    }.get(raw)
    if mapped:
        return mapped
    lowered = raw.lower()
    if "conflict" in lowered:
        return "conflicting"
    if "unsupported" in lowered or "not_supported" in lowered:
        return "unsupported"
    if lowered in {"source", "raw", "raw_ocr"}:
        return "limited"
    if "limited" in lowered or "unverified" in lowered or "partial" in lowered or "uncalibrated" in lowered or "source_level" in lowered or "garbled" in lowered or "raw_ocr" in lowered:
        return "limited"
    if "supported" in lowered or "verified" in lowered or "confirmed" in lowered:
        return "supported"
    return "unknown"


def _claim_type(value: Any) -> str:
    raw = str(value or "unknown")
    lowered = raw.lower()
    if raw in {"observable", "source_claim", "opinion", "prediction", "inference", "fictional_setting"}:
        return raw
    if raw == "direct_fact":
        return "observable"
    if raw == "source_asserted":
        return "source_claim"
    if lowered in {"source_statement_quote", "source_statement_paraphrase", "attributed_report_claim", "reported_event", "video_metadata_statement"} or lowered.startswith("source_") or lowered.startswith("attributed_"):
        return "source_claim"
    if "推断" in raw or "inference" in lowered:
        return "inference"
    if "opinion" in lowered or "commentary" in lowered or ("观点" in raw and "推断" not in raw):
        return "opinion"
    if "prediction" in lowered or "forecast" in lowered:
        return "prediction"
    if "fiction" in lowered or "scenario" in lowered or "setting" in lowered:
        return "fictional_setting"
    if any(token in raw for token in ("标题陈述", "来源简介转述", "转述", "通报", "作者说", "自述", "引用", "引述")):
        return "source_claim"
    if any(token in raw for token in ("观点", "评价", "评论")):
        return "opinion"
    if any(token in raw for token in ("预测", "预期")):
        return "prediction"
    if any(token in raw for token in ("推断", "推测")):
        return "inference"
    if any(token in raw for token in ("剧情", "演绎", "虚构")):
        return "fictional_setting"
    if any(token in raw for token in ("观察", "画面", "直接事实")) or lowered == "observation":
        return "observable"
    return "unknown"


def _temporal_status(value: Any) -> str:
    raw = str(value or "unknown").lower()
    if raw in {"current", "historical", "dynamic", "unknown"}:
        return raw
    if any(token in raw for token in ("past", "historical", "过去", "回顾", "历史")):
        return "historical"
    if any(token in raw for token in ("dynamic", "changing", "持续变化", "动态")):
        return "dynamic"
    if any(token in raw for token in ("present", "current", "当前")) and "not_temporal" not in raw:
        return "current"
    return "unknown"


def _record_list(value: Any, *, string_key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return [{string_key: str(value)}] if value else []
    return [dict(item) if isinstance(item, dict) else {string_key: str(item)} for item in value]


def _source_object(item: dict[str, Any]) -> dict[str, Any] | None:
    source = item.get("original_source")
    if isinstance(source, dict):
        return dict(source)
    if source:
        return {"name": str(source), "type": "unknown"}
    name = item.get("source_name") or item.get("source_label")
    return {"name": str(name), "type": "unknown"} if name else None


def _normalize_understanding(raw: dict[str, Any]) -> Understanding:
    # 模型输出形态不稳定：有时 claims 在顶层，有时套在 Understanding/understanding 里，
    # 还有把 understanding 当子字段、claims 平铺在外层的情况。先按「任意层找列表」再解包。
    raw_claims = []
    for key in ("claims", "candidate_claims", "facts"):
        if isinstance(raw.get(key), list) and raw[key]:
            raw_claims = raw[key]
            break
    if not raw_claims:
        for wrapper in ("Understanding", "understanding", "result", "output", "data"):
            inner = raw.get(wrapper)
            if isinstance(inner, dict):
                for key in ("claims", "candidate_claims", "facts"):
                    if isinstance(inner.get(key), list) and inner[key]:
                        raw = inner
                        raw_claims = inner[key]
                        break
            if raw_claims:
                break
    claims = []
    for i, item in enumerate(raw_claims):
        item = dict(item)
        refs = item.get("evidence_refs") or item.get("evidence_ids") or []
        raw_claim_id = str(item.get("claim_id") or item.get("fact_id") or f"c{i+1}")
        claim_id = "c" + str(int(raw_claim_id[2:])) if raw_claim_id.startswith("c-") and raw_claim_id[2:].isdigit() else raw_claim_id
        claims.append(Claim(
            claim_id=claim_id, text=_claim_text(item) or str(item.get("statement") or "未命名陈述"),
            claim_type=_claim_type(item.get("claim_type") or item.get("kind") or item.get("type")),
            evidence_refs=[str(x) for x in refs],
            time_refs=[str(x) for x in (item.get("time_refs") or ([item["event_time"]] if item.get("event_time") else []))],
            evidence_status=_legacy_evidence_status(item.get("evidence_status") or item.get("assertion_judgment") or item.get("assertion_decision")),
            original_source=_source_object(item),
            transmission_path=_record_list(item.get("transmission_path"), string_key="actor"),
            temporal_status=_temporal_status(item.get("temporal_status")),
            assertion_permission=_legacy_assertion_permission(item.get("assertion_permission") or item.get("assertion_judgment") or item.get("assertion_decision")),
            entities=_record_list(item.get("entities"), string_key="name"),
        ))
    attrs = raw.get("attributes", [])
    if isinstance(attrs, dict):
        attrs = [f"{k}:{v}" for k, v in attrs.items()]
    event_times = raw.get("event_times") or raw.get("event_time") or []
    if isinstance(event_times, dict): event_times = [event_times]
    units_raw = raw.get("semantic_units") or raw.get("semanticUnits") or []
    if not isinstance(units_raw, list):
        units_raw = []
    semantic_units: list[SemanticUnit] = []
    for i, item in enumerate(units_raw):
        if not isinstance(item, dict):
            continue
        tr = item.get("time_range") or item.get("time_range_s")
        if isinstance(tr, (list, tuple)) and len(tr) == 2:
            try:
                tr = (float(tr[0]), float(tr[1]))
            except (TypeError, ValueError):
                tr = None
        else:
            tr = None
        semantic_units.append(SemanticUnit(
            unit_id=str(item.get("unit_id") or f"u{i+1}"),
            meaning=str(item.get("meaning") or item.get("content") or item.get("thesis") or ""),
            claim_ids=[str(x) for x in (item.get("claim_ids") or item.get("claims") or [])],
            evidence_ids=[str(x) for x in (item.get("evidence_ids") or item.get("evidence_refs") or [])],
            kind=str(item.get("kind") or "support"),
            time_range=tr,
            role_in_video=str(item.get("role_in_video") or item.get("role") or ""),
            corroboration=item.get("corroboration") if isinstance(item.get("corroboration"), dict) else {},
        ))
    # 降级：模型未输出语义单元时，每条原子 claim 包成一个单元，保持旧链路行为
    if not semantic_units:
        for i, c in enumerate(claims):
            semantic_units.append(SemanticUnit(
                unit_id=f"u{i+1}", meaning=c.text, claim_ids=[c.claim_id],
                evidence_ids=list(c.evidence_refs), kind="support",
            ))
    return Understanding(
        main_format=str(raw.get("main_format") or raw.get("content_format") or (raw.get("video") or {}).get("format") or "unknown"),
        attributes=[str(x) for x in attrs], claims=claims, event_times=event_times,
        extracted_sources=raw.get("extracted_sources", []) if isinstance(raw.get("extracted_sources", []), list) else [],
        conflicts=raw.get("conflicts", []) if isinstance(raw.get("conflicts", []), list) else [],
        semantic_units=semantic_units,
    )


def _normalize_boundary(raw: dict[str, Any], claim_texts: dict[str, str] | None = None) -> BoundarySet:
    from .types import ClaimDecision
    if isinstance(raw.get("BoundarySet"), dict):
        raw = raw["BoundarySet"]
    global_requirements = {}
    if isinstance(raw.get("boundary_set"), dict):
        metadata = raw["boundary_set"]
        for key, aliases in {
            "required_subject": ("required_subject", "must_keep_subject"),
            "required_source_phrase": ("required_source_phrase", "source_phrase_required"),
            "generic_subject_not_allowed": ("generic_subject_not_allowed",),
            "length_feasibility": ("length_feasibility",),
        }.items():
            for alias in aliases:
                if alias in metadata:
                    global_requirements[key] = metadata[alias]
                    break
    for key in ("boundary_set", "BoundarySet", "boundarySet"):
        nested = raw.get(key)
        if isinstance(nested, dict) and (any(name in nested for name in ("decisions", "items", "claims", "boundaries")) or (key in {"BoundarySet", "boundarySet"} and nested and all(isinstance(value, dict) for value in nested.values()))):
            raw = nested
            break
    items = raw.get("decisions") or raw.get("items") or raw.get("claims") or raw.get("boundaries") or []
    if not items and isinstance(raw.get("decision"), str):
        # 模型有时把单条决策平铺返回，而不是 {"decisions": [...]}
        items = [dict(raw)]
    if not items and not isinstance(raw.get("decision"), str) and raw and all(isinstance(value, dict) for value in raw.values()):
        items = raw
    if isinstance(items, dict):
        items = [{"claim_id": claim_id, **(item if isinstance(item, dict) else {})} for claim_id, item in items.items()]
    decisions = []
    for i, item in enumerate(items):
        item = dict(item)
        refs = item.get("evidence_refs") or item.get("evidence_ids") or []
        decision = str(item.get("decision") or item.get("status") or item.get("verdict") or item.get("usable") or "excluded")
        if decision in {"allowed", "eligible", "usable_with_limits"}: decision = "limited" if decision == "usable_with_limits" else "usable"
        if decision not in {"usable", "limited", "excluded"}: decision = "excluded"
        decisions.append(ClaimDecision(
            claim_id=str(item.get("claim_id") or f"c{i+1}"), supported_statement=item.get("supported_statement") or item.get("statement") or item.get("text") or (claim_texts or {}).get(str(item.get("claim_id") or f"c{i+1}")),
            decision=decision, evidence_refs=[str(x) for x in refs], required_limits=[str(x) for x in (item.get("required_limits") or item.get("limits") or [])],
            forbidden_inferences=[str(x) for x in (item.get("forbidden_inferences") or [])], rule_ids=[str(x) for x in (item.get("rule_ids") or [])], reason=str(item.get("reason") or item.get("rationale") or ""),
            claim_type=_claim_type(item.get("claim_type") or item.get("kind") or item.get("type")),
            evidence_status=_legacy_evidence_status(item.get("evidence_status") or item.get("assertion_decision") or item.get("assertion_judgment")),
            original_source=_source_object(item),
            transmission_path=_record_list(item.get("transmission_path") or ([{"actor": "relay"}] if item.get("source_decision") == "identified_relay" else []), string_key="actor"),
            temporal_status=_temporal_status(item.get("temporal_status")),
            assertion_permission=_legacy_assertion_permission(item.get("assertion_permission") or item.get("assertion_decision") or item.get("assertion_judgment")),
            entities=_record_list(item.get("entities"), string_key="name"),
            attribution_required=bool(item.get("attribution_required", item.get("required_source_phrase") or item.get("required_attribution") or (item.get("assertion_decision") == "source_asserted" and item.get("source_decision") in {"identified_official", "identified_relay"}))),
            required_source_phrase=item.get("required_source_phrase") or item.get("required_attribution"),
            source_evidence_refs=[str(x) for x in (item.get("source_evidence_refs") or item.get("source_refs") or [])],
            verification_required=bool(item.get("verification_required") or item.get("pending_verification")),
        ))
    units_items = raw.get("unit_decisions") or raw.get("unitDecisions") or []
    if isinstance(units_items, dict):
        units_items = [{"unit_id": k, **(v if isinstance(v, dict) else {})} for k, v in units_items.items()]
    if not isinstance(units_items, list):
        units_items = []
    unit_decisions: list[UnitDecision] = []
    for i, item in enumerate(units_items):
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or item.get("status") or item.get("verdict") or "excluded")
        if decision in {"allowed", "eligible"}: decision = "usable"
        if decision == "usable_with_limits": decision = "limited"
        if decision not in {"usable", "limited", "excluded"}: decision = "excluded"
        unit_decisions.append(UnitDecision(
            unit_id=str(item.get("unit_id") or f"u{i+1}"),
            decision=decision,
            required_limits=[str(x) for x in (item.get("required_limits") or item.get("limits") or [])],
            forbidden_inferences=[str(x) for x in (item.get("forbidden_inferences") or [])],
            attribution_required=bool(item.get("attribution_required") or item.get("required_attribution")),
            required_source_phrase=item.get("required_source_phrase") or item.get("required_attribution"),
            reason=str(item.get("reason") or item.get("rationale") or ""),
        ))
    return BoundarySet(decisions=decisions, unresolved_issues=raw.get("unresolved_issues", []) if isinstance(raw.get("unresolved_issues", []), list) else [], global_requirements=global_requirements, unit_decisions=unit_decisions)


def _normalize_candidates(raw: dict[str, Any], rewrite_round: int, parent_id: str | None) -> list[dict[str, Any]]:
    if isinstance(raw.get("AgentDraft"), dict): raw = raw["AgentDraft"]
    if isinstance(raw.get("agent_draft"), dict): raw = raw["agent_draft"]
    if isinstance(raw.get("output"), dict): raw = raw["output"]
    if not raw.get("candidates") and isinstance(raw.get("items"), list): raw = {**raw, "candidates": raw["items"]}
    if not raw.get("candidates") and isinstance(raw.get("drafts"), list): raw = {**raw, "candidates": raw["drafts"]}
    out = []
    for i, item in enumerate(raw.get("candidates", [])):
        item = dict(item)
        headline = (item.get("headline") or item.get("candidate_title") or item.get("title_text")
                    or item.get("title") or item.get("copy") or item.get("draft_text") or item.get("text"))
        if not headline: continue
        candidate_schema_errors = []
        if not (item.get("candidate_id") or item.get("draft_id") or item.get("id")): candidate_schema_errors.append("candidate_id missing from generation output")
        if not (item.get("highlight_id")): candidate_schema_errors.append("highlight_id missing from generation output")
        if not item.get("claim_ids"): candidate_schema_errors.append("claim_ids missing from generation output")
        if not (item.get("evidence_ids") or item.get("evidence_refs")): candidate_schema_errors.append("evidence_ids missing from generation output")
        claim_ids = []
        for claim_id in item.get("claim_ids") or []:
            text_id = str(claim_id)
            if text_id.startswith("c-") and text_id[2:].isdigit(): text_id = "c" + str(int(text_id[2:]))
            claim_ids.append(text_id)
        out.append({"candidate_id": str(item.get("candidate_id") or item.get("draft_id") or item.get("id") or f"a{rewrite_round+1}-{i+1}"), "headline": str(headline), "highlight_id": str(item.get("highlight_id") or "h1"), "unit_id": str(item.get("unit_id") or item.get("semantic_unit_id") or ""), "claim_ids": claim_ids, "evidence_ids": [str(x) for x in (item.get("evidence_ids") or item.get("evidence_refs") or [])], "expression_strategy": str(item.get("expression_strategy") or item.get("strategy") or "unknown"), "used_case_ids": [str(x) for x in (item.get("used_case_ids") or [])], "generation_round": int(item.get("generation_round", rewrite_round)), "parent_candidate_id": item.get("parent_candidate_id") or parent_id, "change_reason": str(item.get("change_reason") or ""), "qualifiers_used": ([str(item["qualifiers_used"])] if isinstance(item.get("qualifiers_used"), str) else [str(x) for x in (item.get("qualifiers_used") or [])]), "addressed_issue_ids": [str(x) for x in (item.get("addressed_issue_ids") or [])], "generation_schema_errors": candidate_schema_errors})
    return out


def _normalize_highlights(raw: dict[str, Any], allowed_claim_ids: set[str], fallback: list[Highlight]) -> list[Highlight]:
    """Normalize P3 output and reject references outside the boundary."""
    if isinstance(raw.get("HighlightSet"), dict): raw = raw["HighlightSet"]
    items = raw.get("highlights") or raw.get("items") or []
    if not items:
        primary = raw.get("primary_highlight")
        alternatives = raw.get("alternative_highlights") or []
        items = ([primary] if isinstance(primary, dict) else []) + (alternatives if isinstance(alternatives, list) else [])
    if isinstance(items, dict):
        items = list(items.values())
    result: list[Highlight] = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        claim_ids = [str(x) for x in (item.get("claim_ids") or item.get("fact_ids") or []) if str(x) in allowed_claim_ids]
        evidence_ids = [str(x) for x in (item.get("evidence_ids") or item.get("delivery_evidence_refs") or item.get("evidence_refs") or [])]
        value = str(item.get("value") or item.get("content") or item.get("viewer_value") or item.get("what_user_can_see") or item.get("viewpoint") or "").strip()
        if not claim_ids or not value:
            continue
        result.append(Highlight(
            highlight_id=str(item.get("highlight_id") or f"h{i+1}"), claim_ids=claim_ids,
            value=value, representativeness=str(item.get("representativeness") or item.get("selection_reason") or ""),
            delivery_evidence_refs=evidence_ids, role=str(item.get("role") or ("main" if i == 0 else "backup")),
            viewer_value=str(item.get("viewer_value") or item.get("what_user_can_see") or value),
            required_qualifiers=[str(x) for x in (item.get("required_qualifiers") or item.get("required_limits") or item.get("qualifiers") or [])],
            selection_reason=str(item.get("selection_reason") or item.get("representativeness") or ""),
            unit_id=str(item.get("unit_id") or item.get("semantic_unit_id") or ""),
        ))
    return result[:3] or fallback


def _normalize_reviews(raw: dict[str, Any], candidate_ids: list[str]) -> list[dict[str, Any]]:
    if isinstance(raw.get("ReviewResultBatch"), dict):
        raw = raw["ReviewResultBatch"]
    if isinstance(raw.get("ReviewBatch"), dict):
        raw = raw["ReviewBatch"]
    if isinstance(raw.get("review_batch"), dict):
        raw = raw["review_batch"]
    reviews = raw.get("reviews") or raw.get("evaluations") or raw.get("results") or raw.get("candidates")
    if isinstance(reviews, list):
        normalized: dict[str, dict[str, Any]] = {}
        anomalies: list[str] = []
        for item in reviews:
            if not isinstance(item, dict):
                anomalies.append("non-object review item")
                continue
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id not in candidate_ids:
                anomalies.append(f"unknown candidate_id: {candidate_id}")
                continue
            if candidate_id in normalized:
                anomalies.append(f"duplicate review candidate_id: {candidate_id}")
            normalized_item = dict(item)
            if not isinstance(normalized_item.get("expected_takeaway"), dict) and isinstance(normalized_item.get("viewer_expectation"), str):
                normalized_item["expected_takeaway"] = normalized_item["viewer_expectation"]
            if not isinstance(normalized_item.get("quality"), dict) and isinstance(normalized_item.get("quality_scores"), dict):
                normalized_item["quality"] = {
                    key: {"score": value, "reason": str((normalized_item.get("quality_reasons") or {}).get(key) or "")}
                    for key, value in normalized_item["quality_scores"].items()
                }
                if any(not score.get("reason") for score in normalized_item["quality"].values() if isinstance(score, dict)):
                    normalized_item.setdefault("_normalization_errors", []).append("quality score rationale missing")
            if not isinstance(normalized_item.get("memory_query"), dict) and isinstance(normalized_item.get("memory_retrieval_query"), str):
                normalized_item["memory_query"] = {"problem_types": [], "query": normalized_item["memory_retrieval_query"]}
            if isinstance(normalized_item.get("issues"), list):
                for index, issue in enumerate(normalized_item["issues"], start=1):
                    if isinstance(issue, dict):
                        if not issue.get("issue_id"):
                            issue["issue_id"] = f"{candidate_id}-issue-{index}"
                        issue.setdefault("rollback_to", _default_rollback(issue))
            normalized[candidate_id] = normalized_item
        result = [normalized.get(cid, {"candidate_id": cid, "_normalization_errors": ["review missing for candidate"]}) for cid in candidate_ids]
        for item in result:
            if anomalies:
                item.setdefault("_normalization_errors", []).extend(anomalies)
        return result
    # Missing batch output is represented as incomplete per-candidate results;
    # schema validation will route them to human review instead of passing.
    return [{"candidate_id": cid} for cid in candidate_ids]


def _has_explicit_source_relation(headline: str) -> bool:
    """Reject generic source labels such as '通报：' without a relation word."""
    text = str(headline).strip()
    if text.startswith(("通报：", "通报:", "通报称")):
        return False
    if "通报：" in text or "通报:" in text:
        prefix = text.split("通报", 1)[0]
        return any(word in prefix for word in ("据", "称", "显示", "指出", "发布"))
    if "通报" in text:
        return any(word in text for word in ("据", "称", "显示", "指出", "发布"))
    return True


def _default_rollback(issue: dict[str, Any]) -> str:
    """D08：把问题落到上游节点。程序判的问题不回流，只有语义问题才指回上游。"""
    explicit = str(issue.get("rollback_to") or issue.get("rollback_node") or "")
    if explicit:
        return explicit
    dim = str(issue.get("primary_dimension") or "")
    code = str(issue.get("issue_code") or "")
    if dim == "G1" or code.startswith("G1_"):
        return "N05"
    if dim == "G3" or code.startswith("G3_"):
        return "N07"
    if dim in {"Q1", "Q2", "Q3"}:
        return "N09"
    return "R01"


def _mechanical_check(candidate: dict[str, Any], *, allowed_claims: set[str], allowed_highlights: set[str], evidence_ids: set[str], max_chars: int, allowed_units: set[str] | None = None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    errors.extend({"code": "G4_SCHEMA_INVALID", "repairable": True, "reason": error} for error in candidate.get("generation_schema_errors", []))
    title = str(candidate.get("headline") or "")
    if not title:
        errors.append({"code": "G4_EMPTY_TITLE", "repairable": True, "reason": "标题为空"})
    if len(title) > max_chars:
        errors.append({"code": "G4_LENGTH_EXCEEDED", "repairable": True, "reason": f"标题长度 {len(title)} 超过 {max_chars}"})
    if title.rstrip().endswith(("?", "？")):
        errors.append({"code": "G4_ENDING_QUESTION_MARK", "repairable": True, "reason": "标题不能以问号结尾"})
    if not candidate.get("candidate_id") or not candidate.get("claim_ids") or not candidate.get("evidence_ids"):
        errors.append({"code": "G4_REQUIRED_FIELD_MISSING", "repairable": True, "reason": "candidate_id、claim_ids 或 evidence_ids 缺失"})
    if candidate.get("highlight_id") not in allowed_highlights or any(x not in allowed_claims for x in candidate.get("claim_ids", [])):
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": "claim_id 或 highlight_id 不在当前任务范围"})
    unit_id = candidate.get("unit_id")
    if unit_id and allowed_units is not None and unit_id not in allowed_units:
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": "unit_id 不在当前任务范围"})
    invalid_evidence = [x for x in candidate.get("evidence_ids", []) if x not in evidence_ids]
    if invalid_evidence:
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": f"无效 evidence_id: {invalid_evidence}"})
    warnings: list[dict[str, Any]] = []
    strategy = str(candidate.get("expression_strategy") or "")
    allowed_strategies = set(rubric().get("title_policy", {}).get("strategy_enum", []))
    if strategy and allowed_strategies and strategy not in allowed_strategies:
        warnings.append({"code": "Q2_STRATEGY_MISMATCH", "reason": f"策略 {strategy} 不在固定枚举内"})
    return {"status": "fail" if errors else "pass", "errors": errors, "warnings": warnings, "title_length": len(title), "max_chars": max_chars}


def _decide_candidate(review: dict[str, Any], mechanical: dict[str, Any], *, schema_errors: list[str], business_policy: dict[str, Any]) -> dict[str, Any]:
    gates = dict(review.get("gates")) if isinstance(review.get("gates"), dict) else {}
    issues = list(review.get("issues")) if isinstance(review.get("issues"), list) else []
    if schema_errors:
        gates["G4"] = {"status": "fail", "reason": "评审输出结构校验失败", "errors": schema_errors}
        return {"candidate_id": review.get("candidate_id"), "status": "needs_review", "reason": "REVIEW_SCHEMA_INVALID", "gates": gates, "quality_sum": None, "issues": issues, "repairable": False}
    gates["G4"] = {"status": mechanical["status"], "reason": "程序机械校验", "errors": mechanical["errors"]}
    if business_policy.get("status") != "approved" or not business_policy.get("rules"):
        gates["G2"] = {"status": "needs_review", "reason": "缺少已批准的业务安全规则", "rule_ids": []}
    scores = [review.get("quality", {}).get(q, {}).get("score") for q in QUALITY_IDS]
    quality_sum = sum(x for x in scores if isinstance(x, int)) if all(isinstance(x, int) for x in scores) else None
    quality_min = int(rubric().get("thresholds", {}).get("quality_min_each", 2))
    statuses = {key: str((gates.get(key) or {}).get("status", "needs_review")) for key in (*GATE_IDS, "G4")}
    all_pass = all(statuses[k] == "pass" for k in statuses)
    if statuses["G2"] == "reject":
        rule_refs = (gates.get("G2") or {}).get("rule_ids", [])
        policy_rule_ids = {str(x.get("rule_id")) for x in business_policy.get("rules", []) if isinstance(x, dict) and x.get("rule_id")}
        status, reason = ("reject", "EXPLICIT_BUSINESS_POLICY_REJECTION") if policy_rule_ids.intersection(rule_refs) else ("needs_review", "REJECT_WITHOUT_POLICY_RULE")
    elif any(status == "needs_review" for status in statuses.values()):
        status, reason = "needs_review", "CORE_FACT_OR_POLICY_UNRESOLVED"
    elif statuses["G4"] == "fail" or any(status == "revise" for status in statuses.values()) or any(isinstance(x, int) and x < quality_min for x in scores):
        status, reason = "rewrite", "REPAIRABLE_GATE_OR_QUALITY_ISSUE"
    elif all_pass and all(isinstance(x, int) and x >= quality_min for x in scores):
        status, reason = "ready_for_human_review", "ALL_GATES_PASS_AND_QUALITY_MEETS_MINIMUM"
    else:
        status, reason = "needs_review", "REVIEW_RESULT_INCOMPLETE"
    return {"candidate_id": review.get("candidate_id"), "status": status, "reason": reason, "gates": gates, "quality_sum": quality_sum, "issues": issues, "repairable": status == "rewrite"}


class Pipeline:
    def __init__(self, dataset: Dataset, storage: Storage, adapter: ModelAdapter, config: RunConfig, review_adapter: ModelAdapter | None = None) -> None:
        self.dataset, self.storage, self.adapter, self.review_adapter, self.config = dataset, storage, adapter, review_adapter or adapter, config
        self.memory = MemoryStore(storage.conn)

    def _call(self, task_id: str, node: str, name: str, user: dict[str, Any], schema: str, adapter: ModelAdapter | None = None) -> dict[str, Any]:
        system = prompt_for(name, schema)
        canonical_request = json.dumps({"system": system, "user": user, "schema": schema}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
        prompt_file = str(Path(__file__).with_name("prompts.py"))
        prompt_constant = {"understand": "UNDERSTAND_PROMPT", "boundary": "BOUNDARY_PROMPT", "highlight": "HIGHLIGHT_PROMPT", "generate": "GENERATION_PROMPT", "rewrite": "REWRITE_PROMPT", "review": "REVIEW_PROMPT"}.get(name, "SHARED_RULES")
        selected_adapter = adapter or self.adapter
        model_name = str(getattr(selected_adapter, "model", self.config.review_model if name == "review" else self.config.generation_model))
        started = time.monotonic()
        self.storage.event(task_id, node, "node_started", {"task": name, "schema": schema, "model": model_name, "request_hash": request_hash})
        self.storage.event(task_id, node, "prompt_snapshot", {"task": name, "schema": schema, "model": model_name, "prompt_file": prompt_file, "prompt_constant": prompt_constant, "prompt_hash": prompt_hash, "request_hash": request_hash, "system_prompt": system, "user_payload": user})
        cached = self.storage.cached_model_reply(task_id, request_hash)
        if cached is not None:
            self.storage.event(task_id, node, "model_cache_hit", {"task": name, "request_hash": request_hash, "prompt_hash": prompt_hash})
            self.storage.event(task_id, node, "node_completed", {"task": name, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "cached": True})
            return cached
        last: ModelError | None = None
        for attempt in range(self.config.schema_retry_limit + 1):
            call_started = time.monotonic()
            try:
                reply = selected_adapter.complete(task=name, system=system, user=user, schema=schema, attempt=attempt)
                if reply.status != "ok" or not isinstance(reply.data, dict): raise ModelError("invalid model response", True)
                self.storage.event(task_id, node, "model_reply", {"task": name, "attempt": attempt, "request_hash": request_hash, "prompt_hash": prompt_hash, "usage": reply.usage, "latency_ms": reply.latency_ms, "data": reply.data})
                self.storage.event(task_id, node, "node_completed", {"task": name, "attempt": attempt, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "model_latency_ms": reply.latency_ms, "cached": False})
                return reply.data
            except ModelError as exc:
                last = exc
                self.storage.event(task_id, node, "model_error", {"task": name, "attempt": attempt, "request_hash": request_hash, "error": str(exc), "retryable": exc.retryable, "duration_ms": int((time.monotonic() - call_started) * 1000)})
                if not exc.retryable: break
        self.storage.event(task_id, node, "node_failed", {"task": name, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "error": str(last or "model call failed")})
        raise last or ModelError("model call failed", False)

    def _business_policy(self) -> dict[str, Any]:
        configured = self.config.extra.get("business_policy", {}) if isinstance(self.config.extra, dict) else {}
        policy = configured if isinstance(configured, dict) else {}
        if policy.get("status") not in {"approved", "missing", "draft"}:
            return {"status": "missing", "version": None, "rules": []}
        return policy or {"status": "missing", "version": None, "rules": []}

    def _manifest(self, task_id: str) -> dict[str, Any]:
        root = Path(__file__).resolve().parents[1]
        source_files = [
            Path(__file__), Path(__file__).with_name("prompts.py"), Path(__file__).with_name("schemas.py"),
            Path(__file__).with_name("config.py"), Path(__file__).with_name("model.py"),
            Path(__file__).with_name("data.py"), Path(__file__).with_name("storage.py"),
            Path(__file__).with_name("memory.py"),
            *(root / "agent_system" / "rules" / f"{name}.py" for name in ("__init__", "loader", "router", "rubric", "models")),
            *(root / "rulepacks" / name for name in ("common.md", "domains.md", "forms.md")),
        ]
        hashes = {}
        for path in source_files:
            if path.is_file():
                hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: ("[REDACTED]" if any(x in str(k).lower() for x in ("key", "token", "secret", "password", "credential")) else redact(v)) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(x) for x in value]
            return value
        return {"run_id": task_id, "database_path": self.storage.path, "python": platform.python_version(), "config": redact(asdict(self.config)), "prompt_pack_version": self.config.prompt_pack_version, "rubric_version": self.config.rubric_version, "source_hashes": hashes}

    def prepare(self, video_id: str, launch_source: str = "cli.run") -> str:
        video = self.dataset.get(video_id)
        task_id = self.storage.create_task(video_id, asdict(self.config))
        manifest = self._manifest(task_id)
        manifest["launch_source"] = launch_source
        state: dict[str, Any] = {"video": to_dict(video), "rewrite_round": 0, "reviews": [], "candidates": [], "business_policy": self._business_policy(), "run_manifest": manifest, "prompt_hashes": {}}
        self.storage.event(task_id, "N00", "run_manifest", state["run_manifest"])
        self.storage.event(task_id, "N00", "input_snapshot", state["video"])
        self.storage.checkpoint(task_id, "prepared", state, "N00", "input", {"video_id": video_id, "evidence_count": len(video.evidence)})
        return task_id

    def run(self, video_id: str, launch_source: str = "cli.run") -> str:
        return self.execute(self.prepare(video_id, launch_source))

    def resume(self, task_id: str) -> str:
        row = self.storage.get_task(task_id)
        if row["status"] != "technical_failed":
            raise ValueError(f"only technical_failed tasks can resume (current status: {row['status']})")
        stored_config = json.loads(row["config_json"] or "{}")
        if stored_config != asdict(self.config):
            raise ValueError("task config differs from current config; refusing a non-reproducible resume")
        state = json.loads(row["state_json"] or "{}")
        original_hashes = (state.get("run_manifest") or {}).get("source_hashes")
        current_hashes = self._manifest(task_id).get("source_hashes")
        if original_hashes and original_hashes != current_hashes:
            raise ValueError("prompt, rule or pipeline files changed since this run; use a new task to preserve reproducibility")
        resume_count = int(state.get("resume_count", 0)) + 1
        self.storage.event(task_id, "N00", "resume_started", {"resume_count": resume_count, "strategy": "replay_with_successful_model_reply_cache"})
        # Rebuild the deterministic pipeline from the saved input snapshot.
        # _call reuses successful outputs by request hash and only calls failed or changed nodes.
        video = self.dataset.get(row["video_id"])
        clean_state = {"video": to_dict(video), "rewrite_round": 0, "reviews": [], "candidates": [], "business_policy": state.get("business_policy", self._business_policy()), "run_manifest": state.get("run_manifest", {}), "prompt_hashes": {}, "resume_count": resume_count}
        self.storage.set_state(task_id, "generating", clean_state)
        return self._execute(task_id, video, clean_state)

    def execute(self, task_id: str) -> str:
        row = self.storage.get_task(task_id)
        video = self.dataset.get(row["video_id"])
        state = json.loads(row["state_json"] or "{}")
        if not state.get("video"):
            state = {"video": to_dict(video), "rewrite_round": 0, "reviews": [], "candidates": [], "business_policy": self._business_policy(), "run_manifest": self._manifest(task_id), "prompt_hashes": {}}
        return self._execute(task_id, video, state)

    def _execute(self, task_id: str, video: Any, state: dict[str, Any]) -> str:  # dsh-skip-func-length
        try:
            self.storage.set_state(task_id, "generating", state)
            understanding_raw = self._call(task_id, "N03", "understand", {
                "video": to_dict(video),
                "source_title": {
                    "text": video.title, "quality": "source",
                    "note": "唯一一手文本。可作改写基线（压缩/规整/去标题党），但不是事实依据；与材料冲突时以材料为准。",
                },
            }, "Understanding")
            understanding = _normalize_understanding(understanding_raw)
            state["understanding"] = to_dict(understanding)
            state["prompt_hashes"]["understand"] = self._last_prompt_hash(task_id, "N03", "understand")
            self.storage.checkpoint(task_id, "generating", state, "N03", "understanding", {"claim_count": len(understanding.claims)})
            rule_bundle = load_rule_bundle(domain=video.domain, form=video.format, attributes=understanding.attributes, version=self.config.prompt_pack_version)
            state["rules"] = rule_bundle.as_context()
            state["rule_bundle_hash"] = hashlib.sha256(json.dumps(state["rules"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self.storage.event(task_id, "N04", "rule_match", rule_bundle.as_context())
            self.storage.checkpoint(task_id, "generating", state, "N04", "rules", {"rule_ids": state["rules"].get("rule_ids", []), "rule_bundle_hash": state["rule_bundle_hash"]})
            boundary_raw = self._call(task_id, "N05", "boundary", {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "rules": [asdict(x) for x in load_rules()], "matched_rules": rule_bundle.as_context()}, "BoundarySet")
            boundary = _normalize_boundary(boundary_raw, {c.claim_id: c.text for c in understanding.claims})
            # Models sometimes format ordinal IDs as c-01/c-02. Reconcile by
            # output order only when the returned ID is not in the upstream set;
            # never silently remap a valid but different ID.
            known_claim_ids = {c.claim_id for c in understanding.claims}
            for i, decision in enumerate(boundary.decisions):
                if decision.claim_id not in known_claim_ids and i < len(understanding.claims):
                    decision.claim_id = understanding.claims[i].claim_id
            claim_ids = {c.claim_id for c in understanding.claims}; errors = _validate_ids([to_dict(d) for d in boundary.decisions], "evidence_refs", {e.evidence_id for e in video.evidence})
            if errors: raise ModelError("boundary invalid: " + "; ".join(errors), False)
            state["boundary"] = to_dict(boundary)
            state["prompt_hashes"]["boundary"] = self._last_prompt_hash(task_id, "N05", "boundary")
            self.storage.checkpoint(task_id, "generating", state, "N05", "boundary", {"decision_count": len(boundary.decisions), "usable_count": sum(x.decision != "excluded" for x in boundary.decisions)})
            allowed = [x for x in boundary.decisions if x.decision != "excluded" and not x.verification_required]
            if not allowed:
                pending = [x.claim_id for x in boundary.decisions if x.verification_required]
                state["verification_tasks"] = [to_dict(x) for x in boundary.decisions if x.verification_required]
                return self._stop(task_id, state, "PENDING_VERIFICATION" if pending else "NO_USABLE_CLAIM")
            fallback_highlights = [Highlight(f"h{i+1}", [d.claim_id], d.supported_statement or "", "由可用陈述覆盖", d.evidence_refs, "main" if i == 0 else "backup", d.supported_statement or "") for i, d in enumerate(allowed[:3])]
            highlight_raw = self._call(task_id, "N07", "highlight", {
                "video": to_dict(video), "claims": to_dict(understanding.claims),
                "semantic_units": to_dict(understanding.semantic_units),
                "unit_decisions": to_dict(boundary.unit_decisions),
                "boundary": to_dict(boundary), "matched_rules": rule_bundle.as_context(),
            }, "HighlightSet")
            if bool(highlight_raw.get("abandon") or highlight_raw.get("no_viable_highlight")):
                state["highlight_selection"] = {"abandoned": True, "reason": str(highlight_raw.get("reason") or ""), "eliminated": highlight_raw.get("eliminated", [])}
                return self._stop(task_id, state, "NO_VIABLE_HIGHLIGHT")
            highlights = _normalize_highlights(highlight_raw, {d.claim_id for d in allowed}, fallback_highlights)
            # 模型未在 highlight 上给 unit_id 时，用其 claim_ids 反查所属语义单元补齐
            claim_to_unit: dict[str, str] = {}
            for u in understanding.semantic_units:
                for cid in u.claim_ids:
                    claim_to_unit.setdefault(cid, u.unit_id)
            for h in highlights:
                if not h.unit_id:
                    for cid in h.claim_ids:
                        if cid in claim_to_unit:
                            h.unit_id = claim_to_unit[cid]
                            break
            if not highlights:
                return self._stop(task_id, state, "NO_HIGHLIGHT")
            state["highlight_selection"] = {"abandoned": False, "eliminated": highlight_raw.get("eliminated", []) if isinstance(highlight_raw.get("eliminated"), list) else []}
            state["highlights"] = to_dict(highlights)
            state["prompt_hashes"]["highlight"] = self._last_prompt_hash(task_id, "N07", "highlight")
            self.storage.checkpoint(task_id, "generating", state, "N07", "highlights", {"highlight_count": len(highlights)})
            candidates = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, 0)
            if candidates["route"]["action"] == "rewrite":
                state["reviews"] = candidates["reviews"]
                for round_no in range(1, self.config.max_rewrite_rounds + 1):
                    state["rewrite_round"] = round_no
                    targets = candidates["route"].get("rewrite_targets") or [
                        {"candidate_id": candidates["route"].get("rewrite_candidate_id"), "mode": candidates["route"].get("rewrite_mode", "表达")}
                    ]
                    round_decisions: list[dict[str, Any]] = []
                    rewritten_ids: list[str] = []
                    for target in targets:
                        source = next((c for c in candidates["candidates"] if c["candidate_id"] == target.get("candidate_id")), None)
                        if source is None:
                            continue
                        rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, round_no, source)
                        candidates["candidates"] += rewritten["candidates"]
                        candidates["reviews"] += rewritten["reviews"]
                        candidates["decisions"] += rewritten["decisions"]
                        round_decisions += rewritten["decisions"]
                        rewritten_ids += [c.get("candidate_id") for c in rewritten["candidates"]]
                    state["reviews"] = candidates["reviews"]
                    if not round_decisions:
                        candidates["route"] = {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "REWRITE_PRODUCED_NO_CANDIDATE", "rewrite_round": round_no}
                        break
                    candidates["route"] = self._route_candidates(round_decisions, round_no)
                    if candidates["route"]["action"] != "rewrite":
                        break
                if candidates["route"]["action"] == "rewrite":
                    candidates["route"] = {**candidates["route"], "action": "stop", "rewrite_candidate_id": None, "rewrite_targets": [], "reason": "REWRITE_BUDGET_EXHAUSTED"}
            state["candidates"], state["reviews"], state["candidate_decisions"], state["route"] = candidates["candidates"], candidates["reviews"], candidates["decisions"], candidates["route"]
            self.storage.save_candidates(task_id, candidates["candidates"]); self.storage.save_reviews(task_id, candidates["reviews"])
            return self._handoff(task_id, state, candidates["route"])
        except Exception as exc:
            state["error"] = str(exc)
            retryable = bool(getattr(exc, "retryable", False))
            state["failure_retryable"] = retryable
            state["failure_checkpoint"] = self._latest_checkpoint(task_id)
            self.storage.set_state(task_id, "technical_failed", state)
            self.storage.event(task_id, "X01", "technical_failure", {"error": str(exc), "retryable": retryable, "resume_available": True, "checkpoint": state["failure_checkpoint"]})
            return task_id

    def _last_prompt_hash(self, task_id: str, node: str, name: str) -> str | None:
        for event in reversed(self.storage.events(task_id)):
            if event["node"] == node and event["event_type"] == "prompt_snapshot" and event["payload"].get("task") == name:
                return event["payload"].get("prompt_hash")
        return None

    def _latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        for event in reversed(self.storage.events(task_id)):
            if event["event_type"] == "checkpoint_saved":
                return {"node": event["node"], **event["payload"]}
        return None

    def rereview(self, task_id: str) -> str:
        """Re-run the independent reviewer against the stored candidate snapshot."""
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        if not state.get("candidates"):
            raise ValueError("task has no stored candidates")
        video = self.dataset.get(row["video_id"])
        checks = [{"candidate_id": c.get("candidate_id"), **c.get("mechanical_checks", {"status": "fail", "errors": [{"code": "G4_CHECK_MISSING", "repairable": False}]}), "programmatic_g1_flags": c.get("programmatic_g1_flags", [])} for c in state["candidates"]]
        review_raw = self._call(task_id, "R01", "review", self._review_input(video, state, state["candidates"], checks), "ReviewResultBatch", self.review_adapter)
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in state["candidates"]])
        reviews, decisions = self._validate_and_decide_reviews(video, state, state["candidates"], reviews, checks)
        route = self._route_candidates(decisions, state.get("rewrite_round", 0))
        state["reviews"], state["route"] = reviews, route
        state["candidate_decisions"] = decisions
        self.storage.save_reviews(task_id, reviews)
        self.storage.set_state(task_id, "pending_human" if route["action"] in {"handoff", "needs_review"} else "stopped", state)
        self.storage.event(task_id, "R01", "rereview_complete", {"route": route})
        return task_id

    def _review_input(self, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], mechanical_checks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "video_materials": {"title": video.title, "description": video.description, "caption": video.caption, "asr_segments": video.asr_segments, "ocr_segments": video.ocr_segments},
            "evidence": to_dict(video.evidence),
            "claims": state.get("understanding", {}).get("claims", []),
            "boundary": state.get("boundary", {}),
            "highlights": state.get("highlights", []),
            "matched_rules": state.get("rules", {}),
            "business_policy": state.get("business_policy", {"status": "missing", "rules": [], "version": None}),
            "display_config": {"max_chars": self.config.max_chars, "ending_question_mark_forbidden": True},
            "mechanical_checks": mechanical_checks,
            "candidate_titles": [{"candidate_id": c.get("candidate_id"), "title": c.get("headline"), "claim_ids": c.get("claim_ids", []), "highlight_id": c.get("highlight_id"), "evidence_ids": c.get("evidence_ids", []), "strategy": c.get("expression_strategy"), "qualifiers_used": c.get("qualifiers_used", [])} for c in candidates],
            "rubric": rubric(),
        }

    def _validate_and_decide_reviews(self, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], reviews: list[dict[str, Any]], mechanical_checks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        evidence_ids = {e.evidence_id for e in video.evidence}
        rule_ids = set(rubric().get("rule_ids", []))
        policy_rule_ids = {str(x.get("rule_id")) for x in state.get("business_policy", {}).get("rules", []) if isinstance(x, dict) and x.get("rule_id")}
        rule_ids.update(load_common_rule_ids())
        rule_ids.update(policy_rule_ids)
        for group in (state.get("rules", {}).get("domain_rules", []), state.get("rules", {}).get("form_rules", [])):
            rule_ids.update(str(x.get("rule_id")) for x in group if x.get("rule_id"))
        checks_by_id = {x.get("candidate_id"): x for x in mechanical_checks}
        reviews_by_id = {x.get("candidate_id"): x for x in reviews}
        normalized_reviews, decisions = [], []
        for candidate in candidates:
            cid = candidate.get("candidate_id")
            review = reviews_by_id.get(cid, {"candidate_id": cid})
            mechanical = checks_by_id.get(cid, candidate.get("mechanical_checks", {"status": "fail", "errors": [{"code": "G4_CHECK_MISSING", "repairable": False}]}))
            mechanical.setdefault("programmatic_g1_flags", candidate.get("programmatic_g1_flags", []))
            normalization_errors = review.setdefault("_normalization_errors", [])
            if not isinstance(review.get("issues"), list):
                normalization_errors.append("issues must be a list")
                review["issues"] = []
            if not isinstance(review.get("rewrite_guidance"), dict):
                normalization_errors.append("rewrite_guidance must be an object")
                review["rewrite_guidance"] = {"preserve": [], "modify": [], "forbidden": []}
            if not isinstance(review.get("gates"), dict):
                normalization_errors.append("gates must be an object")
                review["gates"] = {}
            programmatic_g1_flags = mechanical.get("programmatic_g1_flags", [])
            if any(x.get("code") == "G1_SOURCE_ATTRIBUTION_MISSING" for x in programmatic_g1_flags):
                review["gates"]["G1"] = {"status": "revise", "reason": "可识别信源缺少明确转述关系", "evidence_ids": [], "rule_ids": []}
            validation_errors = validate_review(review, evidence_ids=evidence_ids, rule_ids=rule_ids, issue_codes=set(rubric().get("issue_codes", [])), policy_rule_ids=policy_rule_ids)
            if any(x.get("code") == "G4_CHECK_MISSING" for x in mechanical.get("errors", [])):
                validation_errors.append("programmatic G4 check missing")
            for mechanical_error in mechanical.get("errors", []):
                issue = {
                    "issue_id": f"{cid}-{mechanical_error.get('code')}",
                    "primary_dimension": "G1" if str(mechanical_error.get("code", "")).startswith("G1_") else "G4",
                    "issue_code": mechanical_error.get("code") if mechanical_error.get("code") in rubric().get("issue_codes", []) else "G4_INVALID_REFERENCE",
                    "severity": "high",
                    "repairable": bool(mechanical_error.get("repairable")),
                    "problem_span": candidate.get("headline", ""),
                    "evidence_ids": [], "rule_ids": [], "reason": mechanical_error.get("reason", "程序机械校验失败"),
                    "action": "修正标题或引用后重新完整评审",
                }
                if not any(i.get("issue_code") == issue["issue_code"] for i in review.get("issues", []) if isinstance(i, dict)):
                    review.setdefault("issues", []).append(issue)
            if mechanical.get("errors"):
                guidance = review.setdefault("rewrite_guidance", {"preserve": [], "modify": [], "forbidden": []})
                guidance.setdefault("modify", []).extend(x.get("reason", "修复程序硬约束") for x in mechanical.get("errors", []))
            for flag in programmatic_g1_flags:
                issue = {"issue_id": f"{cid}-{flag.get('code')}", "primary_dimension": "G1", "issue_code": flag.get("code"), "severity": "high", "repairable": bool(flag.get("repairable")), "problem_span": candidate.get("headline", ""), "evidence_ids": [], "rule_ids": [], "reason": flag.get("reason", "G1 程序规则校验失败"), "action": "补充明确的信源转述关系后重新评审"}
                if not any(i.get("issue_code") == issue["issue_code"] for i in review.get("issues", []) if isinstance(i, dict)):
                    review.setdefault("issues", []).append(issue)
            decision = _decide_candidate(review, mechanical, schema_errors=validation_errors, business_policy=state.get("business_policy", {"status": "missing"}))
            review["mechanical_checks"] = mechanical
            review["validation_errors"] = validation_errors
            review["decision"] = decision["status"]
            review["quality_sum"] = decision["quality_sum"]
            review["gates"] = decision["gates"]
            normalized_reviews.append(review)
            decisions.append(decision)
        return normalized_reviews, decisions

    def _route_candidates(self, decisions: list[dict[str, Any]], rewrite_round: int) -> dict[str, Any]:
        ranked = sorted((d for d in decisions if d["status"] == "ready_for_human_review"), key=lambda d: d.get("quality_sum") or 0, reverse=True)
        rewrites = [d for d in decisions if d["status"] == "rewrite"]
        uncertain = [d for d in decisions if d["status"] == "needs_review"]
        rejected = [d for d in decisions if d["status"] == "reject"]
        if ranked:
            return {"action": "handoff", "recommended_candidate_id": ranked[0]["candidate_id"], "eligible_candidate_ids": [d["candidate_id"] for d in ranked], "rewrite_candidate_id": None, "reason": "eligible_candidates_ranked_by_Q1_Q2_Q3", "rewrite_round": rewrite_round}
        if uncertain:
            return {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "core_fact_or_business_policy_requires_human_verification", "candidate_ids": [d["candidate_id"] for d in uncertain], "rewrite_round": rewrite_round}
        if rewrites:
            ranked = sorted(rewrites, key=lambda d: (d.get("quality_sum") or 0, -len(d.get("issues", []))), reverse=True)
            limit = max(1, int(getattr(self.config, "max_rewrite_candidates", 3)))
            targets = []
            for item in ranked[:limit]:
                issues = item.get("issues", []) or []
                mode = "看点" if any(str(i.get("issue_code", "")).startswith("G3_") or i.get("rollback_to") == "N07" for i in issues) else "表达"
                targets.append({"candidate_id": item["candidate_id"], "mode": mode})
            return {"action": "rewrite", "recommended_candidate_id": None, "eligible_candidate_ids": [],
                    "rewrite_candidate_id": targets[0]["candidate_id"], "rewrite_targets": targets,
                    "reason": "repairable_gate_or_quality_issues", "rewrite_round": rewrite_round}
        return {"action": "reject" if rejected else "stop", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "no_eligible_or_repairable_candidate", "rewrite_round": rewrite_round}

    def _generate_and_review(self, task_id: str, state: dict[str, Any], video: Any, understanding: Understanding, boundary: BoundarySet, highlights: list[Highlight], rewrite_round: int, source: dict[str, Any] | None = None) -> dict[str, Any]:
        source_requirements = [to_dict(decision) for decision in boundary.decisions if decision.decision != "excluded" and (decision.attribution_required or decision.required_source_phrase or decision.required_limits or decision.forbidden_inferences)]
        blocked_claims = [
            {"claim_id": decision.claim_id, "forbidden_inferences": decision.forbidden_inferences,
             "reason": "暂缓核验" if decision.verification_required else "已排除", "unresolved": decision.verification_required}
            for decision in boundary.decisions
            if decision.decision == "excluded" or decision.verification_required
        ]
        user = {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "unit_decisions": to_dict(boundary.unit_decisions), "boundary": to_dict(boundary), "source_requirements": source_requirements, "blocked_claims": blocked_claims, "unresolved_issues": boundary.unresolved_issues, "highlights": to_dict(highlights), "matched_rules": state.get("rules", {}), "display_config": {"max_chars": self.config.max_chars, "ending_question_mark_forbidden": True}, "strategy_enum": rubric().get("title_policy", {}).get("strategy_enum", []), "target_count": self.config.rewrite_candidate_count if source else self.config.target_count, "rewrite_round": rewrite_round}
        parent_review = next((r for r in state.get("reviews", []) if r.get("candidate_id") == (source or {}).get("candidate_id")), {})
        rewrite_mode = "表达"
        if source:
            parent_issues = parent_review.get("issues", []) or []
            rewrite_mode = "看点" if any(str(i.get("issue_code", "")).startswith("G3_") or i.get("rollback_to") == "N07" for i in parent_issues) else "表达"
            user.update({
                "parent_candidate_id": source["candidate_id"], "parent_headline": source.get("headline"),
                "parent_highlight_id": source.get("highlight_id"), "review_issues": parent_issues,
                "rewrite_guidance": parent_review.get("rewrite_guidance", {}),
                "rewrite_mode": rewrite_mode, "allow_highlight_change": rewrite_mode == "看点",
            })
        memory_calls = int(state.get("memory_calls", 0))
        if memory_calls < self.config.memory_call_limit:
            memory_query = parent_review.get("memory_query") or {}
            query = str(memory_query.get("query") or " ".join(i.get("issue_code", "") for i in parent_review.get("issues", []))) if source else video.title
            problem_types = [str(x) for x in memory_query.get("problem_types", [])] if source else ["表达", "事实与信源"]
            query = query.strip() or video.title
            scope = [str(x) for x in (video.domain or {}).values()] + [str(video.format)]
            # 案例按「规则包版本」入库，检索也必须用同一个版本号；
            # 用 config.rule_version（临时规则版本 temporary-v1）查会永远 0 命中。
            cases = self.memory.search(query, problem_types, self.config.prompt_pack_version, limit=3, scope=scope)
            state["memory_calls"] = memory_calls + 1
            state.setdefault("memory_trace", []).append({"query": query[:200], "problem_types": problem_types, "status": "ok" if cases else "empty", "used_case_ids": [c.get("case_id") for c in cases], "fallback": "formal_rules_only" if not cases else None})
            user["memory_cases"] = cases
        generation_task = "rewrite" if source else "generate"
        draft = self._call(task_id, "N09", generation_task, user, "AgentDraft")
        state["prompt_hashes"][generation_task] = self._last_prompt_hash(task_id, "N09", generation_task)
        candidates = _normalize_candidates(draft, rewrite_round, source["candidate_id"] if source else None)
        if not candidates:
            route = {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "GENERATION_SCHEMA_INVALID_OR_NO_CANDIDATES", "rewrite_round": rewrite_round}
            self.storage.event(task_id, "Q01", "quality_route", route)
            return {"candidates": [], "reviews": [], "decisions": [], "route": route}
        seen_candidate_ids: set[str] = set()
        for candidate in candidates:
            cid = candidate.get("candidate_id", "")
            if cid in seen_candidate_ids:
                candidate.setdefault("generation_schema_errors", []).append(f"duplicate candidate_id: {cid}")
            seen_candidate_ids.add(cid)
        allowed_claims = {d.claim_id for d in boundary.decisions if d.decision != "excluded"}; allowed_highlights = {h.highlight_id for h in highlights}
        allowed_units = {d.unit_id for d in boundary.unit_decisions if d.decision != "excluded"}
        if not allowed_units:
            allowed_units = {u.unit_id for u in understanding.semantic_units}
        boundary_by_claim = {d.claim_id: d for d in boundary.decisions}
        for c in candidates:
            c["mechanical_checks"] = _mechanical_check(c, allowed_claims=allowed_claims, allowed_highlights=allowed_highlights, evidence_ids={e.evidence_id for e in video.evidence}, max_chars=self.config.max_chars, allowed_units=allowed_units)
            c["programmatic_g1_flags"] = []
            source_required = any(
                boundary_by_claim.get(claim_id)
                and boundary_by_claim[claim_id].attribution_required
                for claim_id in c.get("claim_ids", [])
            )
            if source_required and not _has_explicit_source_relation(c.get("headline", "")):
                c["programmatic_g1_flags"].append({"code": "G1_SOURCE_ATTRIBUTION_MISSING", "repairable": True, "reason": "本条边界明确要求来源归属，但标题未表达转述关系"})
        state["pending_review_candidates"] = candidates
        self.storage.save_candidates(task_id, candidates)
        self.storage.checkpoint(task_id, "reviewing", state, "N09", "candidates_generated", {"candidate_ids": [c.get("candidate_id") for c in candidates], "candidate_count": len(candidates), "generation_round": rewrite_round})
        self.storage.set_state(task_id, "reviewing", state)
        checks = [{"candidate_id": c["candidate_id"], **c["mechanical_checks"], "programmatic_g1_flags": c["programmatic_g1_flags"]} for c in candidates]
        review_raw = self._call(task_id, "R01", "review", self._review_input(video, state, candidates, checks), "ReviewResultBatch", self.review_adapter)
        state["prompt_hashes"]["review"] = self._last_prompt_hash(task_id, "R01", "review")
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in candidates])
        reviews, decisions = self._validate_and_decide_reviews(video, state, candidates, reviews, checks)
        route = self._route_candidates(decisions, rewrite_round)
        if source:
            parent_codes = [str(i.get("issue_code")) for i in (parent_review.get("issues") or []) if i.get("issue_code")]
            child_codes: list[str] = []
            for review in reviews:
                child_codes.extend(str(i.get("issue_code")) for i in (review.get("issues") or []) if i.get("issue_code"))
            state.setdefault("rewrite_outcomes", []).append({
                "parent_candidate_id": source["candidate_id"],
                "child_candidate_ids": [c.get("candidate_id") for c in candidates],
                "mode": rewrite_mode,
                "parent_issue_codes": parent_codes,
                "child_issue_codes": child_codes,
                "resolved": [code for code in parent_codes if code not in child_codes],
                "repeated": [code for code in parent_codes if code in child_codes],
                "addressed_reported": [code for c in candidates for code in (c.get("addressed_issue_ids") or [])],
            })
        state["pending_review_candidates"] = []
        self.storage.checkpoint(task_id, "reviewing", state, "R01", "reviews_validated", {"candidate_ids": [r.get("candidate_id") for r in reviews], "decision_count": len(decisions), "validation_error_count": sum(len(r.get("validation_errors", [])) for r in reviews)})
        self.storage.event(task_id, "Q01", "quality_route", route)
        self.storage.checkpoint(task_id, "reviewing", state, "Q01", "quality_route", {"action": route.get("action"), "reason": route.get("reason"), "candidate_ids": route.get("candidate_ids", route.get("eligible_candidate_ids", []))})
        return {"candidates": candidates, "reviews": reviews, "decisions": decisions, "route": route}

    def _handoff(self, task_id: str, state: dict[str, Any], route: dict[str, Any]) -> str:
        status = "pending_human" if route["action"] in {"handoff", "needs_review"} else "stopped"
        state["uncalibrated"] = True
        self.storage.checkpoint(task_id, status, state, "N12", "human_handoff", {"status": status, "route": route})
        self.storage.event(task_id, "N12", "handoff", {"status": status, "route": route})
        return task_id

    def _stop(self, task_id: str, state: dict[str, Any], reason: str) -> str:
        state["stop_reason"] = reason; self.storage.set_state(task_id, "stopped", state); self.storage.event(task_id, "X02", "business_stop", {"reason": reason}); return task_id

