from __future__ import annotations

import json
import hashlib
import copy
import shutil
import tempfile
import re
import platform
import time
from urllib.parse import urlsplit
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import RunConfig
from .data import Dataset, assess_material
from .labels import MIN_BODY_BUDGET, body_budget, extract_inline_prefixes, normalize_prefix_labels, render_labels, render_title, required_prefixes, route_identity, self_declared_prefixes
from .memory import MemoryStore
from .signals import extract_risk_signals, check_signal_constraints, unsupported_numeric_facts, signal_review_reasons
from .model import ModelAdapter, ModelError
from .rules import load_common_rule_ids, load_rule_bundle, load_rules, prompt_text_from_context, rubric
from .prompts import prompt_for
from .schemas import GATE_IDS, QUALITY_IDS, validate_review
from .storage import Storage
from .types import BoundarySet, Claim, Evidence, EventNode, EventTurn, Highlight, SemanticUnit, Understanding, UnitDecision, VideoInput, to_dict
from .abuse import attack_rewrite_hint, find_personal_attacks
from .thesis import contracts_disagree, normalize_thesis_contract, validate_highlight_alignment, SLOTS
from .evidence_audit import audit_evidence
from .prompts import RECONCILE_SYSTEM, VERIFY_PROMPT
from .verification import build_verification_query, create_search_provider
from .verification.judge import compute_risk, parse_verification
from .verification.router import extract_verification_suggestion, normalize_verification_mode, pick_core_claims, resolve_verification, whitelist_for


def _freeze_rereview_candidates(candidates: Any) -> list[dict[str, str | None]]:
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("缺少候选快照")
    frozen, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("候选快照无效")
        cid, headline = candidate.get("candidate_id"), candidate.get("headline")
        rendered = candidate.get("rendered_title")
        if not isinstance(cid, str) or not cid or cid in seen or not isinstance(headline, str) or not headline:
            raise ValueError("候选身份或标题无效")
        if rendered is not None and (not isinstance(rendered, str) or not rendered):
            raise ValueError("候选完整标题无效")
        seen.add(cid)
        # 正文是冻结身份；完整标题只在快照里本来就有时一起冻结。
        # 机械检查会在重评中重算 rendered_title，把「原本没有」当成有值去比会误报标题被改。
        frozen.append({
            "candidate_id": cid,
            "headline_sha256": hashlib.sha256(headline.encode()).hexdigest(),
            "title_sha256": hashlib.sha256(rendered.encode()).hexdigest() if rendered else None,
        })
    return frozen


def _rereview_identity_drift(candidates: Any, frozen: list[dict[str, Any]]) -> list[str]:
    """比对候选是否仍与冻结快照同身份、同标题；返回差异说明，空表示一致。"""
    if not isinstance(candidates, list):
        return ["候选快照缺失"]
    current = {c.get("candidate_id"): c for c in candidates if isinstance(c, dict)}
    drift: list[str] = []
    for item in frozen:
        cid = str(item.get("candidate_id"))
        candidate = current.get(cid)
        if candidate is None:
            drift.append(f"{cid}: 候选缺失")
            continue
        headline = candidate.get("headline") or ""
        if hashlib.sha256(str(headline).encode()).hexdigest() != item.get("headline_sha256"):
            drift.append(f"{cid}: 正文被改写")
        if item.get("title_sha256"):
            rendered = candidate.get("rendered_title") or headline
            if hashlib.sha256(str(rendered).encode()).hexdigest() != item.get("title_sha256"):
                drift.append(f"{cid}: 完整标题被改写")
    extra = sorted(set(current) - {str(item.get("candidate_id")) for item in frozen})
    if extra:
        drift.append(f"新增候选：{'、'.join(extra)}")
    return drift


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


def _norm_claim_ref(raw_id: str) -> str:
    """把 c-1 / c01 这类模型变体统一成 c1，与 claims 的 claim_id 对齐。"""
    text = str(raw_id)
    if text.startswith("c-") and text[2:].isdigit():
        return "c" + str(int(text[2:]))
    return text


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


def _claims_nested_in_units(units: Any) -> list[dict[str, Any]]:
    """从语义单元内部收集被嵌套的 claim 对象。

    真实故障：理解提示词把 candidate_claims 描述成「语义单元内的原子事实层」，
    模型于是只在单元里写 claim_ids、把 claim 对象整批省略，顶层没有任何 claim 数组。
    解析端只认顶层 → claims 为 0 → 边界节点拿到空集合后自行编造 claim_id →
    引用审计判全部悬空 → 整条视频以「引用不全」的误导理由归零。
    这里把嵌在单元里的 claim 对象提上来，能救回这类输出。
    """
    if not isinstance(units, list):
        return []
    lifted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            continue
        for key in ("candidate_claims", "claims", "facts"):
            items = unit.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_id = str(item.get("claim_id") or item.get("fact_id") or "")
                if raw_id and raw_id in seen:
                    continue
                if raw_id:
                    seen.add(raw_id)
                lifted.append(item)
    return lifted


def _unit_claim_refs(units: Any) -> set[str]:
    """语义单元声明的 claim_ids（已归一化），用于检查引用是否都有定义。"""
    refs: set[str] = set()
    if not isinstance(units, list):
        return refs
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ids = unit.get("claim_ids") or unit.get("claims_ids") or []
        if isinstance(ids, list):
            refs.update(_norm_claim_ref(str(x)) for x in ids if str(x).strip())
    return refs


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
    if not raw_claims:
        # 最后一道：claim 对象被嵌在语义单元里（见 _claims_nested_in_units 的故障说明）。
        units_for_lift = raw.get("semantic_units") or raw.get("semanticUnits") or []
        if not isinstance(units_for_lift, list) or not units_for_lift:
            for wrapper in ("Understanding", "understanding", "result", "output", "data"):
                inner = raw.get(wrapper)
                if isinstance(inner, dict):
                    nested = inner.get("semantic_units") or inner.get("semanticUnits")
                    if isinstance(nested, list) and nested:
                        units_for_lift = nested
                        break
        raw_claims = _claims_nested_in_units(units_for_lift)
    claims = []
    for i, item in enumerate(raw_claims):
        item = dict(item)
        refs = item.get("evidence_refs") or item.get("evidence_ids") or []
        raw_claim_id = str(item.get("claim_id") or item.get("fact_id") or f"c{i+1}")
        claim_id = _norm_claim_ref(raw_claim_id)
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
            verification_query=str(item.get("verification_query") or item.get("verify_query") or item.get("search_query") or "").strip(),
        ))
    attrs = raw.get("attributes", [])
    if isinstance(attrs, dict):
        attrs = [f"{k}:{v}" for k, v in attrs.items()]
    event_times = raw.get("event_times") or raw.get("event_time") or []
    if isinstance(event_times, dict): event_times = [event_times]
    units_raw = raw.get("semantic_units") or raw.get("semanticUnits") or []
    # 模型常把 semantic_units 放在 Understanding/understanding 包装里，而 candidate_claims 在顶层；
    # 只在顶层找会漏掉真单元并误走「每 claim 一个单元」的降级路径（thesis/role 全丢）。
    if not isinstance(units_raw, list) or not units_raw:
        for wrapper in ("Understanding", "understanding", "result", "output", "data"):
            inner = raw.get(wrapper)
            if isinstance(inner, dict):
                nested_units = inner.get("semantic_units") or inner.get("semanticUnits")
                if isinstance(nested_units, list) and nested_units:
                    units_raw = nested_units
                    break
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
        grounding = str(item.get("grounding") or "unknown")
        if grounding not in {"grounded", "inferred", "speculative", "unknown"}:
            grounding = "unknown"
        semantic_use = str(item.get("semantic_use") or "unknown")
        if semantic_use not in {"content_summary", "source_opinion", "observable_fact", "unsupported_inference", "unknown"}:
            semantic_use = "unknown"
        semantic_units.append(SemanticUnit(
            unit_id=str(item.get("unit_id") or f"u{i+1}"),
            meaning=str(item.get("meaning") or item.get("content") or item.get("thesis") or ""),
            claim_ids=[_norm_claim_ref(str(x)) for x in (item.get("claim_ids") or item.get("claims") or [])],
            evidence_ids=[str(x) for x in (item.get("evidence_ids") or item.get("evidence_refs") or [])],
            kind=str(item.get("kind") or "support"),
            time_range=tr,
            role_in_video=str(item.get("role_in_video") or item.get("role") or ""),
            grounding=grounding,
            semantic_use=semantic_use,
            corroboration=item.get("corroboration") if isinstance(item.get("corroboration"), dict) else {},
        ))
    # 事件时间线：和 semantic_units 一样要能在 Understanding 包装里找到。
    # 模型这次就把 events/turns 放进了内层，只读顶层会以为它没输出。
    events_raw = raw.get("events") or raw.get("event_timeline") or []
    turns_raw = raw.get("turns") or raw.get("event_turns") or []
    for wrapper in ("Understanding", "understanding", "result", "output", "data"):
        inner = raw.get(wrapper)
        if not isinstance(inner, dict):
            continue
        if not isinstance(events_raw, list) or not events_raw:
            events_raw = inner.get("events") or inner.get("event_timeline") or []
        if not isinstance(turns_raw, list) or not turns_raw:
            turns_raw = inner.get("turns") or inner.get("event_turns") or []
    events: list[EventNode] = []
    for i, item in enumerate(events_raw if isinstance(events_raw, list) else []):
        if not isinstance(item, dict):
            continue
        events.append(EventNode(
            event_id=str(item.get("event_id") or f"e{i+1}"),
            actor=str(item.get("actor") or item.get("who") or ""),
            action=str(item.get("action") or item.get("what") or item.get("text") or ""),
            when=str(item.get("when") or item.get("stage") or "现场"),
            evidence_ids=[str(x) for x in (item.get("evidence_ids") or item.get("evidence_refs") or [])],
            confidence=str(item.get("confidence") or item.get("grounding") or "inferred"),
        ))
    turns: list[EventTurn] = []
    for i, item in enumerate(turns_raw if isinstance(turns_raw, list) else []):
        if not isinstance(item, dict):
            continue
        turn_type = str(item.get("type") or "sequence")
        expectation = str(item.get("expectation") or item.get("expected") or "").strip()
        # 说不出「按常理本该发生什么」的，不算反差：那是顺承，不是预期违背
        if turn_type == "contrast" and not expectation:
            turn_type = "sequence"
        turns.append(EventTurn(
            # 模型常用 from_event_id/to_event_id 命名；只认 from_event 会把端点读成空串，
            # 引用审计随即判 empty_ref，整条视频零产出。
            from_event=str(item.get("from_event") or item.get("from_event_id") or item.get("from") or ""),
            to_event=str(item.get("to_event") or item.get("to_event_id") or item.get("to") or ""),
            type=turn_type,
            note=str(item.get("note") or item.get("notes") or ""),
            expectation=expectation,
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
        conflicts=[dict(x) if isinstance(x, dict) else {"conflict_id": f"conflict-{i+1}", "description": str(x), "severity": "high", "evidence_ids": sorted({ref for c in claims for ref in c.evidence_refs}), "normalization_note": "非结构化冲突保守覆盖全部引用，需语义复核"} for i, x in enumerate(raw.get("conflicts", []) if isinstance(raw.get("conflicts", []), list) else [])],
        semantic_units=semantic_units,
        events=events,
        turns=turns,
    )


TEXT_PRIMARY_QUALITIES = {"raw_asr", "raw_ocr"}


def _demote_claims_without_refs(boundary: BoundarySet, issues: list[dict[str, Any]]) -> list[str]:
    """把引用不全的陈述降级为 excluded，返回被降级的 claim_id。

    缺证据就不该写：这是逐条收紧，而不是让整条任务产出为零。已 excluded 的不重复处理。
    """
    demoted: list[str] = []
    for issue in issues:
        m = re.match(r"boundary\.decisions\[(\d+)\]", str(issue.get("path") or ""))
        if not m:
            continue
        idx = int(m.group(1))
        if not (0 <= idx < len(boundary.decisions)):
            continue
        decision = boundary.decisions[idx]
        if decision.decision == "excluded":
            continue
        decision.decision = "excluded"
        decision.reason = (decision.reason + "；" if decision.reason else "") + "引用不全，按不可写处理"
        demoted.append(decision.claim_id)
    return demoted


def _prune_dangling_unit_refs(understanding: Understanding, boundary: BoundarySet, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """语义单元引用了不存在的 claim 时，剪掉悬空引用；剪空的单元判 excluded。

    悬空引用只污染个别单元，不该让整条视频零产出——那等于用一处笔误换掉全部候选。
    真实出现过：模型给了 18 条 claim，单元却引用到 c22，整任务被判 REFERENCE_AUDIT_INVALID。
    """
    valid = {c.claim_id for c in understanding.claims}
    targets: set[int] = set()
    for issue in issues or []:
        if str(issue.get("code") or "") != "dangling_ref":
            continue
        match = re.search(r"semantic_units\[(\d+)\]", str(issue.get("path") or ""))
        if match:
            targets.add(int(match.group(1)))
    by_unit = {unit.unit_id: unit for unit in boundary.unit_decisions}
    pruned: list[dict[str, Any]] = []
    for index in sorted(targets):
        if not (0 <= index < len(understanding.semantic_units)):
            continue
        unit = understanding.semantic_units[index]
        dropped = [str(c) for c in (unit.claim_ids or []) if str(c) not in valid]
        if not dropped:
            continue
        unit.claim_ids = [str(c) for c in unit.claim_ids if str(c) in valid]
        emptied = not unit.claim_ids and not (unit.evidence_ids or [])
        if emptied:
            decision = by_unit.get(unit.unit_id)
            reason = "引用的陈述不存在，按不可写处理"
            if decision is not None:
                decision.decision = "excluded"
                decision.reason = (decision.reason + "；" if decision.reason else "") + reason
            else:
                boundary.unit_decisions.append(UnitDecision(unit_id=unit.unit_id, decision="excluded", reason=reason))
        pruned.append({"unit_id": unit.unit_id, "dropped": dropped, "excluded": emptied})
    return pruned


def _prune_invalid_turns(understanding: Understanding, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删掉端点缺失或悬空的 turn，而不是让整条视频零产出。

    turns 是从事件派生的结构注解，不是一手证据；一条 turn 端点写坏，不该换掉全部候选。
    真实出现过：模型用 from_event_id/to_event_id 输出，解析器只认 from_event，端点全空，
    整条视频被判 REFERENCE_AUDIT_INVALID。
    """
    targets: set[int] = set()
    for issue in issues or []:
        match = re.search(r"understanding\.turns\[(\d+)\]", str(issue.get("path") or ""))
        if match:
            targets.add(int(match.group(1)))
    valid = {event.event_id for event in understanding.events}
    dropped: list[dict[str, Any]] = []
    kept: list[Any] = []
    for index, turn in enumerate(understanding.turns):
        unusable = (
            index in targets
            or not turn.from_event
            or not turn.to_event
            or turn.from_event not in valid
            or turn.to_event not in valid
        )
        if unusable:
            dropped.append({"index": index, "from": turn.from_event, "to": turn.to_event, "type": turn.type})
            continue
        kept.append(turn)
    if dropped:
        understanding.turns = kept
    return dropped


def _exclude_units_with_empty_refs(understanding: Understanding, boundary: BoundarySet, issues: list[dict[str, Any]]) -> list[str]:
    """语义单元连引用都空了的，判该单元 excluded，而不是让整条视频零产出。

    剪掉悬空引用后可能留下空壳单元；空壳不能当看点来源，但它继续阻断整任务就是过度惩罚。
    """
    targets: set[int] = set()
    for issue in issues or []:
        if str(issue.get("code") or "") not in {"empty_refs", "dangling_ref", "invalid_refs"}:
            continue
        match = re.search(r"semantic_units\[(\d+)\]", str(issue.get("path") or ""))
        if match:
            targets.add(int(match.group(1)))
    by_unit = {unit.unit_id: unit for unit in boundary.unit_decisions}
    excluded: list[str] = []
    for index in sorted(targets):
        if not (0 <= index < len(understanding.semantic_units)):
            continue
        unit = understanding.semantic_units[index]
        if unit.claim_ids and unit.evidence_ids:
            continue
        reason = "引用为空，按不可写处理"
        decision = by_unit.get(unit.unit_id)
        if decision is not None:
            if decision.decision == "excluded":
                continue
            decision.decision = "excluded"
            decision.reason = (decision.reason + "；" if decision.reason else "") + reason
        else:
            boundary.unit_decisions.append(UnitDecision(unit_id=unit.unit_id, decision="excluded", reason=reason))
        excluded.append(unit.unit_id)
    return excluded


# 时间码是定位元数据，不是信任凭据。素材本身允许有误差（分段时间码由音频切分而来，
# 越界几十毫秒、甚至几十秒都出现过），这类偏差一律只记录、不阻断。
LOCATOR_ISSUE_CODES = {"location_unknown", "location_invalid"}


def _blocking_reference_issues(issues: list[dict[str, Any]], boundary: BoundarySet) -> list[dict[str, Any]]:
    """哪些引用问题真的该阻断整个任务。

    三条放行规则，都是为了不把"材料或单条陈述的瑕疵"升级成"整条视频零产出"：
    1. 被判 excluded 的陈述本来就不需要引用，空引用不算问题；
    2. 位置未知本身不阻断（已有既定口径）；
    3. 时间码越界同样不阻断——原材料允许误差，定位不准不等于这段文字不可信，
       真正影响可信度的是它引用了什么，而不是它标在第几秒。
    其余问题（编造引用、ID 重复、引用歧义）属于提案侧的结构错误，保留阻断。
    """
    out: list[dict[str, Any]] = []
    for issue in issues or []:
        if str(issue.get("code") or "") in LOCATOR_ISSUE_CODES:
            continue
        path = str(issue.get("path") or "")
        m = re.match(r"boundary\.decisions\[(\d+)\]", path)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(boundary.decisions) and boundary.decisions[idx].decision == "excluded":
                continue
        out.append(issue)
    return out


def _primary_evidence_audit(understanding: Understanding, video: Any) -> dict[str, Any]:
    """标记没有引用 ASR、OCR 或原题文本的陈述，包括纯画面输入。

    原题只允许进入后续核验，不证明视频内容；即使引用了文本，也不能据此推断语义真实或画面已被观察。
    """
    quality = {e.evidence_id: (e.quality or "") for e in video.evidence}
    text_primary = {eid for eid, q in quality.items() if q in TEXT_PRIMARY_QUALITIES}
    source_primary = {eid for eid, q in quality.items() if q == "source"}
    flagged: dict[str, dict[str, Any]] = {}
    for claim in understanding.claims:
        refs = [r for r in claim.evidence_refs if r in quality]
        if set(refs) & (text_primary | source_primary):
            continue
        flagged[claim.claim_id] = {
            "code": "G1_PRIMARY_EVIDENCE_MISSING",
            "referenced_qualities": sorted({quality[r] for r in refs}),
            "reason": "只引用了模型描述/画面路径，未引用任何可用的 ASR/OCR/原题原文",
        }
    return {"applicable": True, "claims": flagged, "claim_total": len(understanding.claims), "flagged": len(flagged),
            "text_primary_count": len(text_primary), "source_count": len(source_primary)}


GENDERED_ACTORS = ("女生", "男生", "女孩", "男孩", "女子", "男子", "她", "他", "学姐", "学妹", "学长", "学弟", "闺蜜")


def _bind_semantic_use_to_evidence(understanding: Understanding, video: Any) -> list[dict[str, Any]]:
    """把「这条意思有多少一手材料支撑」变成程序判定，而不是模型自报。

    C22 的漏洞不在引用合法性，而在 semantic_use/grounding 由模型自己打：模型把只有
    画面描述的单元标成 observable_fact + grounded，程序就放行。这里只做两件确定的事：

    1) 没有 ASR/OCR 支撑的单元不得声明 observable_fact——原题只证明「标题这么写」，
       帧路径和衍生描述不证明画面里发生了什么；
    2) 连一手材料（ASR/OCR/原题）都没有的单元不得声明 grounded。

    只降不升，只动这两个最容易被夸大的标签，并返回改动明细供人工复核。语义真假本身
    仍由独立评审判断——这一步只是不让模型给自己发通行证。
    """
    quality = {e.evidence_id: (e.quality or "") for e in video.evidence}
    claim_refs = {c.claim_id: list(c.evidence_refs or []) for c in understanding.claims}
    adjusted: list[dict[str, Any]] = []
    for unit in understanding.semantic_units:
        refs = [str(r) for r in (unit.evidence_ids or [])]
        if not refs:
            refs = [str(r) for cid in (unit.claim_ids or []) for r in claim_refs.get(str(cid), [])]
        qualities = {quality.get(r, "") for r in refs}
        has_content_text = bool(qualities & TEXT_PRIMARY_QUALITIES)
        has_any_primary = bool(qualities & (TEXT_PRIMARY_QUALITIES | {"source"}))
        before = {"semantic_use": unit.semantic_use, "grounding": unit.grounding}
        downgrades: list[str] = []
        if not has_content_text and unit.semantic_use == "observable_fact":
            unit.semantic_use = "unsupported_inference"
            downgrades.append("semantic_use: 无 ASR/OCR 文本，不能声明可观察事实")
        if not has_any_primary and unit.grounding == "grounded":
            unit.grounding = "unknown"
            downgrades.append("grounding: 无一手材料，不能声明已证实")
        if downgrades:
            adjusted.append({
                "unit_id": unit.unit_id,
                "before": before,
                "after": {"semantic_use": unit.semantic_use, "grounding": unit.grounding},
                "referenced_qualities": sorted(x for x in qualities if x),
                "reasons": downgrades,
            })
    return adjusted


def _sanitize_event_actors(events: list[EventNode], video: Any) -> int:
    """没有一手文本证据的事件，不许指定人物性别。

    画面看不出是谁在说话，也看不出"女生递给男生"里的女生是谁。description 里写死的
    「女生抱花束递给窗内男生」会变成模型判断台词归属的先验，从而把整段对话的方向对调
    （BV1PwsR6oEKW 连续两次都把"男生送吃的"写成"女生送吃的"）。这里把没有 ASR/OCR/原题
    支撑的事件主语降级为"一方"，避免一个未核验的描述决定整条视频的人物关系。
    """
    quality = {e.evidence_id: (e.quality or "") for e in video.evidence}
    changed = 0
    for event in events:
        qualities = {quality.get(x, "") for x in event.evidence_ids}
        if qualities & (TEXT_PRIMARY_QUALITIES | {"source"}):
            continue
        if any(word in event.actor for word in GENDERED_ACTORS):
            event.actor = "一方"
            event.confidence = "speculative"
            changed += 1
    return changed


# 只有「事件之间的关系」算结构断言。评价性判断（这份通报在替谁开脱、这是营销话术、
# 作者有意误导）不对应任何事件，不在此列——那是立场，不是主干。
NARRATIVE_TURN_TYPES = {"contrast", "turn", "causal"}

# 「标题说 X，实际是 Y」不是事件，是元陈述。它把"标题承诺"当成事件端点，
# 于是"标题党"被误读成剧情反差（BV12HxE6cEQ5 把换装摆拍写成了 contrast）。
META_EVENT_WORDS = ("标题", "字幕", "文案", "封面", "描述", "配文", "承诺", "打出", "写着")


def _is_meta_event(event: EventNode) -> bool:
    return any(word in (event.action or "") for word in META_EVENT_WORDS)


# 「她说要交流科学知识，实际在换装」不是叙事反差，是言行不一——那是揭穿/评价，
# 不是"处境与预期相反"。叙事反差的一端必须是发生的事（迟到、赢球、下雨），
# 不能是言语行为本身。
SPEECH_ACT_WORDS = ("说", "喊", "宣称", "表示", "强调", "介绍", "邀请", "承诺", "声明", "回应", "吐槽", "夸")


def _is_speech_event(event: EventNode) -> bool:
    head = (event.action or "")[:8]
    return any(word in head for word in SPEECH_ACT_WORDS)


def _structural_candidates(understanding: Understanding, boundary: BoundarySet) -> list[dict[str, Any]]:
    """引用重合无法证明事件或事件关系；缺独立关系证据契约时不构造结构候选。

    原始事件、转折仍保留在理解结果中供独立主旨审核，不作为已验证的看点。
    """
    return []


DIALOGUE_MIN_SEGMENTS = 12      # 少于这个数，不像多轮对话
DIALOGUE_YOU_RATIO = 0.30       # 含"你"的段占比：对话的标志，解说词几乎没有
DIALOGUE_MAX_AVG_LEN = 10       # 平均句长：长句是播报，短句是台词
NARRATIVE_DESC_RATIO = 0.30     # 缺一手文本的陈述占比：主线不能靠描述撑


def _narrative_risk(video: Any, audit: dict[str, Any]) -> dict[str, Any]:
    """给标题打「生成可信度」标记，不改变判定标准。

    这条只做一件事：告诉评审者"这条是猜的"。判定仍然是三档、标准一致——系统不能因为
    自己没把握就要求人放宽或收紧。分发侧将来按这个标记降权。

    目前能测的两个信号：
    1) 多角色对话但没有说话人标注——"你/我"互指密集而 ASR 无 speaker 字段时，
       事件方向（谁对谁做了什么）只能靠推断。BV1PwsR6oEKW 就栽在这：把"男生让她回去"
       理解成了"女生劝他回去"。
    2) 主线只活在模型描述里——C22 标记占比高，说明情节主要来自 description 而非一手证据。
    """
    signals: list[dict[str, Any]] = []
    segments = [str(seg.get("text") or "") for seg in (getattr(video, "asr_segments", None) or [])]
    if segments:
        you_ratio = sum(1 for text in segments if "你" in text) / len(segments)
        avg_len = sum(len(text) for text in segments) / len(segments)
        if len(segments) >= DIALOGUE_MIN_SEGMENTS and you_ratio >= DIALOGUE_YOU_RATIO and avg_len <= DIALOGUE_MAX_AVG_LEN:
            signals.append({
                "code": "DIALOGUE_SPEAKER_UNATTRIBUTED",
                "value": f"{len(segments)} 段对话、{you_ratio:.0%} 段含「你」、平均 {avg_len:.1f} 字",
                "detail": "多角色对话但 ASR 无说话人标注：谁对谁做了什么只能靠推断，人物方向可能反",
            })
    total = int(audit.get("claim_total") or 0)
    if audit.get("applicable") and total:
        ratio = int(audit.get("flagged") or 0) / total
        if ratio >= NARRATIVE_DESC_RATIO:
            signals.append({
                "code": "NARRATIVE_IN_DESCRIPTION",
                "value": f"{audit.get('flagged')}/{total} 条陈述缺一手文本",
                "detail": "主线情节主要来自模型描述而非一手证据，细节可能是补出来的",
            })
    level = "high" if signals else "none"
    return {
        "level": level,
        "signals": signals,
        "note": "生成可信度低：标题仍是系统产出，判定标准不变；分发侧可按此标记降权。",
    } if signals else {"level": "none", "signals": [], "note": ""}


def _apply_primary_evidence_guard(boundary: BoundarySet, audit: dict[str, Any], understanding: Understanding) -> tuple[BoundarySet, int]:
    """兜底：模型没按提示词判的，程序来判。缺一手文本的陈述不得 usable/limited。

    只降级、不删除：claim 仍然留在 boundary 里并带原因，事后能追到是哪一条编的。
    """
    flagged = set((audit.get("claims") or {}).keys())
    if not flagged:
        return boundary, 0
    downgraded = 0
    for decision in boundary.decisions:
        if decision.claim_id in flagged and decision.decision != "excluded":
            decision.decision = "excluded"
            decision.verification_required = True
            decision.evidence_status = "unknown"
            note = "该陈述缺乏可用文本支持，模型描述或帧路径不能证明画面事实（C22）；保留待核。"
            decision.reason = f"{decision.reason} {note}".strip()
            decision.rule_ids = list(dict.fromkeys([*decision.rule_ids, "U-B01"]))
            downgraded += 1
    # 单元是"几条陈述合起来讲一个意思"：核心陈述是编的，这个意思就不可靠。
    # 只要求"全废"才降级会被一句真话救活整个编造场景——BV1PwsR6oEKW 的 su-01 就是
    # 靠一句真实的「我走了」（c3）让"女生送花束"活了下来。所以过半即降级。
    dead_units: set[str] = set()
    for unit in understanding.semantic_units:
        ids = list(unit.claim_ids or [])
        if not ids:
            continue
        hit = sum(1 for cid in ids if cid in flagged)
        if hit:
            dead_units.add(unit.unit_id)
    for unit_decision in boundary.unit_decisions:
        if unit_decision.unit_id in dead_units and unit_decision.decision != "excluded":
            unit_decision.decision = "excluded"
            note = "该单元依赖待核陈述（C22），不能作为已确认看点来源。"
            unit_decision.reason = f"{unit_decision.reason} {note}".strip()
    return boundary, downgraded


def _apply_conflict_gate(understanding: Any, boundary: Any) -> Any:
    """冲突闸门：medium/high 冲突涉及的 claim 从 usable 降到 limited。"""
    conflicts = getattr(understanding, "conflicts", None) or []
    if not conflicts or not getattr(boundary, "decisions", None):
        return boundary
    evidence_to_claims: dict[str, list[str]] = {}
    for claim in understanding.claims:
        for ev in claim.evidence_refs or []:
            evidence_to_claims.setdefault(str(ev), []).append(claim.claim_id)
    decisions = {d.claim_id: d for d in boundary.decisions}
    for conflict in conflicts:
        severity = str(conflict.get("severity") or "").lower()
        if severity not in {"medium", "high"}:
            continue
        touched: set[str] = set()
        for ev in conflict.get("evidence_ids") or []:
            touched.update(evidence_to_claims.get(str(ev), []))
        for claim_id in touched:
            decision = decisions.get(claim_id)
            if decision is None or decision.decision != "usable":
                continue
            decision.decision = "limited"
            decision.required_limits = list(decision.required_limits) + [
                "存在冲突（%s）：%s" % (conflict.get("conflict_id", ""), str(conflict.get("description") or "")[:60])
            ]
            decision.reason = (decision.reason or "") + " | 冲突闸门：降级为 limited"
    return boundary


def _foreign_evidence_ratio(refs: list[str]) -> float:
    """证据文本的外文占比（ASCII 字母字符 ÷ 全部非空白字符）。

    V 集 2026-11-04 实况：英文解说视频的 ASR/OCR 是英文，中文 claim 的数字词
    （「数小时」）对英文证据（"Do it for hours every day"）做词法比对必然失败，
    supported 的 claim 被数字守卫整条 excluded。跨语言词法测不出，不能据此否决。
    """
    if not refs:
        return 0.0
    letters = sum(1 for t in refs for ch in str(t or "") if ch.isascii() and ch.isalpha())
    chars = sum(1 for t in refs for ch in str(t or "") if not ch.isspace())
    return letters / chars if chars else 0.0


def _apply_numeric_conflict_gate(video: Any, understanding: Any, boundary: Any) -> Any:
    """数字必须能落到所引用的原始文本，不恢复被其他守卫排除的陈述。

    跨语言证据（外文占比 > 50%）跳过词法核验：中文数字词对英文 ASR/OCR
    永远匹配不上，据此排除是系统性误伤（人体科学视频 c7 实况：6 条证据、
    evidence_status=supported，仍被整条 excluded）。
    """
    evidence = {e.evidence_id: e for e in video.evidence}
    decisions = {d.claim_id: d for d in boundary.decisions}
    for claim in understanding.claims:
        refs = [evidence[r].text for r in claim.evidence_refs if r in evidence and evidence[r].quality not in {"model_derived_uncalibrated", "unverified_source_metadata"} and evidence[r].field != "frame"]
        decision = decisions.get(claim.claim_id)
        if _foreign_evidence_ratio(refs) > 0.5:
            if decision is not None and decision.decision in {"usable", "limited"}:
                # 无法词法核验不是数字已获支持；保留为待核，禁止进入自动可写集合。
                if unsupported_numeric_facts(claim.text, refs):
                    decision.decision = "limited"
                    decision.verification_required = True
                    decision.reason = (decision.reason or "") + " | 跨语言数字证据无法词法核验，须人工核实"
            continue
        unsupported = unsupported_numeric_facts(claim.text, refs)
        if not unsupported or decision is None or decision.decision not in {"usable", "limited"}:
            continue
        decision.decision = "excluded"
        decision.verification_required = True
        decision.reason = (decision.reason or "") + " | 数字证据守卫：引用文本未支持该数值、单位或指标，须核验；原题不自动优先"
    return boundary


BOUNDARY_ENVELOPE_KEYS = ("boundary_set", "BoundarySet", "boundarySet")
# 分组键别名。allowed_claims 是真实模型用过的写法（BV1j6JR6AEAh）：漏认它会把
# 5 条 usable 读成 0，整任务按 NO_USABLE_CLAIM 白跑。
BOUNDARY_GROUP_ALIASES = (
    ("usable", ("usable_claims", "usable", "allowed_claims", "allowed")),
    ("limited", ("limited_claims", "limited")),
    ("excluded", ("excluded_claims", "excluded")),
)
BOUNDARY_OBJECT_CONTAINERS = ("decisions", "claim_decisions", "items", "boundaries", "claims")
BOUNDARY_PENDING_KEYS = ("verification_required_claims", "verification_pending", "verification_required")
_BOUNDARY_DECISION_RANK = {"usable": 0, "limited": 1, "excluded": 2}
BOUNDARY_DECISION_MISSING = "BOUNDARY_DECISION_MISSING"


def _iter_boundary_layers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """按「外层优先」广度遍历 raw 及其所有边界信封容器。

    真实模型会把同一份决策拆到不同层级：完整 claim 对象在顶层，决策分组在
    boundary_set 里（BV13ZHT6AEYW）。只看一层必然漏读。
    """
    layers: list[dict[str, Any]] = []
    seen: set[int] = set()
    queue: list[Any] = [raw]
    while queue:
        layer = queue.pop(0)
        if not isinstance(layer, dict) or id(layer) in seen:
            continue
        seen.add(id(layer))
        layers.append(layer)
        for key in BOUNDARY_ENVELOPE_KEYS:
            nested = layer.get(key)
            if isinstance(nested, dict):
                queue.append(nested)
    return layers


def _boundary_pending_ids(layer: dict[str, Any]) -> set[str]:
    return {str(x) for key in BOUNDARY_PENDING_KEYS
            if isinstance(layer.get(key), list) for x in layer[key] if isinstance(x, str) and x}


def collect_boundary_groups(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """收集所有层级的「按决策分组」结果，作为逐条决策的覆盖层。

    按「最严者胜」合并重复 ID（一条 claim 同时出现在 usable 与 excluded 时取 excluded）。
    """
    merged: dict[str, dict[str, Any]] = {}
    for layer in _iter_boundary_layers(raw):
        for decision, aliases in BOUNDARY_GROUP_ALIASES:
            for alias in aliases:
                values = layer.get(alias)
                if not isinstance(values, list):
                    continue
                for cid in values:
                    if not isinstance(cid, str) or not cid:
                        continue
                    current = merged.setdefault(cid, {})
                    if _BOUNDARY_DECISION_RANK[decision] >= _BOUNDARY_DECISION_RANK.get(str(current.get("decision") or ""), -1):
                        current["decision"] = decision
        pending = _boundary_pending_ids(layer)
        if isinstance(layer.get("verification_required"), bool) and layer["verification_required"]:
            # 布尔式 verification_required=true 只在 limited 组内联时才是 claim 级信号，
            # 这里只在它旁边确实有可用清单时采纳，避免把全局标志读成逐条待核。
            pending = pending | {str(x) for x in (layer.get("usable_claims") or []) if str(x)}
        for cid in pending:
            merged.setdefault(cid, {}).setdefault("decision", "limited")
            merged[cid]["verification_required"] = True
    return merged


def collect_boundary_objects(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """收集所有层级的逐条 claim 决策对象，外层优先、同一 ID 不覆盖。

    分组摘要只提供 decision，会丢掉 required_limits / forbidden_inferences /
    original_source 等字段；完整对象必须保留，不能被分组摘要换掉。
    """
    ordered: dict[str, dict[str, Any]] = {}
    for layer in _iter_boundary_layers(raw):
        for name in BOUNDARY_OBJECT_CONTAINERS:
            seq = layer.get(name)
            if not isinstance(seq, list):
                continue
            for item in seq:
                if not isinstance(item, dict):
                    continue
                cid = item.get("claim_id") or item.get("id")
                if cid is None:
                    continue
                cid = str(cid)
                # 旧别名与新字段可能跨不同包装同时出现；合并前统一，不丢任何限定。
                if isinstance(item.get("limits"), list):
                    item = {**item, "required_limits": list(dict.fromkeys([*(item.get("required_limits") or []), *item["limits"]]))}
                if cid not in ordered:
                    ordered[cid] = {**item, "claim_id": cid}
                    continue
                existing = ordered[cid]
                previous = _boundary_item_decision(existing)
                incoming = _boundary_item_decision(item)
                if incoming and (not previous or _BOUNDARY_DECISION_RANK[incoming] > _BOUNDARY_DECISION_RANK[previous]):
                    existing["decision"] = incoming
                for key in ("required_limits", "conditional_constraints", "factual_context", "forbidden_inferences", "source_evidence_refs", "rule_ids"):
                    if isinstance(item.get(key), list):
                        existing[key] = list(dict.fromkeys([*(existing.get(key) or []), *item[key]]))
                if item.get("verification_required") or item.get("pending_verification") or _item_requires_verification(item):
                    existing["verification_required"] = True
    return list(ordered.values())


def _summary_keyed_values(raw: dict[str, Any], *keys: str) -> dict[str, list[str]]:
    """解析 "c2: 须保留…" 这种按 claim 编号成条摘要的字段。"""
    out: dict[str, list[str]] = {}
    for layer in _iter_boundary_layers(raw):
        for key in keys:
            seq = layer.get(key)
            if not isinstance(seq, list):
                continue
            for entry in seq:
                text = str(entry)
                for sep in (":", "："):
                    head, found, tail = text.partition(sep)
                    if found and 0 < len(head.strip()) <= 8 and tail.strip():
                        out.setdefault(head.strip(), []).append(tail.strip())
                        break
    return out


def _grouped_boundary_decisions(raw: dict[str, Any]) -> list[dict[str, Any]] | None:
    """按决策分组的 ID 列表 → 逐条决策对象（兼容包装，勿用于替换完整对象）。"""
    groups = collect_boundary_groups(raw)
    if not groups:
        return None
    items = []
    for cid, group in groups.items():
        item: dict[str, Any] = {"claim_id": cid, "decision": group.get("decision") or "excluded"}
        if group.get("verification_required"):
            item["verification_required"] = True
        items.append(item)
    return items


def _missing_boundary_claims(boundary: BoundarySet) -> list[str]:
    """取出「模型没表态、被兜底成 excluded」的 claim id 清单。"""
    ids: list[str] = []
    for issue in getattr(boundary, "unresolved_issues", None) or []:
        if isinstance(issue, dict) and str(issue.get("code") or "") == BOUNDARY_DECISION_MISSING:
            ids.extend(str(x) for x in (issue.get("claim_ids") or []))
    return sorted(set(ids))


def _normalize_boundary_decision(value: Any) -> str | None:
    """边界决策枚举归一。认不出的值返回 None —— 不许静默当成 excluded。"""
    if isinstance(value, bool):
        return "usable" if value else "excluded"
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().lower()
    if text in {"usable", "useable", "allowed", "eligible", "allow"}:
        return "usable"
    if text in {"limited", "usable_with_limits", "useable_with_limits", "with_limits", "partially_usable"}:
        return "limited"
    if text in {"verification_required", "pending_verification", "needs_verification", "uncertain", "qualified"}:
        return "limited"
    if text in {"excluded", "exclude", "forbidden", "blocked", "not_usable"}:
        return "excluded"
    return None


def _item_requires_verification(item: dict[str, Any]) -> bool:
    return any(str(item.get(key) or "").strip().lower() in {"verification_required", "pending_verification", "needs_verification"}
               for key in ("decision", "status", "verdict"))


def _boundary_item_decision(item: dict[str, Any]) -> str | None:
    for key in ("decision", "status", "verdict", "usable"):
        if key in item:
            resolved = _normalize_boundary_decision(item.get(key))
            if resolved:
                return resolved
    return None


def _normalize_boundary(raw: dict[str, Any], claim_texts: dict[str, str] | None = None, claim_evidence: dict[str, list[str]] | None = None) -> BoundarySet:
    from .types import ClaimDecision
    # 模型会把完整 claim 对象放外层、决策分组放 boundary_set（BV13ZHT6AEYW），或者反过来。
    # 任何「换成某一层」的做法都会整批丢决策：这里保留所有层级，逐层收集后再合并。
    envelope = raw.get("BoundarySet") if isinstance(raw.get("BoundarySet"), dict) else raw.get("boundarySet")
    if isinstance(envelope, dict):
        raw = {**envelope, **raw}
    layers = _iter_boundary_layers(raw)
    global_requirements: dict[str, Any] = {}
    for metadata in layers:
        for key, aliases in {
            "required_subject": ("required_subject", "must_keep_subject"),
            "required_source_phrase": ("required_source_phrase", "source_phrase_required"),
            "generic_subject_not_allowed": ("generic_subject_not_allowed",),
            "length_feasibility": ("length_feasibility",),
        }.items():
            if key in global_requirements:
                continue
            for alias in aliases:
                if alias in metadata:
                    global_requirements[key] = metadata[alias]
                    break
    groups = collect_boundary_groups(raw)
    summary_limits = _summary_keyed_values(raw, "required_limits_summary")
    summary_forbidden = _summary_keyed_values(raw, "forbidden_inferences_summary")
    # 完整对象是底稿：分组摘要只有 decision，覆盖上去会把 required_limits / 来源信息抹掉。
    items: Any = collect_boundary_objects(raw)
    if not items and isinstance(raw.get("decision"), str):
        # 模型有时把单条决策平铺返回，而不是 {"decisions": [...]}
        items = [dict(raw)]
    if not items and not isinstance(raw.get("decision"), str) and raw and all(isinstance(value, dict) for value in raw.values()):
        # 平铺形状的兜底只认真正的 claim_id。否则 {"claims_summary": {...}} 这种摘要容器
        # 会被当成一条 claim_id=claims_summary 的决策，凭空造出一条不存在于材料的边界。
        known_claims = set(claim_texts or {})
        items = {key: value for key, value in raw.items() if key not in BOUNDARY_ENVELOPE_KEYS and (not known_claims or key in known_claims)}
    if isinstance(items, dict):
        items = [{"claim_id": claim_id, **(item if isinstance(item, dict) else {})} for claim_id, item in items.items()]
    # 只出现在分组里、没有完整对象的 claim：补一条最小对象，也不许把它的决策丢掉。
    known_ids = {str(it.get("claim_id") or it.get("id")) for it in items if isinstance(it, dict) and (it.get("claim_id") or it.get("id"))}
    for cid, group in groups.items():
        if cid not in known_ids:
            items.append({"claim_id": cid, "_from_group": group})
    decisions = []
    missing_decision_ids: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        cid = str(item.get("claim_id") or item.get("id") or "")
        if not cid:
            continue
        group = item.pop("_from_group", None) or groups.get(cid) or {}
        refs = item.get("evidence_refs") or item.get("evidence_ids") or []
        # 边界节点经常不回填 evidence_refs：引用关系在理解阶段已经建立，
        # 这里直接从对应 claim 继承，而不是事后判"引用列表为空"把整条任务废掉。
        if not refs and claim_evidence:
            refs = list(claim_evidence.get(cid) or [])
        choices = [d for d in (_boundary_item_decision(item), _normalize_boundary_decision(group.get("decision"))) if d]
        decision = max(choices, key=_BOUNDARY_DECISION_RANK.get) if choices else None
        if decision is None:
            # 模型对这条 claim 完全没表态。excluded 是保守兜底，但必须留痕：
            # 静默兜底会让「模型漏答」看起来像「材料真的不可用」，整任务白跑还没人知道。
            missing_decision_ids.append(cid)
            decision = "excluded"
        decisions.append(ClaimDecision(
            claim_id=cid, supported_statement=item.get("supported_statement") or item.get("statement") or item.get("text") or (claim_texts or {}).get(cid),
            decision=decision, evidence_refs=[str(x) for x in refs],
            required_limits=[str(x) for x in (item.get("required_limits") or item.get("limits") or group.get("required_limits") or summary_limits.get(cid) or [])],
            forbidden_inferences=[str(x) for x in (item.get("forbidden_inferences") or group.get("forbidden_inferences") or summary_forbidden.get(cid) or [])],
            rule_ids=[str(x) for x in (item.get("rule_ids") or [])], reason=str(item.get("reason") or item.get("rationale") or ""),
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
            conditional_constraints=[str(x) for x in (item.get("conditional_constraints") or [])],
            factual_context=[str(x) for x in (item.get("factual_context") or [])],
            verification_required=bool(item.get("verification_required") or item.get("pending_verification") or group.get("verification_required") or _item_requires_verification(item)),
        ))
    units_items: Any = []
    for layer in layers:
        candidate_units = layer.get("unit_decisions") or layer.get("unitDecisions")
        if candidate_units:
            units_items = candidate_units
            break
    if isinstance(units_items, dict):
        # 模型也可能给 {"u1": "limited"}：字符串值是决策本身，不能当成空对象丢掉
        units_items = [{"unit_id": k, **(v if isinstance(v, dict) else {"decision": v})} for k, v in units_items.items()]
    if not isinstance(units_items, list):
        units_items = []
    unit_decisions: list[UnitDecision] = []
    for i, item in enumerate(units_items):
        if not isinstance(item, dict):
            continue
        decision = _boundary_item_decision(item) or "excluded"
        unit_decisions.append(UnitDecision(
            unit_id=str(item.get("unit_id") or f"u{i+1}"),
            decision=decision,
            required_limits=[str(x) for x in (item.get("required_limits") or item.get("limits") or [])],
            forbidden_inferences=[str(x) for x in (item.get("forbidden_inferences") or [])],
            attribution_required=bool(item.get("attribution_required") or item.get("required_attribution")),
            required_source_phrase=item.get("required_source_phrase") or item.get("required_attribution"),
            reason=str(item.get("reason") or item.get("rationale") or ""),
        ))
    # 输入中整条缺失同样是漏答；不得按位置补配或视为业务拒绝。
    if claim_texts is not None:
        present = {d.claim_id for d in decisions}
        missing_decision_ids.extend(cid for cid in claim_texts if cid not in present)
    missing_decision_ids = sorted(set(missing_decision_ids))
    unresolved = raw.get("unresolved_issues") if isinstance(raw.get("unresolved_issues"), list) else []
    if missing_decision_ids:
        unresolved = list(unresolved) + [{
            "code": BOUNDARY_DECISION_MISSING,
            "claim_ids": sorted(missing_decision_ids),
            "message": f"模型未对 {len(missing_decision_ids)} 条 claim 给出决策；这些 claim 不得进入可写集合。", 
        }]
    return BoundarySet(decisions=decisions, unresolved_issues=unresolved, global_requirements=global_requirements, unit_decisions=unit_decisions)


REFUTED_EVIDENCE_LIMIT = "外部来源提出反驳但证据不足：不得作为确定事实，须标明存在争议"
# 核查争议类限定必须落到标题文本：只说「据称」不够，用户仍需知道这条说法存在争议。
DISPUTE_LIMIT_MARK = "存在争议"
DISPUTE_MARK_PHRASES = ("存在争议", "有争议", "尚存疑", "存疑", "未经证实", "未获证实", "说法不一", "遭到反驳", "被质疑", "另有报道", "有报道称")


def _apply_verification_constraints(
    understanding: Understanding,
    boundary: BoundarySet,
    refute_min_sources: int = 2,
    confidence_threshold: float = 0.8,
) -> dict[str, list[str]]:
    """把外部核查结论变成对标题表达的实际约束。

    - 反驳达标（独立来源与置信度都过阈值）：claim 强制 excluded，禁止进入标题；
    - 反驳未达标（downgraded）：保留「存在反驳」这一信息，降级为 limited + required_limits，
      既不放行成确定事实，也不假装没核查过。
    """
    excluded: list[str] = []
    limited: list[str] = []
    for claim in understanding.claims:
        verification = getattr(claim, "verification", None)
        if getattr(verification, "verdict", "") != "refuted":
            continue
        sources = {str(x) for x in (getattr(verification, "sources", []) or []) if x}
        confidence = float(getattr(verification, "confidence", 0.0) or 0.0)
        downgraded = bool(getattr(verification, "downgraded", False))
        if downgraded or len(sources) < refute_min_sources or confidence < confidence_threshold:
            limited.append(claim.claim_id)
        else:
            excluded.append(claim.claim_id)
    if not excluded and not limited:
        return {"excluded": [], "limited": []}
    by_id = {decision.claim_id: decision for decision in boundary.decisions}
    for claim_id in excluded:
        decision = by_id.get(claim_id)
        if decision is None:
            continue
        decision.decision = "excluded"
        decision.reason = (decision.reason + "；" if decision.reason else "") + "外部核查驳回（refuted），禁止进入标题"
    for claim_id in limited:
        decision = by_id.get(claim_id)
        if decision is None or decision.decision == "excluded":
            continue
        decision.decision = "limited"
        limits = list(getattr(decision, "required_limits", []) or [])
        if REFUTED_EVIDENCE_LIMIT not in limits:
            limits.append(REFUTED_EVIDENCE_LIMIT)
        decision.required_limits = limits
        decision.reason = (decision.reason + "；" if decision.reason else "") + "外部核查存在反驳但证据不足，降级为限定表达"
    hard, soft = set(excluded), set(limited)
    for unit in understanding.semantic_units:
        ids = list(getattr(unit, "claim_ids", []) or [])
        if not ids or not (hard | soft) & set(ids):
            continue
        unit.grounding = "speculative" if hard and all(claim_id in hard for claim_id in ids) else "inferred"
    return {"excluded": excluded, "limited": limited}


def _title_key(text: Any) -> str:
    """比较标题是否重复用的归一化键：去掉标点、空白、大小写差异。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or "")).lower()


def _rewrite_structure_error(raw: dict[str, Any]) -> str | None:
    """空候选合法；非空候选必须含可用标题，避免归一化时静默丢失。"""
    for key in ("AgentDraft", "agent_draft", "output"):
        if isinstance(raw.get(key), dict):
            raw = raw[key]
    items = raw.get("candidates")
    if not items and isinstance(raw.get("items"), list):
        items = raw["items"]
    if not items and isinstance(raw.get("drafts"), list):
        items = raw["drafts"]
    if not isinstance(items, list):
        return "candidates must be a list"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return f"candidates[{index}] must be an object"
        headline = next((item.get(key) for key in ("headline", "candidate_title", "title_text", "title", "copy", "draft_text", "text") if item.get(key)), None)
        if not isinstance(headline, str) or not headline.strip():
            return f"candidates[{index}] headline must be a non-empty string"
    return None


def _normalize_candidates(raw: dict[str, Any], rewrite_round: int, parent_id: str | None, required_prefixes: list[str] | None = None) -> list[dict[str, Any]]:
    required = list(required_prefixes or [])
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
        # 模型常把【剧情演绎】直接写进正文：先剥回前缀字段，正文只留可读内容
        inline_labels, head_body = extract_inline_prefixes(str(headline))
        declared = list(item.get("prefix_labels") or []) + inline_labels
        prefix_labels = normalize_prefix_labels(declared, required)
        candidate_schema_errors = []
        # candidate_id 与 highlight_id 缺失由程序确定性补全（顺序号 / 父候选继承），
        # 不构成"结构不合法"——把它们记成错误会让一条本来可交付的标题被判 G4_SCHEMA_INVALID。
        if not item.get("claim_ids"): candidate_schema_errors.append("claim_ids missing from generation output")
        if not (item.get("evidence_ids") or item.get("evidence_refs")): candidate_schema_errors.append("evidence_ids missing from generation output")
        claim_ids = []
        for claim_id in item.get("claim_ids") or []:
            text_id = str(claim_id)
            if text_id.startswith("c-") and text_id[2:].isdigit(): text_id = "c" + str(int(text_id[2:]))
            claim_ids.append(text_id)
        candidate = {"candidate_id": str(item.get("candidate_id") or item.get("draft_id") or item.get("id") or f"a{rewrite_round+1}-{i+1}"), "headline": str(head_body), "prefix_labels": prefix_labels, "self_declared_prefixes": self_declared_prefixes(declared, required), "highlight_id": str(item.get("highlight_id") or "h1"), "unit_id": str(item.get("unit_id") or item.get("semantic_unit_id") or ""), "claim_ids": claim_ids, "evidence_ids": [str(x) for x in (item.get("evidence_ids") or item.get("evidence_refs") or [])], "expression_strategy": str(item.get("expression_strategy") or item.get("strategy") or "unknown"), "used_case_ids": [str(x) for x in (item.get("used_case_ids") or [])], "generation_round": int(item.get("generation_round", rewrite_round)), "parent_candidate_id": item.get("parent_candidate_id") or parent_id, "change_reason": str(item.get("change_reason") or ""), "qualifiers_used": ([str(item["qualifiers_used"])] if isinstance(item.get("qualifiers_used"), str) else [str(x) for x in (item.get("qualifiers_used") or [])]), "addressed_issue_ids": [str(x) for x in (item.get("addressed_issue_ids") or [])], "slot_id": str(item.get("slot_id") or ""), "thesis_id": str(item.get("thesis_id") or ""), "thesis_basis": str(item.get("thesis_basis") or ""), "generation_schema_errors": candidate_schema_errors}
        candidate["rendered_title"] = render_title(candidate)
        candidate["title_length"] = len(candidate["rendered_title"])
        out.append(candidate)
    return out


def _fill_compress_draft(draft: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """压缩草案只带文字：引用、槽位、主旨、前缀一律继承父候选。

    模型这一步看不到 video/claims/boundary 全文，让它自己报 claim_ids 只会逼它
    编造；而引用错了会被判 G4_INVALID_REFERENCE，等于把一次纯文字压缩变成结构
    缺陷。继承父候选是唯一安全的做法：压缩不改绑定。
    """
    out = dict(draft or {})
    items = []
    for item in out.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        headline = (item.get("headline") or item.get("candidate_title") or item.get("title_text")
                    or item.get("title") or item.get("copy") or item.get("draft_text") or item.get("text"))
        if not headline:
            continue
        items.append({
            "candidate_id": source.get("candidate_id"),
            "headline": headline,
            "prefix_labels": list(source.get("prefix_labels") or []),
            "highlight_id": source.get("highlight_id"),
            "claim_ids": list(source.get("claim_ids") or []),
            "evidence_ids": list(source.get("evidence_ids") or []),
            "unit_id": source.get("unit_id"),
            "slot_id": source.get("slot_id"),
            "thesis_id": source.get("thesis_id"),
            "expression_strategy": item.get("strategy") or source.get("expression_strategy") or "直接描述",
            "qualifiers_used": item.get("qualifiers_used") or [],
            "used_case_ids": [],
            "generation_round": int(source.get("generation_round") or 0) + 1,
            "parent_candidate_id": source.get("candidate_id"),
            "change_reason": str(item.get("change_reason") or "压缩到字数预算内，事实与归属不变"),
        })
    out["candidates"] = items
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
        hook_type = str(item.get("hook_type") or item.get("hook") or "")
        if hook_type not in {"information", "suspense", "identity", "conflict", "emotion", "contrast"}:
            hook_type = ""
        result.append(Highlight(
            highlight_id=str(item.get("highlight_id") or f"h{i+1}"), claim_ids=claim_ids,
            value=value, representativeness=str(item.get("representativeness") or item.get("selection_reason") or ""),
            delivery_evidence_refs=evidence_ids, role=str(item.get("role") or ("main" if i == 0 else "backup")),
            viewer_value=str(item.get("viewer_value") or item.get("what_user_can_see") or value),
            required_qualifiers=[str(x) for x in (item.get("required_qualifiers") or item.get("required_limits") or item.get("qualifiers") or [])],
            selection_reason=str(item.get("selection_reason") or item.get("representativeness") or ""),
            unit_id=str(item.get("unit_id") or item.get("semantic_unit_id") or ""),
            hook_type=hook_type, relation=str(item.get("relation") or ""),
            thesis_id=str(item.get("thesis_id") or ""),
        ))
    return result[:3] or fallback


def _enforce_rewrite_slot(candidates: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
    """重写必须留在父候选的槽位：slot_id 由程序从父候选继承。

    slot_id 是父候选的结构属性，不是重写要重新表达的语义。要求模型复述它，
    等于用一个模型没理由关心的字段把好标题打成结构非法——真实故障
    （2026-11-02，R09 白菜）24 条候选因此被判 G4_SCHEMA_INVALID 掉出交付面，
    而它们的槽位从头到尾没变过。只有模型明确写了另一个槽位才算"改槽"。
    """
    parent_slot = str(source.get("slot_id") or "")
    if parent_slot not in SLOTS:
        return candidates
    for c in candidates:
        declared = str(c.get("slot_id") or "")
        if not declared:
            c["slot_id"] = parent_slot
            c["slot_id_inherited"] = True
        elif declared != parent_slot:
            c.setdefault("generation_schema_errors", []).append(f"rewrite changed slot_id: {declared} != {parent_slot}")
    return candidates


def _enforce_rewrite_thesis(candidates: list[dict[str, Any]], source: dict[str, Any], contract: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """重写只能修同一个题目：换了 thesis_id 就等于换了看点。

    这是"重写靠删掉主题让标题变好过审"的唯一程序闸门。命中时不立刻丢弃候选，
    而是记一条结构错误，让机械校验把它退回重写/待审，保留可追溯的失败原因。
    """
    locked = str(source.get("thesis_id") or (contract or {}).get("thesis_id") or "")
    for c in candidates:
        if not locked:
            c.setdefault("generation_schema_errors", []).append("rewrite missing locked thesis_id")
        elif not c.get("thesis_id"):
            c["thesis_id"] = locked
            c["thesis_id_inherited"] = True
        elif c["thesis_id"] != locked:
            c.setdefault("generation_schema_errors", []).append(f"rewrite changed thesis_id: {c['thesis_id']} != {locked}")
    return candidates


REVIEW_DIMENSIONS = {"G1", "G2", "G3", "Q1", "Q2", "Q3"}
ISSUE_SEVERITIES = {"low", "medium", "high", "critical"}
SEVERITY_ALIASES = {"严重": "critical", "高": "high", "中": "medium", "低": "low", "high": "high", "medium": "medium"}


def _normalize_issue_fields(issue: Any) -> Any:
    """把评审问题的字段写法归一，而不是整份评审作废。

    真实故障：模型把 primary_dimension 写成 issue_code 全名、severity 用中文，
    校验器直接判 REVIEW_SCHEMA_INVALID，三个候选全部变成"待核"，视频零交付。
    字段写法偏差属于表达层，不该换来一次零产出。
    """
    if not isinstance(issue, dict):
        return issue
    fixed = dict(issue)
    notes: list[str] = list(fixed.get("_normalization_notes") or [])
    code = str(fixed.get("issue_code") or "")
    prefix = code.split("_", 1)[0]
    dimension = str(fixed.get("primary_dimension") or "")
    if dimension not in REVIEW_DIMENSIONS and prefix in REVIEW_DIMENSIONS:
        fixed["primary_dimension"] = prefix
        notes.append(f"primary_dimension 由 issue_code 前缀推得: {prefix}")
    severity = str(fixed.get("severity") or "")
    if severity not in ISSUE_SEVERITIES:
        fixed["severity"] = SEVERITY_ALIASES.get(severity, "medium")
        notes.append(f"severity 归一为 {fixed['severity']}（原值 {severity or '空'}）")
    if notes:
        fixed["_normalization_notes"] = notes
    return fixed


CONSISTENCY_RELATIONS = ("aligned", "detail", "missing_point", "contradiction")


def _normalize_thesis_consistency(raw: Any) -> dict[str, Any] | None:
    """读 R01 回传的跨契约一致性判断；缺失或非法一律返回 None，交由词法判据兜底。

    R01 本来就同时拿到两套主旨契约，提示词也一直写着「不一致就标 needs_review」，
    只是没有一个结构化字段可以回传这个任务级结论——于是它只把判断掺在 G3 的理由句里
    （canary 剧情那条的理由是「与视频 ASR 呈现的关心互动一致」，等于没查）。
    这里给它一个位置，因此**不增加任何模型调用**。

    非法即视为没答，不做宽容解析：`contradiction` 必须点名互斥在哪一处，空着说明
    模型只是随手给了个结论，不能据此降级交付。
    """
    node: Any = raw
    for key in ("ReviewResultBatch", "ReviewBatch", "review_batch"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    if not isinstance(node, dict):
        return None
    item = node.get("thesis_consistency")
    if not isinstance(item, dict) and isinstance(node.get("relation"), str):
        # 专职比对调用直接返回判定对象本身，没有 thesis_consistency 外壳
        item = node
    if not isinstance(item, dict):
        return None
    relation = str(item.get("relation") or "").strip()
    if relation not in CONSISTENCY_RELATIONS:
        return None
    out = {
        "relation": relation,
        "reason": str(item.get("reason") or "").strip(),
        "contradiction_point": str(item.get("contradiction_point") or "").strip(),
        "generated_missing": [str(x) for x in (item.get("generated_missing") or []) if str(x).strip()],
        "review_missing": [str(x) for x in (item.get("review_missing") or []) if str(x).strip()],
    }
    if relation == "contradiction" and not out["contradiction_point"]:
        return None
    return out


REVIEW_EVIDENCE_FIELDS = ("evidence_id", "field", "text", "quality", "locator")
REVIEW_SIGNAL_FIELDS = ("signal_id", "signal_type", "value", "quality", "evidence_id")


def _compact_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把 R01 载荷裁到「评审实际会引用」的部分，不改变它读到的材料。

    实测同一条视频的 R01 载荷 72,236 字符：`evidence` 68 条里只有 32 条被
    候选、claim 或 boundary 引用，其余 36 条没进入任何结论；`risk_signals`
    65 条每条重复 12 个键名，键名开销约占该字段一半，且 type 全是 number、
    polarity 全是 positive、status 全相同——逐条复述这些常量不提供判据。

    裁剪原则是**引用闭包**：凡是某条证据的 id 在载荷其他位置出现过，就保留它；
    一手材料（`quality=source`，原题/简介/字幕原文）无条件保留，因为它们不是
    「被引用才成立」而是事实底座。闭包为空时整份原样返回，避免裁成空壳。
    """
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return payload
    rest = {key: value for key, value in payload.items() if key != "evidence"}
    blob = json.dumps(rest, ensure_ascii=False)
    referenced = {
        str(item.get("evidence_id"))
        for item in evidence
        if isinstance(item, dict) and str(item.get("evidence_id") or "") and str(item.get("evidence_id")) in blob
    }
    primary = {
        str(item.get("evidence_id"))
        for item in evidence
        if isinstance(item, dict) and str(item.get("evidence_id") or "") and item.get("quality") == "source"
    }
    keep = referenced | primary
    if not keep:
        return payload
    compacted = dict(rest)
    compacted["evidence"] = [
        {key: item[key] for key in REVIEW_EVIDENCE_FIELDS if key in item}
        for item in evidence
        if isinstance(item, dict) and str(item.get("evidence_id")) in keep
    ]
    signals = (payload.get("risk_signals") or {}).get("signals")
    kept_signals = 0
    if isinstance(signals, list):
        slim = [
            {key: item[key] for key in REVIEW_SIGNAL_FIELDS if key in item}
            for item in signals
            if isinstance(item, dict)
        ]
        kept_signals = len(slim)
        risk = dict(payload.get("risk_signals") or {})
        risk["signals"] = slim
        risk.pop("extractor_version", None)
        compacted["risk_signals"] = risk
    compacted["_compaction"] = {
        "evidence": [len(evidence), len(compacted["evidence"])],
        "risk_signals": [len(signals) if isinstance(signals, list) else 0, kept_signals],
        "note": "只保留被候选/claim/boundary 引用的证据与一手材料；信号保留判断所需字段",
    }
    return compacted


def _review_batch_structure_error(raw: dict[str, Any], candidate_ids: list[str]) -> str | None:
    """只检查结构与覆盖率；证据、规则和语义仍由下游裁决。"""
    node: Any = raw
    for key in ("ReviewResultBatch", "ReviewBatch", "review_batch"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    if not isinstance(node, dict):
        return "review batch must be an object"
    reviews = next((node[k] for k in ("reviews", "evaluations", "results", "candidates") if k in node), None)
    if not isinstance(reviews, list):
        return "reviews must be a list"
    ids = []
    for item in reviews:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str):
            return "review and candidate_id have invalid types"
        ids.append(item["candidate_id"])
        takeaway = item.get("expected_takeaway", item.get("viewer_expectation"))
        if not (isinstance(takeaway, str) and takeaway.strip()) and not (
            isinstance(takeaway, dict) and all(isinstance(takeaway.get(k), str) for k in ("subject", "content_nature", "viewing_promise"))
        ):
            return "expected_takeaway missing or invalid"
        gates = item.get("gates")
        if not isinstance(gates, dict):
            return "gates missing or invalid"
        for key in GATE_IDS:
            gate = gates.get(key)
            if not isinstance(gate, dict) or gate.get("status") not in ("pass", "revise", "needs_review", "reject") or not isinstance(gate.get("reason"), str) or not gate["reason"].strip():
                return f"{key} missing or invalid"
            if any(not isinstance(gate.get(k, []), list) or any(not isinstance(x, str) for x in gate.get(k, [])) for k in ("evidence_ids", "rule_ids")):
                return f"{key} references invalid"
        quality = item.get("quality")
        if quality is None and isinstance(item.get("quality_scores"), dict) and isinstance(item.get("quality_reasons"), dict):
            quality = {k: {"score": v, "reason": item["quality_reasons"].get(k)} for k, v in item["quality_scores"].items()}
        if not isinstance(quality, dict):
            return "quality missing or invalid"
        for key in QUALITY_IDS:
            score = quality.get(key)
            if not isinstance(score, dict) or (score.get("score") is not None and (type(score.get("score")) is not int or score["score"] not in (1, 2, 3))) or "score" not in score or not isinstance(score.get("reason"), str) or not score["reason"].strip():
                return f"{key} missing or invalid"
        issues = item.get("issues")
        if not isinstance(issues, list) or any(not isinstance(x, dict) for x in issues):
            return "issues missing or invalid"
        for issue in issues:
            if any(not isinstance(issue.get(k), str) or not issue[k].strip() for k in ("primary_dimension", "issue_code", "severity", "problem_span", "reason", "action")) or not isinstance(issue.get("repairable"), bool):
                return "issue fields missing or invalid"
            if any(not isinstance(issue.get(k, []), list) or any(not isinstance(x, str) for x in issue.get(k, [])) for k in ("evidence_ids", "rule_ids")):
                return "issue references invalid"
        guidance = item.get("rewrite_guidance")
        if not isinstance(guidance, dict) or any(not isinstance(guidance.get(k), list) or any(not isinstance(x, str) for x in guidance[k]) for k in ("preserve", "modify", "forbidden")):
            return "rewrite_guidance missing or invalid"
        memory = item.get("memory_query")
        if memory is None and isinstance(item.get("memory_retrieval_query"), str):
            memory = {"problem_types": [], "query": item["memory_retrieval_query"]}
        if not isinstance(memory, dict) or not isinstance(memory.get("problem_types"), list) or any(not isinstance(x, str) for x in memory["problem_types"]) or not isinstance(memory.get("query"), str):
            return "memory_query missing or invalid"
        # limit_checks：限定必答项的形态校验（条目结构合法，verdict 在枚举内）。
        # 覆盖校验（每条 requirement 必须有答案）在 _validate_and_decide_reviews 做，
        # 因为那里才知道每条候选挂着哪些限定。
        checks = item.get("limit_checks", [])
        if not isinstance(checks, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("claim_id"), str)
            or not isinstance(c.get("limit"), str)
            or c.get("verdict") not in ("satisfied", "violated", "unclear")
            or not isinstance(c.get("reason"), str)
            for c in checks
        ):
            return "limit_checks missing or invalid"
    if len(ids) != len(candidate_ids) or len(set(ids)) != len(ids) or set(ids) != set(candidate_ids):
        return "reviews do not cover each requested candidate exactly once"
    return None


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
        non_object_count = 0
        for item in reviews:
            if not isinstance(item, dict):
                non_object_count += 1
                continue
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id not in candidate_ids:
                anomalies.append(f"unknown candidate_id: {candidate_id}")
                continue
            if candidate_id in normalized:
                anomalies.append(f"duplicate review candidate_id: {candidate_id}")
            normalized_item = dict(item)
            if isinstance(normalized_item.get("issues"), list):
                normalized_item["issues"] = [_normalize_issue_fields(issue) for issue in normalized_item["issues"]]
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
        if non_object_count:
            anomalies.append(f"non-object review item: {non_object_count}")
        result = [normalized.get(cid, {"candidate_id": cid, "_normalization_errors": ["review missing for candidate"]}) for cid in candidate_ids]
        for item in result:
            if anomalies:
                item.setdefault("_normalization_errors", []).extend(anomalies)
        return result
    # Missing batch output is represented as incomplete per-candidate results;
    # schema validation will route them to human review instead of passing.
    return [{"candidate_id": cid} for cid in candidate_ids]


# 来源归属的明确表达词：attribution_required 时，标题至少出现其中一个才算「有归属」。
# 「受访」不是口头动词，但在标题里与「自述」同义：它是把后面那句话归给受访者的标记。
# 缺了它，「黄多多首次走红毯受访：画作是自己画的」会被判成没表达归属。
_ATTRIBUTION_MARKERS = ("据", "称", "表示", "透露", "介绍", "报道", "博主", "网友", "网传", "疑似", "回应", "驳斥", "否认", "自述", "显示", "指出", "发布", "曝",
                        "受访", "讲述", "坦言", "回忆", "提到", "补充", "解释",
                        # 转述关系本身就是归属：「黄多多模仿妹妹：…」写明了话是谁转述的。
                        # 只按冒号前长度判会把 7 字前缀当主题短语，误判成缺归属。
                        "模仿", "转述", "复述")


# 载体（视频/旁白/字幕/画面）不是信源。剧情演绎类视频里，唯一"来源"就是视频自己的叙述，
# 此时披露靠【剧情演绎】前缀完成，标题正文不该再写「旁白称」——那既重复又违反 U-S09。
_CARRIER_SOURCE_TOKENS = ("旁白", "字幕", "口播", "画面", "视频", "文案", "标题", "封面", "片子", "原题")
_REAL_SOURCE_TOKENS = ("博主", "作者", "媒体", "警方", "官方", "通报", "机构", "公司", "医院", "学校",
                       "专家", "研究", "报告", "调查", "法院", "政府", "当事人", "家属", "网友")
_NARRATIVE_PREFIX = "剧情演绎"
# 限定里出现这些词就说明它要求的是事实层面的限定（范围/条件/争议），
# 前缀承担不了，必须由正文表达。
_FACTUAL_LIMIT_WORDS = ("争议", "反驳", "未经证实", "说法不一", "数据", "比例", "销量", "价格",
                        "时间", "地点", "人数", "单位", "可能", "仅", "范围", "条件", "前提")


def _declares_narration(prefix_labels: Any) -> bool:
    """标题是否已用【剧情演绎】这类前缀声明了叙述属性。"""
    return _NARRATIVE_PREFIX in {str(x) for x in (prefix_labels or [])}


def _carrier_only_source(phrase: Any) -> bool:
    """归属要求是否只指向载体（旁白/字幕/画面），而不是真实信源。"""
    text = str(phrase or "")
    if not text:
        return False
    if any(tok in text for tok in _REAL_SOURCE_TOKENS):
        return False
    return any(tok in text for tok in _CARRIER_SOURCE_TOKENS)


_PROHIBITION_MARKERS = ("不得", "不要", "不能", "禁止", "避免", "不许", "勿", "严禁")
# 有正向要求词才说明这条限定"必须写出来"；只说"不得…"的是禁令，靠不写来满足。
_POSITIVE_LIMIT_MARKERS = ("须", "应", "需", "必须", "务必")

# 「OCR 数据**对应**关系不明确」里的「应」不是「应当」。裸字匹配会把纯禁令读成正向要求，
# 于是要求标题写出数据口径——而这条限定的本意恰恰是「别把数字对应到行业」。
_POSITIVE_TOKEN_FALSE_FRIENDS = ("对应", "相应", "适应", "反应", "响应", "应急", "应用", "应对", "供应", "响应")

# 「须作为现场救援描述，不得推断出警过程细节」这类：前半的「须作为…描述」只规定了口吻/定位，
# 真正要守的是后半的禁令，靠标题不出现该信息来满足。不剥掉这个框架，
# 「须」会被读成正向要求，标题就必须写出「本条不得推断细节」这种审核话——永远写不出来，候选必死。
_POSITIVE_FRAME_MARKERS = ("须作为", "须按", "须围绕", "须基于", "须依据", "须遵循", "须依照")


def _limit_is_prohibition(limit: Any) -> bool:
    """「ASR含糊，不得补出具体动作细节」这类是禁令，靠标题里不出现该信息来满足。

    把它当成"必须表达出来的限定"，就会要求标题写出「本条不得补出细节」这种审核话，
    机械检查永远判缺限定，候选必死。
    """
    text = str(limit or "").strip()
    if not text:
        return False
    if not any(marker in text for marker in _PROHIBITION_MARKERS):
        return False
    # 「须归属为旁白，不得写成史实定论」这类前半是正向要求，仍算需要表达。
    for word in _POSITIVE_TOKEN_FALSE_FRIENDS:
        text = text.replace(word, "")
    for frame in _POSITIVE_FRAME_MARKERS:
        text = text.replace(frame, "，")
    # 「须避免混淆日期」是加强语气的禁令，不是要求标题写日期；
    # 只消除紧邻禁令的义务词，其他独立正向要求仍必须表达。
    text = re.sub(r"(?:必须|务必|须|应|需)\s*(?:不得|不要|不能|禁止|避免|不许|勿|严禁)", "，", text)
    return not any(marker in text for marker in _POSITIVE_LIMIT_MARKERS)


# 限定要求说的到底是「把话归给谁」还是「范围/条件/不确定性」。两者由不同表达满足，
# 混成一个判断就会出现「写了『自述』却因为没写『不』被判缺限定」这类误杀。
_ATTRIBUTION_LIMIT_TOKENS = ("归属", "信源", "来源", "转述", "自述", "称")

# 「须保留活动名称与地点」里的「名**称**」是名词，不是归属动词。裸 token 匹配会把这类
# 限定误判成归属类，进而要求标题出现信源——而标题老老实实保留了活动名，本已合规。
# 对照实验（2026-11-02，compare_v2）里「古力娜扎…星光之夜现场记录」就是这么被判否的。
_ATTRIBUTION_TOKEN_FALSE_FRIENDS = ("名称", "简称", "称号", "称呼", "称作", "对称", "俗称")


def _limit_is_attribution(limit: Any) -> bool:
    text = str(limit or "")
    for word in _ATTRIBUTION_TOKEN_FALSE_FRIENDS:
        text = text.replace(word, "")
    return any(token in text for token in _ATTRIBUTION_LIMIT_TOKENS)


# 「可写司机昏迷斜靠座椅，但病因须归属」——归属要求只针对「病因」这一个子事实。
# 标题里根本没提病因，就没有归属义务；把整条限定当标题级要求，等于凭空多一条必死约束。
_ATTRIBUTION_SCOPED_PATTERN = re.compile(r"[，,；;但而]([^，,；;但而]{2,8})(?:须|应|需)归属")


def _limit_attribution_scope_absent(limit: Any, headline: str) -> bool:
    """归属限定绑定的子事实不在标题里：这条归属要求不适用。"""
    for match in _ATTRIBUTION_SCOPED_PATTERN.finditer(str(limit or "")):
        subject = match.group(1).strip()
        if subject and subject not in headline:
            return True
    return False


def _limit_needs_body(limit: Any, prefix_labels: Any, headline: str = "") -> bool:
    """这条限定是否必须由标题正文表达。

    ``headline`` 用来判断「写作指引型限定」是否已经照做——不传时按「必须表达」处理，
    宁可多拦也不放过，避免静默放宽。
    """
    if (_limit_is_prohibition(limit) or _limit_covered_by_narration(limit, prefix_labels)
            or _conditional_duration_absent(limit, headline)):
        return False
    # 仅识别完整的日期区分事实，不扩展到带额外义务的复合限定。
    # 标题没有日期时无需强加报道日期；有日期则继续按原路径并保留语义待核。
    date_distinction = re.fullmatch(
        r"\s*\d{1,2}月\d{1,2}日为报道日期[，,]非事件发生日期[。.]?\s*", str(limit or "")
    )
    date_in_headline = re.search(
        r"\d|[〇零一二三四五六七八九十]{1,4}[年月日号]|今年|去年|前年|明年|今天|昨日|昨天|前天|明天|后天|当日|当天|近日|上周|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天]|上个?月|本月|这个月|下个?月", headline
    )
    if date_distinction and not date_in_headline:
        return False
    # 写作指引型限定是「标题该写什么」，标题照它写就已经满足；不能再要求正文表达限定，
    # 否则会要求标题写出来源，而标题写的正是被建议写的那个动作。
    if _limit_is_writing_guidance(limit, headline):
        return False
    # 归属类永远要表达：它说的就是「把话归给谁」，标题不写就等于把博主说法当成了事实。
    if _limit_is_attribution(limit):
        return True
    return not _is_retention_only_limit(limit)


# 「不得写死用量/时长」这类数值禁令，靠标题不出现具体量值来满足。
# 但断言词禁令（「不得写成已证实」「不得写成死亡」）同义改写仍可能违反，不能这样放开。
_NUMERIC_LIMIT_MARKERS = ("用量", "份量", "分量", "时长", "价格", "重量", "温度", "数量", "克数", "毫升")
_NUMERIC_IN_TITLE = re.compile(r"\d|[一二两三四五六七八九十百千万半]+\s*(?:勺|克|斤|两|毫升|分钟|小时|度|元|块|次|份)")


def _numeric_prohibition_absent(limit: Any, headline: str) -> bool:
    """数值类禁令 + 标题无具体量值 = 已满足，不必进待核。

    只认「禁令 + 数值维度」这一种组合：标题里没有任何数字或量词，
    就不可能写出被禁的用量、时长、价格。「不得写成已证实」这类词面禁令不在此列。
    """
    text = str(limit or "")
    if not _limit_is_prohibition(text):
        return False
    if not any(marker in text for marker in _NUMERIC_LIMIT_MARKERS):
        return False
    return not _NUMERIC_IN_TITLE.search(str(headline or ""))


# 边界的限定有两种写法：一种是「标题必须补上什么」，另一种是「标题该写什么」。
# 后一种（「标题可写画面可见的看球动作」「宜写为转身面向电脑等可观察动作」）是给生成器的
# 改法指引，标题照它写就已经满足；过去一律按「必须表达限定」处理，于是要求标题在正文里
# 写出来源——而标题写的正是被建议写的那个动作，形成必死的约束。
# 真实故障（2026-11-03 Evolink 复测）：正常新闻样本三条候选全被判 G1_MISSING_LIMIT，
# 人工判定三条全部可用。
_GUIDANCE_MARKERS = ("标题可写", "可写", "宜写", "应写", "建议写", "写成", "写为", "改为")
_QUOTED_SPAN = re.compile(r"[“\"「]([^”\"」]{1,30})[”\"」]")


def _limit_is_writing_guidance(limit: Any, headline: str) -> bool:
    """限定是在给标题改法，且标题没有落在它否定的那个子事实上。"""
    text = str(limit or "")
    if not any(marker in text for marker in _GUIDANCE_MARKERS):
        return False
    if not headline:
        return False
    # 引号里是被否定的子事实；标题若写了它，说明没照做，仍按必须表达处理。
    rejected_spans = [m.group(1) for m in _QUOTED_SPAN.finditer(text)]
    return bool(rejected_spans) and not any(span and span in headline for span in rejected_spans)


# 「须保留 X」「限定为 X」「须以 X 为准」要求的是「别改写、别夸大」——标题照材料原样
# 保留就已经满足，机器却要求它把测试条件、未引用的数值口径复述进标题。标题做不到，
# 于是判定永远缺限定。2026-11-02 对照实验里「央视财经六字解读…」「博主实测Neo12…」
# 「古力娜扎…星光之夜现场记录」都栽在这里，而人工判定它们是可用的。
_NON_EXPRESSIVE_LIMIT_MARKERS = ("须保留", "保留为", "须保持", "限定为", "须以", "为准", "不得直接引用")


def _is_retention_only_limit(limit: Any) -> bool:
    """只在「别弄错」层面约束、不要求标题额外写出内容的限定。"""
    return any(marker in str(limit or "") for marker in _NON_EXPRESSIVE_LIMIT_MARKERS)


# 范围/条件类限定是否已落地，过去靠一张全局词表（据/称/可能/不/未…）判断：其中「不」「未」
# 几乎任何标题都命中，而「据」「称」又是归属词——结果是该拦的没拦、该放的没放。
# 现在改成「限定文本里的可核对锚点是否出现在标题里」，抽不出锚点就放行交给评审。
_LIMIT_SCOPE_WORDS = (
    "上半年", "下半年", "全年", "今年", "去年", "明年", "本季度", "当季", "截至", "以来",
    "以内", "以上", "以下", "之前", "之后", "期间", "阶段", "部分地区", "部分", "个别",
    "某些", "特定", "仅限", "背景下", "前提下", "条件下", "范围内", "口径", "版本", "型号",
)
_LIMIT_UNCERTAINTY_MARKERS = (
    "可能", "疑似", "据说", "网传", "尚未", "未经", "约", "左右", "大约", "预计", "或将",
    "部分", "个别", "有观点认为", "一种说法", "存在争议", "未证实", "未获证实",
)
_LIMIT_ANCHOR_FILLER = ("须保留", "须保持", "保留为", "限定为", "须以", "为准", "不得", "必须", "须", "应")


_LIMIT_RETAIN_HINTS = ("须保留", "需保留", "应保留", "保留为", "保留", "须保持", "须延续", "须沿用")


def _conditional_duration_absent(limit: Any, headline: str) -> bool:
    """仅豁免明确以「标题写时长」为条件且标题未表达时长的约束。"""
    text = str(limit or "")
    conditional = re.search(r"(?:标题若写|若标题写|标题(?:如|如果)写|(?:如|如果)标题写)(?:具体)?(?:步骤)?时长", text)
    if not conditional:
        conditional = re.search(r"标题若写(?=须|需|应|必须)", text)
        if not conditional or not re.search(r"(?:步骤)?时长", text[:conditional.start()]):
            return False
    # 不将独立的必须披露义务一并豁免。
    before = text[:conditional.start()]
    if re.search(r"(?:必须|须|应|需要|需)(?:写明|注明|标明|说明|保留|表达)", before):
        return False
    if re.search(r"[；;，,](?:另|并|同时|还)(?:必须|须|应|需要|需)(?:写明|注明|标明|说明|保留|表达)|[；;](?:必须|须|应|需要|需)(?:写明|注明|标明|说明|保留|表达)", text[conditional.end():]):
        return False
    duration = re.search(
        r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百半几数来多]+)\s*(?:秒|分钟|分(?!钟)|小时|天|日|周|星期|个月|月|年)"
        r"|时长|耗时|用时|多久|顷刻|瞬间|转眼|片刻|一会儿|一会|很快|快速|慢炖|久煮|煮至", headline
    )
    return not bool(duration)


_CONDITIONAL_MARKERS = re.compile(r"(?:若|如果|如)(?:标题|片中|视频)?(?:写|写到|涉及|沿用|出现|提到)|标题(?:写|写到|涉及|沿用|出现|提到)时")
_QUOTED_SPAN_ANY = re.compile(r"[“\"「『]([^”\"」』]{2,30})[”\"」』]")
# 条件已成立时可用来证明「要求已照做」的锚点；只收具体限定词，不收泛化虚词。
_CONDITIONAL_ANCHORS = ("此次", "当天", "当日", "当年", "去年", "今年", "早年", "事后", "近期",
                        "目前", "还需进一步核实", "仍需核实", "快讯", "报道称", "原文称", "博主称",
                        "博主认为", "博主提醒", "谭乔", "交警部门表示")
_ATTRIBUTION_VERBS = ("发帖称", "发帖表示", "发帖预测", "自述", "坦言", "回忆", "讲述", "指出", "提醒",
                      "说明", "透露", "回应", "解释", "证实", "确认", "澄清", "承认", "报道", "发布",
                      "显示", "认为", "表示", "称", "说")
_FUNCTION_CHARS = set("的了和与及或是在为其之这那就也都而并且如若时写标题涉及沿用出现提到须应与保留同现不得成")


def _content_bigrams(text: str) -> set[str]:
    """两字滑窗：用来判断引号里的触发条件是否真的出现在标题里。

    只用两字组而不做分词，是因为这里要回答「能不能证明条件没被触发」。
    证据不够时应当保持待核，所以只要有任何内容两字组重合，就算条件可能成立。
    """
    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
    grams = {clean[i:i + 2] for i in range(len(clean) - 1)}
    return {g for g in grams if not all(ch in _FUNCTION_CHARS for ch in g)}


def _conditional_trigger_absent(limit: Any, headline: str) -> bool:
    """能证明「标题没写这个触发条件」时才判不适用。

    只对「若标题写『X』…」这类引号条件生效：引号里的 X 与标题没有任何内容重合，
    才说明这条约束的前提不成立。条件本身没有引号（「若标题涉及具体处置情况」）
    无法字面证伪，一律保持待核——那类正是最不能机械放行的。
    """
    text = str(limit or "")
    if not _CONDITIONAL_MARKERS.search(text):
        return False
    clause = re.split(r"[，,；;]", text, maxsplit=1)[0]
    quoted = [q for q in _QUOTED_SPAN_ANY.findall(clause) if q]
    if not quoted:
        return False
    title_grams = _content_bigrams(headline)
    for trigger in quoted:
        if trigger in headline or (_content_bigrams(trigger) & title_grams):
            return False
    return True


_LIMIT_NAME_SUFFIXES = ("本人", "个人", "主观", "的", "所说", "自述", "观点", "态度", "表态", "评价", "说法",
                        "叙述", "口播", "描述")


def _limit_source_names(text: str) -> list[str]:
    """从「归属为萨戈夫本人的表态」「如「博主称」」里抽出应该出现在标题里的信源名。"""
    names: list[str] = []
    match = re.search(r"归属(?:为|于)?\s*([^\s，,；;。「」“”]{2,18})", str(text or ""))
    if match:
        raw = match.group(1)
        for suffix in _LIMIT_NAME_SUFFIXES:
            raw = raw.split(suffix)[0]
        if 2 <= len(raw) <= 8:
            names.append(raw)
    for quoted in _QUOTED_SPAN_ANY.findall(str(text or "")):
        base = quoted.strip()
        for verb in _ATTRIBUTION_VERBS:
            if base.endswith(verb) and len(base) > len(verb):
                base = base[: -len(verb)]
                break
        base = base.replace("/", "").strip()
        if 2 <= len(base) <= 8:
            names.append(base)
    return list(dict.fromkeys(names))


def _conditional_satisfied(limit: Any, headline: str) -> bool:
    """条件成立时，能不能字面证明标题已按约束写。

    只认三种可核对证据：引号要求原样出现、具体限定锚点出现、冒号式归属。
    都证明不了就返回 False，由调用方保留待核。
    """
    text = str(limit or "")
    if any(q and q in headline for q in _QUOTED_SPAN_ANY.findall(text)):
        return True
    if any(anchor in text and anchor in headline for anchor in _CONDITIONAL_ANCHORS):
        return True
    if "归属" in text:
        verbs = "|".join(_ATTRIBUTION_VERBS)
        for name in _limit_source_names(text):
            if re.search(rf"{re.escape(name)}[^，,。；;：:与和及]{{0,6}}(?:{verbs}|[:：])", headline):
                return True
    return False


def _prohibition_hit(limit: Any, headline: str) -> str | None:
    """禁令类限定唯一可判定的形态：被禁词被引号括出、且字面出现在标题里。

    「不得写成"已证实"」+ 标题「事件已证实」→ 命中「已证实」，这是词法真正测得出的违反。
    引号里没写被禁词（如「不得推断中毒」）、或标题改写了同义词——测不出，交评审，
    不连坐。返回命中的词，未命中返回 None。
    """
    text = str(limit or "")
    if not _limit_is_prohibition(text):
        return None
    for quoted in _QUOTED_SPAN_ANY.findall(text):
        if quoted and quoted in headline:
            return quoted
    return None


def _limit_satisfied_by_literal(limit: Any, headline: str) -> bool:
    """保留类/归属类限定能不能字面证明已照做。

    只处理两种低风险形态：「须保留『X』」且 X 原样在标题里；「须保留 X 归属」且主体在标题里。
    其余（含禁令、含推断要求的复合限定）一律返回 False，继续保持待核。
    """
    text = str(limit or "")
    quoted = [q for q in _QUOTED_SPAN_ANY.findall(text) if q]
    if _is_retention_only_limit(text) and quoted:
        # 「须保留 X」是「写了别写错」：标题完全没写 X 时保留义务不成立。
        # 但 X 已经出现在标题里时仍需人看——同一条限定可能还带着别的约束。
        return not any(q in headline for q in quoted)
    if _limit_is_attribution(text):
        verbs = "|".join(_ATTRIBUTION_VERBS)
        for name in _limit_source_names(text) or [_subject_of(text)]:
            if name and re.search(rf"{re.escape(name)}[^，,。；;：:与和及]{{0,6}}(?:{verbs}|[:：])", headline):
                return True
    return False


def _limit_key(item: Any) -> tuple[str, str]:
    """限定的身份：同一条 claim 下的同一条限定文本。"""
    if not isinstance(item, dict):
        return ("", "")
    return (str(item.get("claim_id") or ""), str(item.get("limit") or ""))


def _limit_check_coverage_errors(review: Any, mechanical: Any) -> list[str]:
    """机械层登记的每条限定都必须有评审判定，缺一条就是「没答」。

    与 schema 校验不同，这里知道每条候选挂着哪些 requirement（在 mechanical 里），
    所以覆盖校验只能放这一层。输出是 validation_error 文案，直接进已有的单条重问通道。
    """
    requirements = [r for r in ((mechanical or {}).get("limit_requirements") or []) if isinstance(r, dict)]
    if not requirements:
        return []
    answered = _limit_check_verdicts(review)
    gaps = [r for r in requirements if _limit_key(r) not in answered]
    return [f"limit_checks 未覆盖全部限定（缺 {len(gaps)}/{len(requirements)} 条，如「{str(gaps[0].get('limit'))[:40]}」）须逐条给出 satisfied/violated/unclear"] if gaps else []


def _limit_check_verdicts(review: Any) -> dict[tuple[str, str], str]:
    """评审对每条限定的判定；只认三个枚举值，其余视为没答。"""
    verdicts: dict[tuple[str, str], str] = {}
    for item in ((review or {}).get("limit_checks") or []) if isinstance(review, dict) else []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "")
        if verdict in {"satisfied", "violated", "unclear"}:
            key = _limit_key(item)
            # 同一限定出现冲突判定时不能让末项覆盖前项并放行。
            if key not in verdicts:
                verdicts[key] = verdict
            elif verdicts[key] != verdict:
                verdicts[key] = "unclear"
    return verdicts


def _limit_pending_reason(limit: Any, headline: str) -> str:
    """待核原因要能分组：评审台按原因批量处理，比逐条重读快得多。

    先按限定性质分类，再看落点。反过来的话，「不得写成确定动作名」会显示成
    「标题未出现落点：确定动作名」——要求是「别写」，不是「写上」，那是误导。
    """
    text = str(limit or "")
    if _limit_is_attribution(text):
        return "归属类限定：需确认标题是否归给了对的人"
    if _limit_is_prohibition(text):
        return "禁止类限定：需确认标题没有写出被禁内容"
    anchors = _limit_anchors(text)
    if anchors:
        missing = [a for a in anchors if a not in headline]
        if missing:
            return f"标题未出现限定要求的落点：{'、'.join(missing[:3])}"
    return "限定没有任何可核对落点：需人工判断是否适用"


def _limit_quoted_in_headline(limit: Any, headline: str) -> bool:
    """限定里引号包的原文是否真的出现在标题里。

    这是可核对的：原文逐字在标题里出现，就不是「语义判不了」，没必要再进待核队列。
    """
    for quoted in re.findall(r"[\"“「『']([^\"”」』']{2,20})[\"”」』']", str(limit or "")):
        text = quoted.strip()
        if text and text in headline:
            return True
    return False


def _limit_anchors(limit: Any) -> list[str]:
    """从限定文本里抽出「标题里应该出现」的可核对锚点：引号词、数字、范围/口径词。"""
    text = str(limit or "")
    anchors: list[str] = []
    for quoted in re.findall(r"[“\"「『]([^”\"」』]{2,20})[”\"」』]", text):
        anchors.extend(part.strip() for part in re.split(r"[/、，,]", quoted) if part.strip())
    anchors.extend(re.findall(r"\d+(?:\.\d+)?%?", text))
    anchors.extend(word for word in _LIMIT_SCOPE_WORDS if word in text)
    cleaned = []
    for anchor in anchors:
        if any(filler in anchor for filler in _LIMIT_ANCHOR_FILLER):
            continue
        cleaned.append(anchor)
    return list(dict.fromkeys(cleaned))


# 限定文本说的是哪一类，就该由哪一类表达来满足：
#   时间/范围类 → 标题要有年份、月份、期间、截至这类时间落点；
#   不确定性类 → 标题要有「未经证实/传闻/存在争议」这类标记；
#   条件类     → 标题要有「情况下/前提下/仅/部分」这类范围词；
#   数值口径类 → 标题要有数字或「约/左右/以上」这类量级词。
# 把这几类混成一张全局词表，就会出现「写了『据称』却被判缺限定」和「写了『不』就全过」两种错。
_LIMIT_CATEGORY_RULES = (
    ("time", ("时间范围", "时间", "范围", "期间", "时长", "日期", "年份", "月份", "季度", "截至"),
     ("年", "月", "日", "季度", "上半年", "下半年", "至今", "截至", "期间", "以来", "近期", "最新", "当时", "此前")),
    ("uncertainty", ("未经证实", "未证实", "传闻", "尚未", "未获", "说法", "推测", "未经核实", "存在争议", "尚存疑"),
     _LIMIT_UNCERTAINTY_MARKERS),
    ("condition", ("条件", "前提", "背景", "情境", "适用于", "针对"),
     ("情况下", "前提下", "条件下", "背景下", "如果", "若", "仅", "只", "部分", "个别", "针对", "适用于")),
    ("quantity", ("口径", "统计", "数据", "样本", "比例", "数值"),
     ("约", "左右", "近", "超过", "不足", "以上", "以下", "倍", "成")),
)


def _limit_is_expressed(limit: Any, headline: str) -> bool:
    """范围/条件/不确定性类限定是否已经落进标题正文。

    先按限定语义分类，用该类对应的表达词表判断；认不出类别的才退回锚点比对，
    锚点也抽不出来时放行——机械判不了的事交给评审，但不能因此放过已知类别。
    """
    text = str(limit or "")
    # 引号里是被限制的那段原文（ASR 原词、「约三分钟」）。两种情况：
    #  - 原文已经进了标题：限定落实了，放行（「约三分钟」写全了就不该判缺）；
    #  - 原文没进标题：要求「标注/写明」的没有标注义务，要求「不得写」的也没违约；
    #    只有「须保留 X」还要求标题必须留住这段原文，没留住才算缺。
    # 真实误杀：「ASR『攻艦步』须标注为转写推断」判否了标题根本没提这个动作的候选。
    quoted = [q.strip() for q in re.findall(r"[\"“「『']([^\"”」』']{2,20})[\"”」』']", text) if q.strip()]
    if quoted:
        if any(q in headline for q in quoted):
            return True
        return not any(hint in text for hint in _LIMIT_RETAIN_HINTS)
    for name, triggers, markers in _LIMIT_CATEGORY_RULES:
        if not any(trigger in text for trigger in triggers):
            continue
        if any(marker in headline for marker in markers):
            return True
        if name in {"time", "quantity"} and re.search(r"\d", headline):
            return True
        return False
    anchors = _limit_anchors(text)
    if not anchors:
        return True
    return any(anchor in headline for anchor in anchors)




def _limit_covered_by_narration(limit: Any, prefix_labels: Any) -> bool:
    """「须归属为旁白/剧情叙述」这类限定：前缀已声明，正文不必再写。"""
    if not _declares_narration(prefix_labels):
        return False
    text = str(limit or "")
    if not any(tok in text for tok in _CARRIER_SOURCE_TOKENS) and "剧情" not in text and "叙述" not in text:
        return False
    return not any(word in text for word in _FACTUAL_LIMIT_WORDS)


_NATURE_DISCLOSURE_PREFIXES = ("剧情演绎", "AI生成")


def _nature_disclosure_applied(state: dict[str, Any]) -> bool:
    """内容性质信号（游戏画面/演绎/AI）一旦由平台前缀向用户声明，就已经处置完毕。

    这类信号常常只来自模型自己写的画面描述（model_derived_uncalibrated）。
    既按它加了【剧情演绎】前缀，又拿它阻断交付，等于让同一份不可核验的描述
    既当披露依据又当停止依据——前缀已经是这套规则给用户的答案。
    """
    prefixes = {str(x) for x in (state.get("required_prefixes") or [])}
    pending = list((state.get("risk_signals") or {}).get("pending_signal_ids") or [])
    signals = list((state.get("risk_signals") or {}).get("signals") or [])
    pending_signals = [s for s in signals if s.get("signal_id") in pending]
    if not pending_signals:
        return False
    if not prefixes.intersection(_NATURE_DISCLOSURE_PREFIXES):
        return False
    return all(s.get("signal_type") in {"simulation", "fictional", "ai_generated"} for s in pending_signals)


# 「据」和「称」是单字标记，裸字匹配会把名词读成信源：「数据」「证据」「根据」「名称」
# 都能让一个没有任何归属的标题被判成"已表达归属"。这个方向的错误是漏放——把博主说法
# 当成事实放行，比误杀更贵，所以在匹配前先剥掉这些复合词。
# （2026-11-02 扫过 compare_v2b 全批 40 条，命中 0 条；属于防御性修复，不影响该批结论。）
_ATTRIBUTION_MARKER_FALSE_FRIENDS = (
    "数据", "证据", "根据", "依据", "票据", "收据", "单据", "占据",
    "名称", "简称", "称号", "称呼", "称作", "对称", "俗称", "称重",
)


def _strip_attribution_false_friends(text: str) -> str:
    for word in _ATTRIBUTION_MARKER_FALSE_FRIENDS:
        text = text.replace(word, "")
    return text


# 冒号式归属：标题最省字、最常用的写法是「主体：内容」——「萨戈夫：德文是传奇」
# 「作者：财经媒体误导性强」。裸 token 表认不出这种结构，却有反例：错误标题
# 「赢过德文一次，萨戈夫为何仍称他传奇」因为句中有个「称」被判「已归属」。结果是
# 写对的被判缺归属、写错的被判已归属（2026-11-03 复测 8 条误拦的根因，人工全判可用）。
_COLON_SOURCE = re.compile(r"([^：:，,。！？!?]{1,12})[：:]")
# 「须归属为博主自述，不得写成系统确认的普遍适用结论」里的主体是「博主自述」。
_ATTRIBUTION_SUBJECT = re.compile(r"归属(?:为|于)?\s*([^，,；;。]{1,12})")
# 冒号前必须是「谁在说」，不能是「发生了什么」：「黄多多首次走红毯：紧张到出来才好」的
# 前半句是事件，读者从中得不到信源；把它当主体会把真缺归属的标题放过去（既有测试锁住了
# 这一行为）。出现谓词/事件标记就回落 token 判定，宁可继续要求归属也不误放。
_SUBJECT_DISQUALIFIERS = ("首次", "走", "到", "被", "在", "了", "将", "正", "已", "发生", "现身",
                          "亮相", "出席", "参加", "获得", "夺", "击败", "赢", "输", "开", "拍",
                          "做", "吃", "买", "卖", "称", "表示", "回应", "发帖")
# 泛称不是信源：读者看完不知道是谁说的，仍算缺归属。「通报：」由上面的分支单独处理。
_GENERIC_SOURCE_LABELS = ("通报", "有消息", "据了解", "据称", "据悉", "消息", "相关", "有关",
                          "知情人士", "内部人士", "外界", "舆论", "网传")
# 把「萨戈夫称」「博主自述」「作者认为」还原成主体本身，用来核对标题归给了谁。
_SOURCE_PHRASE_TAILS = ("发帖称", "发帖表示", "发帖预测", "受访", "自述", "坦言", "回忆", "讲述", "指出", "提醒",
                        "说明", "透露", "回应", "解释", "报道", "发布", "显示", "认为", "表示", "证实",
                        "确认", "澄清", "承认", "预测", "称")


def _subject_of(phrase: Any) -> str:
    """从「萨戈夫称」「博主自述」「须归属为博主自述，不得…」里抽出要求归给的主体。

    抽不出主体就返回空串，由调用方跳过——把整句限定当成主体名会凭空多出一条必死约束。
    """
    text = str(phrase or "").strip()
    match = _ATTRIBUTION_SUBJECT.search(text)
    if match:
        text = match.group(1).strip()
    elif len(text) > 12 or "，" in text or "," in text:
        return ""
    for tail in _SOURCE_PHRASE_TAILS:
        if text.endswith(tail) and len(text) > len(tail):
            text = text[: -len(tail)].strip()
            break
    return text if len(text) <= 12 else ""


def _attribution_subject_present(headline: str, phrases: Any) -> bool:
    """边界要求归给谁，标题就得出现谁。

    抽不出主体（如只有「须归属」这类泛化要求）时不额外拦截，交给上层判定，
    避免把没有具体归属对象的限定变成一条必死约束。
    """
    for phrase in phrases or []:
        alternatives = [part.strip() for part in re.split(r"[/／]", str(phrase or "")) if part.strip()]
        def matches(part: str) -> bool:
            # 只认能解析的具体主体；归属动词必须与该主体同一短语，不能借用别人的「称」。
            part = re.split(r"[，,；;]", part, maxsplit=1)[0].strip()
            if part in {"报道称", "据报道", "媒体报道", "媒体称"}:
                return part in headline
            subject = _subject_of(part)
            if not subject:
                return False
            escaped = re.escape(subject.removeprefix("据"))
            # 主体与归属动词之间只允许不含标点、不含并列连词的短状语（「萨戈夫赛后称」「博主回应称」）。
            # 不允许跨句借用别的信源的归属动词：「警方抓人，网友称…」里的「称」属于网友。
            verbs = "|".join(_ATTRIBUTION_VERBS)
            return bool(re.search(
                rf"(?:据)?{escaped}[^，,。；;：:与和及]{{0,4}}(?:{verbs}|[:：])", headline))
        if alternatives and not any(matches(part) for part in alternatives):
            return False
    return True


def _has_explicit_source_relation(headline: str, required_phrases: Any = None) -> bool:
    """标题是否把话归给了该归的人。

    ``required_phrases`` 是边界给出的具体归属要求（「萨戈夫称」「博主自述」）。给了就必须
    归给那个主体——只看「有没有任何归属表达」是不够的：「女生居家一周四练day1：哑铃练背」
    有冒号却没有博主，而它要的正是博主自述（既有测试锁住了这一条）。
    """
    text = _strip_attribution_false_friends(str(headline).strip())
    if required_phrases is not None:
        if not _attribution_subject_present(text, required_phrases):
            return False
        # 具体归属要求已经带着主体与归属动词（或冒号）核对过，本身就是归属证据：
        # 「谭乔证实…」不需要再额外出现「据/称/表示」这些泛化标记。
        if any(_subject_of(part) or part in {"报道称", "据报道", "媒体报道", "媒体称"}
               for phrase in required_phrases
               for part in re.split(r"[/／]", str(phrase or ""))):
            return True
    if text.startswith(("通报：", "通报:", "通报称")):
        return False
    if "通报：" in text or "通报:" in text:
        prefix = text.split("通报", 1)[0]
        return any(word in prefix for word in ("据", "称", "显示", "指出", "发布"))
    if "通报" in text:
        return any(word in text for word in ("据", "称", "显示", "指出", "发布"))
    # 冒号前是具体主体（专名、作者、博主、机构）即完成归属；泛称与载体（视频/画面/字幕）
    # 不算信源，落入下面的 token 判定继续要求明确归属表达。
    colon = _COLON_SOURCE.search(text)
    if colon:
        prefix = colon.group(1).strip()
        event_like = any(word in prefix for word in _SUBJECT_DISQUALIFIERS)
        generic = any(word in prefix for word in _GENERIC_SOURCE_LABELS + _CARRIER_SOURCE_TOKENS)
        # 主体要短：人名 2-4 字、带「赛后/现场」的 5-6 字；「女生居家一周四练day1」这种
        # 主题短语不是信源，放了它等于让真缺归属的标题过关（既有测试锁住了这一条）。
        named = any(word in prefix for word in _REAL_SOURCE_TOKENS) or len(prefix) <= 6
        if prefix and not event_like and not generic and named:
            return True
    # 其它信源同样要有明确归属表达，不能因不含「通报」就默认已归属。
    return any(word in text for word in _ATTRIBUTION_MARKERS)


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


# 载体元话语：标题把「视频/画面/标题/封面」当主语或信源（"视频称""画面却是""标题说""据视频所述"），
# 这是审核/开发视角，不是用户视角。
def _attribution_identity_present(headline: str, required_phrases: Any) -> bool:
    """程序旗标兜底：主体词/身份称谓/性质前缀出现即算归属已表达，不要求转述动词。

    _has_explicit_source_relation 要「主体+称/：」的转述形式，实测把
    「商业观察：博主拆解小包装…」也判缺（2026-11-04 v2，小包装 9/9 候选被
    programmatic_g1_flags 强判 revise，压过评审模型的 pass，而人工对这些标题判可用）。
    人评口径是「读者会不会把它当官方信息」：主体已出现、或标题自带内容性质框架，
    读者就知道这是谁的观察。纯断言标题（没有任何身份词/性质词）仍然返回 False，旗标照打。
    """
    text = str(headline or "")
    for phrase in (required_phrases or []):
        for part in re.split(r"[/／]", str(phrase or "")):
            subject = _subject_of(part) or part
            if subject and (subject in text or subject.lstrip("据") in text):
                return True
    return bool(_ATTRIBUTION_IDENTITY_RE.search(text))


_META_PHRASE_RE = re.compile(
    # 冒号式（「旁白：」「字幕：」）和「旁白讲」同样是载体元话语，早先只匹配「旁白称」这类带动词的说法，
    # 于是「旁白：狙击手装焦尸」既逃过元话语拦截、又被 G1 判成"没表达归属"，两头都错。
    # 「标/标注/注」是 V 集人工评审暴露的新写法：「博主路侧拍环广西赛段 字幕标车手秒差」
    # ——把字幕当信息源报数据，人工 2 条同理由拒绝，旧词表只认「字幕称/字幕：」漏拦。
    r"(?:视频|画面|标题|封面|片子|原题|字幕|口播|旁白|文案)(?:里|中)?(?:称|说|讲|提到|显示|写着|只见|却是|透露|表示|标注?|[:：])"
    r"|据(?:视频|画面|标题|封面|原题|字幕|片子|旁白|口播)"
    r"|作者称"
)
# 制作者动作：把博主的剪辑/制作行为或制作者身份写进标题（V 集人工 rejected 2 条、
# 同任务复测又 2 条同理由拒绝——「制作者放进标题感觉怪」「博主自评封神很奇怪」，
# 人工同类证据 4 条，唯一 approved 反例是「博主：X合集」归属前缀）。注意边界：
# 「博主：X合集」（approved）是归属前缀 + 内容宾语，不在此列；
# 「博主把X剪成合集」「博主合集：X」「博主自评」（rejected）把制作动作/身份摆进正文。
_CREATOR_ACTION_RE = re.compile(
    r"博主.{0,12}?(?:剪成|剪出|剪了|剪辑成?|把.{0,16}剪)"
    r"|^博主(?:合集|剪辑)[:：]?"
    r"|博主自评"
)
# 校验结论：标题写「与…不符/不一致」是评审层的话，不该出现在成品标题。
_VERIFICATION_CONCLUSION_RE = re.compile(r"与[^，。！？]{0,12}(?:不符|不一致)")
# 归属线索词：出现任一即视为标题已给出「这是谁的观察/演示」的信号。
# 小包装实测（2026-11-04 v2）：标题写「商业观察：博主拆解…」评审仍 9/9 报
# G1_SOURCE_ATTRIBUTION_MISSING——模型把 required_source_phrase 当字面关键词，
# 没写「博主称」就不认。程序先做字面预判，把结论交给评审当事实输入。
_ATTRIBUTION_IDENTITY_RE = re.compile(
    r"博主|UP主|Up主|up主|主理人|作者|本人|商业观察|新闻评论|消费观察|行业观察|观察|解读|测评|实测|演示|教学|教程|拆解|体验|探店|自述|亲测|分享"
)


def _title_grams(text: str) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", text or "")
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _title_similarity(a: str, b: str) -> float:
    ga, gb = _title_grams(a), _title_grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _dedupe_similar_candidates(candidates: list[dict[str, Any]], threshold: float = 0.8) -> list[dict[str, Any]]:
    """同一视频的候选要「明显不同」：2-gram Jaccard 过高的后出候选直接丢，不送去评审。"""
    kept: list[dict[str, Any]] = []
    for c in candidates:
        if any(_title_similarity(str(c.get("headline") or ""), str(k.get("headline") or "")) >= threshold for k in kept):
            continue
        kept.append(c)
    return kept


def _unit_asr_span(unit: SemanticUnit, claim_refs: dict[str, list[str]], segments: list[dict[str, Any]]) -> tuple[float, float] | None:
    """用单元的 claim→ASR 证据反推它覆盖的时间段。单元自身 time_range 常为空。"""
    indexes: list[int] = []
    for cid in unit.claim_ids or []:
        for ref in claim_refs.get(cid, []):
            match = re.match(r"e-asr-(\d+)$", str(ref))
            if match:
                indexes.append(int(match.group(1)))
    indexes = [i for i in indexes if 0 <= i < len(segments)]
    if not indexes:
        return None
    start = segments[min(indexes)].get("start")
    end = segments[max(indexes)].get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    return float(start), float(end)


def _enforce_single_mainline(contract: dict[str, Any], understanding: Understanding, video: Any) -> list[dict[str, Any]]:
    """多段并列的视频只能有一个主线：取开头那一段，其余核心单元降级。

    短视频的承诺点在开头；把「合集」当主旨时，后面每段都会被当成并列核心，
    core_hook 槽就会拿尾段最猎奇的那个去生成标题（实测：装死视频出了铁锹绑RPG）。
    只在核心单元明显跨段（起点相差 ≥ 8 秒）时才动手，同一段落内的细分不碰。
    """
    core_ids = [str(x) for x in (contract.get("core_unit_ids") or [])]
    if len(core_ids) < 2:
        return []
    units = {unit.unit_id: unit for unit in understanding.semantic_units}
    claim_refs = {claim.claim_id: list(claim.evidence_refs) for claim in understanding.claims}
    segments = list(getattr(video, "asr_segments", None) or [])
    if not segments:
        return []
    spans: list[tuple[str, float, float, str]] = []
    for uid in core_ids:
        unit = units.get(uid)
        if unit is None:
            continue
        span = _unit_asr_span(unit, claim_refs, segments)
        if span is None:
            continue
        spans.append((uid, span[0], span[1], str(unit.kind or "")))
    # 缺少 ASR 时间不能证明单元不是核心；可能来自画面、OCR 或其他一手材料。
    if len(spans) != len(core_ids):
        return []
    if len(spans) < 2:
        return []
    # 时间间距只能辅助识别互不相干的叙事，不能证明教程的步骤和评价是不同主线。
    # 模型标为核心的观点、评价等保留在契约中，是否兑现标题承诺交给 G3 审核。
    if any(item[3] not in {"叙事", "narrative"} for item in spans):
        return []
    earliest = min(spans, key=lambda item: item[1])
    if any(item[0] != earliest[0] and item[1] <= earliest[2] for item in spans):
        return []
    far = [item for item in spans if item[1] - earliest[1] >= 8.0]
    if not far:
        return []
    narrative = [item for item in spans if item[3] == "叙事"]
    keep = min(narrative or spans, key=lambda item: item[1])
    demoted = [{"unit_id": item[0], "start": item[1], "end": item[2], "kind": item[3],
                "reason": "主线取开头叙事段，其余段落降为支撑"}
               for item in spans if item[0] != keep[0]]
    contract["core_unit_ids"] = [uid for uid in core_ids if uid not in {d["unit_id"] for d in demoted}]
    contract["demoted_core_units"] = demoted
    return demoted


def _core_hook_is_aligned(candidate: dict[str, Any], highlights: list[Any], contract: dict[str, Any]) -> bool:
    """主看点或被主旨契约明确保留的核心看点；其余交由正常拦截。"""
    def field(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    main = next((h for h in highlights if field(h, "role") == "main"), None)
    if not main or candidate.get("highlight_id") == field(main, "highlight_id"):
        return True
    selected = next((h for h in highlights if field(h, "highlight_id") == candidate.get("highlight_id")), None)
    return bool(selected and field(selected, "relation") == "core"
                and field(selected, "unit_id") in (contract.get("core_unit_ids") or [])
                and field(selected, "thesis_id") == contract.get("thesis_id")
                and contract.get("thesis_id"))


def _unit_id_of(item: Any) -> str:
    """取单元 id：理解域单元与抽帧脚本产出的是对象，存档状态里是 dict，两种都要能读。"""
    if isinstance(item, dict):
        return str(item.get("unit_id") or "")
    return str(getattr(item, "unit_id", "") or "")


def _allowed_unit_ids(understanding: Any, boundary: Any, highlights: Any) -> set[str]:
    """本任务可引用的单元集合：语义单元 + 结构片段 + 边界表态过的单元，再减去明确排除的。

    真实故障（2026-11-03 Evolink 复测）：结构候选绑定的单元名是 turn-2 / axis-1，语义单元
    却叫 su-N，而允许集合只收 semantic_units，于是所有绑结构片段的候选一律被判
    G4_INVALID_REFERENCE「unit_id 不在当前任务范围」——它们指向的恰恰是主看点。同一批里
    「两员工偷看球赛」三条候选、共 4 条被判否，人工全部判可用。
    初评与重评此前各写一套集合构造，分叉又放大了这个问题；这里统一成一个来源。
    """
    units = {_unit_id_of(u) for u in (getattr(understanding, "semantic_units", None) or [])}
    units |= {_unit_id_of(h) for h in (highlights or [])}
    decisions = getattr(boundary, "unit_decisions", None) or []
    units |= {_unit_id_of(d) for d in decisions}
    excluded = {_unit_id_of(d) for d in decisions if str(getattr(d, "decision", "")) == "excluded"}
    return {uid for uid in units if uid} - excluded


def _mechanical_check(candidate: dict[str, Any], *, allowed_claims: set[str], allowed_highlights: set[str], evidence_ids: set[str], max_chars: int, allowed_units: set[str] | None = None, required_prefixes: list[str] | None = None, claim_decisions: list[Any] | None = None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors.extend({"code": "G4_SCHEMA_INVALID", "repairable": True, "reason": error} for error in candidate.get("generation_schema_errors", []))
    required = list(required_prefixes or [])
    prefix_labels = [str(x) for x in (candidate.get("prefix_labels") or [])]
    # 总长度口径：前缀 + 正文一起计入，前缀不豁免（业务方 2026-10-31 定）
    head_body = str(candidate.get("headline") or "")
    rendered = render_labels(prefix_labels) + head_body
    candidate["rendered_title"] = rendered
    if not head_body:
        errors.append({"code": "G4_EMPTY_TITLE", "repairable": True, "reason": "标题为空"})
    if len(rendered) > max_chars:
        prefix_len = len(render_labels(prefix_labels))
        errors.append({"code": "G4_LENGTH_EXCEEDED", "repairable": True, "reason": f"渲染后总长 {len(rendered)} 超过 {max_chars}（前缀 {prefix_len} 字 + 正文 {len(head_body)} 字）"})
    if head_body.rstrip().endswith(("?", "？")):
        errors.append({"code": "G4_ENDING_QUESTION_MARK", "repairable": True, "reason": "标题不能以问号结尾"})
    # 前缀在生成侧就被 normalize_prefix_labels 无条件并进候选，渲染时又由 render_labels
    # 写进标题，所以「缺前缀」在这一层不可达：全库 23 个必填前缀任务、91 条候选、0 次命中。
    # 它只在有人绕过归一化手搓状态时才有意义，因此降级为 warning：留观测，不阻断交付。
    missing_prefixes = [label for label in required if label not in prefix_labels]
    if missing_prefixes:
        errors.append({"code": "G4_PREFIX_MISSING", "repairable": True, "reason": f"候选未携带必填前缀：{'、'.join(missing_prefixes)}"})
    if body_budget(max_chars, required) < MIN_BODY_BUDGET:
        errors.append({"code": "G4_PREFIX_BUDGET_EXHAUSTED", "repairable": False, "reason": f"前缀占用 {len(render_labels(required))} 字，正文预算不足，需要业务决策而非改标题"})
    if not candidate.get("candidate_id") or not candidate.get("claim_ids") or not candidate.get("evidence_ids"):
        errors.append({"code": "G4_REQUIRED_FIELD_MISSING", "repairable": True, "reason": "candidate_id、claim_ids 或 evidence_ids 缺失"})
    cited_claims = [str(x) for x in (candidate.get("claim_ids") or [])]
    known_claims = {str(getattr(d, "claim_id", "")) for d in (claim_decisions or [])}
    # 拆开两种「引用不合法」：id 在任务里根本不存在（生成器幻觉）→ 真错误，拦；
    # id 存在但被边界判了暂缓核验/已排除 → 不是引用错误，是内容约束。V 集人工评审
    # 7 条 INVALID_REFERENCE 全部引用真实存在的暂缓核验 claim，人工 5/6 判可用：
    # 「绑了哪条 claim」是元数据，标题文本有没有把该说法写成断言才是语义问题，
    # 交评审判（机械只登记，评审输入里有 blocked_claims 清单）。
    hallucinated = [x for x in cited_claims if x not in allowed_claims and x not in known_claims]
    blocked_refs = [x for x in cited_claims if x not in allowed_claims and x in known_claims]
    if candidate.get("highlight_id") not in allowed_highlights or hallucinated:
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": f"claim_id 或 highlight_id 不在当前任务范围：{hallucinated or [candidate.get('highlight_id')]}"})
    if blocked_refs:
        warnings.append({"code": "G4_BLOCKED_CLAIM_REF", "reason": f"候选绑定了暂缓核验/已排除的陈述（{'、'.join(blocked_refs)}）；标题是否把这些说法写成断言由评审判断", "blocked_claim_ids": blocked_refs})
    unit_id = candidate.get("unit_id")
    if unit_id and allowed_units is not None and unit_id not in allowed_units:
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": "unit_id 不在当前任务范围"})
    invalid_evidence = [x for x in candidate.get("evidence_ids", []) if x not in evidence_ids]
    if invalid_evidence:
        errors.append({"code": "G4_INVALID_REFERENCE", "repairable": True, "reason": f"无效 evidence_id: {invalid_evidence}"})
    # 元信息表述：标题要陈述视频内容本身，不能写成「视频称/画面却是/标题说/据视频所述……」这类描述载体怎么说的框架
    meta_hit = _META_PHRASE_RE.search(head_body)
    if meta_hit:
        errors.append({"code": "G4_META_PHRASE", "repairable": True, "reason": f"标题含元信息表述「{meta_hit.group(0)}」，应直接陈述视频内容而非描述视频/画面怎么说"})
    # 制作者动作：博主是视频的制作者，不是场面的当事人或见证人。V 集人工
    # rejected 2 条 + 复测再 2 条同理由（含「博主自评」），同类证据 4 条；
    # 唯一 approved 反例「博主：X合集」是归属前缀，不在词形内。恢复硬拦截。
    creator_hit = _CREATOR_ACTION_RE.search(head_body)
    if creator_hit:
        errors.append({"code": "G4_CREATOR_PERSPECTIVE", "repairable": True, "reason": f"标题把博主的制作者身份/剪辑/自评动作写进正文「{creator_hit.group(0)}」；博主是视频制作者不是场面见证人，应直接陈述内容（如「博主：X合集」）"})
    # 校验结论：标题不能写「与…不符/不一致」这类审核判断
    verify_hit = _VERIFICATION_CONCLUSION_RE.search(head_body)
    if verify_hit:
        errors.append({"code": "G4_VERIFICATION_CONCLUSION", "repairable": True, "reason": f"标题含校验结论「{verify_hit.group(0)}」，标题陈述内容，不写「与事实不符/不一致」"})
    # 业务规则 P9（2026-11-02）：标题不得直接出现人身攻击用语，转述网友评价也不行。
    # 这条不靠模型判：模型可能认为「转述网友」不是自己在下判断就放行，而读者看到的
    # 就是那个词。词表只收无正常义的词，误伤由重写轮兜底。
    #
    # 但它不属于 G4（展示/格式）那一维度，而是业务安全底线：这里只产出「程序侧命中的
    # 业务政策规则」证据，不塞 G4 errors。维度归属、是否处置、处置动作全部交给 G2 与
    # business_policy 的 action 表决定——与 P1–P8 同一条路，不额外开一档。
    policy_hits: list[dict[str, Any]] = []
    attacks = find_personal_attacks(rendered)
    if attacks:
        policy_hits.append({"rule_id": "P9", "code": "G2_POLICY_VIOLATION", "terms": attacks,
                            "repairable": True, "reason": attack_rewrite_hint(attacks)})
    # 数字与引用证据的语义对齐在独立信号校验中执行；原题不是数值白名单。
    # 归属写法有多种：「据X报道，…」「X称，…」「X通报，…」都成立，不能只认一种。
    # 逗号/冒号停顿的「据通报：」「据现代快报，」人工判过可用（gold.csv 2026-10-31）；
    # 「据X +空格」是人评明确判过「语法结构不完整」的格式（gold.csv：
    # 据咪咕体育 费兰补时破门→勉强通过、据咪咕体育 费兰托雷斯补时进球瞬间→不可用），
    # 空格版必须报错。「据爆料红米…」这种无停顿连写机械判不了，交给评审 Q1_UNCLEAR。
    dangling = re.match(r"^据\s*(?:[，,。；;]|$)", head_body)
    if dangling:
        errors.append({"code": "G4_INCOMPLETE_ATTRIBUTION", "repairable": True, "reason": "「据」后没有信源，需补全可识别的发布方或信源名"})
    spaced = re.match(r"^据\s*[\u4e00-\u9fffA-Za-z0-9]{2,8}\s", head_body)
    if spaced:
        errors.append({"code": "G4_INCOMPLETE_ATTRIBUTION", "repairable": True, "reason": "「据X」后接空格不完整，应写「X：」「据X报道，」或「据X，」"})
    # 词法命中不能证明限定与标题语义一致；保留不确定项供裁决路由使用。
    limit_pending = []
    for d in claim_decisions or []:
        if str(getattr(d, 'claim_id', '')) not in {str(x) for x in (candidate.get('claim_ids') or [])}:
            continue
        for limit in (getattr(d, 'required_limits', None) or []) if str(getattr(d, 'decision', '')) == 'limited' else []:
            # 引号可能是禁写内容、信源或附加条件；字面命中或缺失都不能证明
            # 限定整体满足（同义改写也可能违反禁止项），统一留待人工核实。
            # 但「字面已照做」的保留/归属类不构成待核，否则每条保留口径都变成必查项。
            if _limit_satisfied_by_literal(limit, head_body):
                continue
            # 数值类禁令（「不得写死用量/时长」）靠标题不出现具体量值来满足：
            # 标题里一个数字都没有，就不可能违反。人工判可用的案例里 23 条
            # G1_MISSING_LIMIT 有相当一部分是这类被误挂成待核。
            if _numeric_prohibition_absent(limit, head_body):
                continue
            if not _conditional_duration_absent(limit, head_body):
                limit_pending.append({'claim_id': str(getattr(d, 'claim_id', '')), 'limit': str(limit),
                                      'reason': _limit_pending_reason(limit, head_body)})
        # 条件性约束只在条件可能成立、且无法字面证明已照做时才待核。
        # 过去一律待核，等于把「标题根本没写这件事」也变成必查项：二仙桥、财经、访谈三条任务
        # 全部候选因此零推荐，而人工对这些标题的判定是可用。
        for limit in getattr(d, 'conditional_constraints', None) or []:
            date_only = bool(re.fullmatch(r'\s*\d{1,2}月\d{1,2}日为报道日期[，,]非事件发生日期[。.]?\s*', str(limit)))
            has_date = bool(re.search(r'\d|[〇零一二三四五六七八九十]{1,4}[年月日号]|今年|去年|前年|明年|今天|昨日|昨天|前天|明天|后天|当日|当天|近日|上周|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天]|上个?月|本月|这个月|下个?月', head_body))
            if date_only and not has_date:
                continue
            if _conditional_trigger_absent(limit, head_body) or _conditional_satisfied(limit, head_body):
                continue
            if not _conditional_duration_absent(limit, head_body):
                limit_pending.append({'claim_id': str(getattr(d, 'claim_id', '')), 'limit': str(limit),
                                      'reason': '条件性约束需核实是否适用及是否写错'})
    # 限定校验：引用了「limited 且带 required_limits」的陈述时，标题必须表达限定，
    # 否则会把仅见于标题/声称的信息写成确定事实（等评审抓太晚，这里直接拦在生成后）。
    limit_requirements: list[dict[str, Any]] = []
    if claim_decisions:
        cited = {str(x) for x in (candidate.get("claim_ids") or [])}
        limited_cited = [
            d for d in claim_decisions
            if str(getattr(d, "decision", "")) == "limited"
            and getattr(d, "required_limits", None)
            and str(getattr(d, "claim_id", "")) in cited
            # 载体披露（【剧情演绎】已写明）豁免保留；子事实豁免（scope_absent）删除——
            # 它靠字面匹配判断「标题没提病因」，而「因酒驾昏迷」正是病因断言，测不出。
            # 测不出就不该静默豁免，该交给评审判断适用性。
            and any(not _limit_covered_by_narration(x, prefix_labels)
                    for x in (getattr(d, "required_limits", None) or []))
        ]
        if limited_cited:
            # 归属类限定走和 G1_SOURCE_ATTRIBUTION_MISSING 同一套判据：真实故障（2026-11-02）
            # 里「黄多多自述：…」「黄多多受访：…」既被判已归属、又被判缺限定——两个检查
            # 各用各的词表，结果一条视频的六个候选全被判否，零交付。
            # 反过来也不能让归属语顶掉范围/条件类限定：那是两件事。
            # 只收「确实需要正文表达」的限定：过去把该 claim 的全部 limits 重新摊进来，
            # 被上面豁免掉的禁令/保留类限定又回到 needs_other，而禁令永远写不进标题 ⇒ 必死。
            limits_flat = [
                str(x) for d in limited_cited for x in (getattr(d, "required_limits", None) or [])
                if _limit_needs_body(x, prefix_labels, head_body) and not _limit_attribution_scope_absent(x, head_body)
            ]
            needs_attribution = [x for x in limits_flat if _limit_is_attribution(x)]
            needs_other = [x for x in limits_flat if not _limit_is_attribution(x)]
            satisfied = ((not needs_attribution or _has_explicit_source_relation(head_body))
                         and (not needs_other or all(_limit_is_expressed(x, head_body) for x in needs_other)))
            # 「限定有没有被遵守」是语义判断：词法命中不能证明一致，词法没命中也不能证明违规
            # （V 集实测 24 条被覆盖的候选里 18 条模型明确判 pass、标题也确实归给了对的人）。
            # 所以这里不再否决，只把「必须逐条回答的限定」交给评审：
            #   - limit_requirements 进评审输入，R01 必须在 limit_checks 里逐条给出判定；
            #   - 没答全 → 单独重问这一条候选；
            #   - 裁决跟着模型的判定走（violated→revise，unclear→needs_review，satisfied→不改）。
            # 禁令类例外：被禁词用引号括出且字面出现在标题里，这是词法真正「测出了违反」的
            # 唯一形态——直接拦；没命中（引号没写、或改写了）仍然交评审，不连坐。
            for d in limited_cited:
                for x in (getattr(d, "required_limits", None) or []):
                    hit = _prohibition_hit(x, head_body)
                    if hit:
                        errors.append({"code": "G1_PROHIBITION_HIT", "repairable": True, "reason": f"标题写出被禁内容「{hit}」（限定原文：{str(x)[:60]}），须改写或删除"})
                    elif not _limit_covered_by_narration(x, prefix_labels):
                        limit_requirements.append({"claim_id": str(getattr(d, "claim_id", "")), "limit": str(x)})
            if limit_requirements:
                codes = "、".join(sorted({x["claim_id"] for x in limit_requirements}))
                warnings.append({"code": "G1_LIMIT_REVIEW_REQUIRED", "reason": f"标题引用了需限定的陈述（{codes}），须由评审逐条确认", "limits": limit_requirements})
            # 核查争议类限定单独校验：归属语（「某称」）不等于标明争议，
            # 否则「A 称 X」会被判通过，而用户仍把一个有反证的说法当成确定事实。
            disputed = [
                d for d in limited_cited
                if any(DISPUTE_LIMIT_MARK in str(limit) for limit in (getattr(d, "required_limits", None) or []))
            ]
            if disputed and not any(p in head_body for p in DISPUTE_MARK_PHRASES):
                codes = "、".join(str(getattr(d, "claim_id", "")) for d in disputed)
                errors.append({"code": "G1_MISSING_DISPUTE_MARK", "repairable": True, "reason": f"标题引用了存在外部反驳的陈述（{codes}），但未标明争议；须写「存在争议/未经证实/说法不一」等，或改用其他陈述"})
    # 条件约束也是待评义务，不能仅登记在 limit_pending 后被决策层忽略。
    # 是否触发交给语义评审，不能因字面关键词缺失就静默视为不适用。
    for d in claim_decisions or []:
        cid = str(getattr(d, "claim_id", ""))
        if cid not in {str(x) for x in (candidate.get("claim_ids") or [])}:
            continue
        for constraint in getattr(d, "conditional_constraints", None) or []:
            item = {"claim_id": cid, "limit": str(constraint)}
            if item not in limit_requirements:
                limit_requirements.append(item)
    if candidate.get("self_declared_prefixes"):
        warnings.append({"code": "G4_PREFIX_SELF_DECLARED", "reason": f"模型自报非必填前缀：{'、'.join(candidate['self_declared_prefixes'])}（已保留，仅观测）"})
    if head_body.startswith(("【", "[")):
        warnings.append({"code": "G4_INLINE_PREFIX_IN_BODY", "reason": "正文仍以方括号开头，可能残留未剥离的标注"})
    strategy = str(candidate.get("expression_strategy") or "")
    allowed_strategies = set(rubric().get("title_policy", {}).get("strategy_enum", []))
    if strategy and allowed_strategies and strategy not in allowed_strategies:
        warnings.append({"code": "Q2_STRATEGY_MISMATCH", "reason": f"策略 {strategy} 不在固定枚举内"})
    # 归属预判：程序先给字面结论，评审不许再对同一事实报缺（见 _ATTRIBUTION_IDENTITY_RE 注释）。
    attribution_check: dict[str, Any] | None = None
    if claim_decisions:
        _cited = {str(x) for x in (candidate.get("claim_ids") or [])}
        attr_claims = [d for d in claim_decisions
                       if str(getattr(d, "claim_id", "")) in _cited and getattr(d, "attribution_required", False)]
        if attr_claims:
            phrases = sorted({str(getattr(d, "required_source_phrase") or "") for d in attr_claims
                              if getattr(d, "required_source_phrase", None)})
            identity_present = any(p and (p in head_body or p.lstrip("据") in head_body) for p in phrases) or bool(_ATTRIBUTION_IDENTITY_RE.search(head_body))
            attribution_check = {"required": True, "phrases": phrases, "identity_present": bool(identity_present)}
    return {"status": "fail" if errors else "pass", "errors": errors, "warnings": warnings, "policy_hits": policy_hits, "limit_pending": limit_pending, "limit_requirements": limit_requirements, "blocked_claim_refs": blocked_refs, "attribution_check": attribution_check, "title_length": len(rendered), "body_length": len(head_body), "prefix_labels": prefix_labels, "rendered_title": rendered, "max_chars": max_chars}


def _slot_delivery(candidates: list[dict[str, Any]], decisions: list[dict[str, Any]], thesis_id: Any,
                   prefer_basis: str | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    """每槽取 quality_sum 最高的 ready 候选；缺槽留空，不复制标题也不拿边角凑数。

    thesis_id 可以是单个 id 或 id 集合：两套解读分歧后按复核侧重出的一组，主旨 id 与
    生成侧不同，但两组都服务于同一条视频的交付，不能把其中一组整体过滤掉。
    """
    accepted = {thesis_id} if isinstance(thesis_id, str) else set(thesis_id or ())
    decisions_by_id = {d["candidate_id"]: d for d in decisions}
    ready = [c for c in candidates if decisions_by_id.get(c["candidate_id"], {}).get("status") == "ready_for_human_review"]
    delivery: list[dict[str, Any]] = []
    slot_seen: set[str] = set()
    for slot in SLOTS:
        taken = {c.get("candidate_id") for c in delivery}
        # prefer_basis 让分歧后按复核侧重出的那组在同一槽位上优先，组内仍按质量分排。
        pool = sorted((c for c in ready if c.get("slot_id") == slot and c.get("thesis_id") and c.get("thesis_id") in accepted and c.get("candidate_id") not in taken),
                      key=lambda c: (bool(prefer_basis) and c.get("thesis_basis") == prefer_basis,
                                     decisions_by_id.get(c["candidate_id"], {}).get("quality_sum") or 0), reverse=True)
        if pool:
            delivery.append(pool[0])
            slot_seen.add(slot)
    return delivery, slot_seen


def _delivery_thesis_ids(state: dict[str, Any]) -> set[str]:
    """可参与槽位交付的主旨 id：生成侧基准 + 分歧后按复核侧重出的一组。"""
    accepted = {str((state.get("thesis_contract") or {}).get("thesis_id") or "")}
    regeneration = state.get("thesis_regeneration") or {}
    if regeneration.get("status") == "ok":
        accepted.add(str(regeneration.get("review_thesis_id") or ""))
    accepted.discard("")
    return accepted


def _apply_slot_delivery(route: dict[str, Any], delivery: list[dict[str, Any]]) -> dict[str, Any]:
    """把可用候选收窄到真正能交付的槽位；没有可交付槽位时明确转人工。"""
    if route.get("action") != "handoff":
        return route
    delivery_ids = {c.get("candidate_id") for c in delivery}
    route = {**route, "eligible_candidate_ids": [cid for cid in route.get("eligible_candidate_ids", []) if cid in delivery_ids]}
    if not route["eligible_candidate_ids"]:
        return {**route, "action": "needs_review", "reason": "NO_VALID_DELIVERY_SLOT", "recommended_candidate_id": None}
    if route.get("recommended_candidate_id") not in delivery_ids:
        route["recommended_candidate_id"] = route["eligible_candidate_ids"][0]
    return route


def _slot_status(delivery: list[dict[str, Any]], slot_seen: set[str]) -> dict[str, Any]:
    return {"expected": list(SLOTS), "filled": sorted(slot_seen) or [c.get("slot_id") for c in delivery if c.get("slot_id")],
            "missing": [s for s in SLOTS if s not in slot_seen]}


# 违规处置动作的严格度：reject（不可发）> review（必须人看）> rewrite（可自动改写后重跑）
POLICY_ACTION_STRICTNESS = {"reject": 3, "review": 2, "rewrite": 1}

# 纯机械缺陷可定向修复，但与语义重写共享每条 parent 链的轮次预算。
COMPRESSIBLE_MECHANICAL_CODES = {"G4_LENGTH_EXCEEDED", "G4_ENDING_QUESTION_MARK"}

def _policy_action(rule_ids: list[str], business_policy: dict[str, Any]) -> str | None:
    """按命中规则的 action 取最严处置；规则没写 action 时返回 None（保持旧行为）。"""
    rules = {str(r.get("rule_id")): r for r in business_policy.get("rules", []) if isinstance(r, dict) and r.get("rule_id")}
    actions = [str(rules[rid].get("action") or "") for rid in rule_ids if rid in rules]
    actions = [a for a in actions if a in POLICY_ACTION_STRICTNESS]
    if not actions:
        return None
    return max(actions, key=lambda a: POLICY_ACTION_STRICTNESS[a])


def _compressible_defects(candidate: dict[str, Any], decision: dict[str, Any], review: dict[str, Any]) -> list[dict[str, Any]]:
    """识别可压缩缺陷；仅对超长混合缺限定开放，其余语义缺陷仍不进入。"""
    if (decision or {}).get("status") not in {"rewrite", "needs_review"}:
        return []
    # 命中了业务政策（含 P9 人身攻击词）就不进压缩通道：压缩只砍字，不会把违规词换掉，
    # 必须走语义重写，让模型按 forbid 清单重新表述。
    if (candidate.get("mechanical_checks") or {}).get("policy_hits"):
        return []
    errors = [e for e in ((candidate.get("mechanical_checks") or {}).get("errors") or []) if isinstance(e, dict)]
    if not errors:
        return []
    codes = {str(e.get("code") or "") for e in errors}
    mixed = "G4_LENGTH_EXCEEDED" in codes and "G1_MISSING_LIMIT" in codes
    if not codes <= (COMPRESSIBLE_MECHANICAL_CODES | ({"G1_MISSING_LIMIT"} if mixed else set())):
        return []
    if decision.get("policy_action") == "reject":
        return []
    if mixed and any(
        str(issue.get("issue_code") or "").startswith("G1_") and issue.get("issue_code") != "G1_MISSING_LIMIT"
        for issue in (decision.get("issues") or []) if isinstance(issue, dict)
    ):
        return []
    gates = (decision or {}).get("gates") or (review or {}).get("gates") or {}
    allowed = {"pass", "needs_review"} if decision.get("status") == "needs_review" else {"pass"}
    for gate in ("G1", "G2", "G3"):
        gate_allowed = allowed | ({"revise"} if mixed and gate == "G1" else set())
        if str((gates.get(gate) or {}).get("status") or "needs_review") not in gate_allowed:
            return []
    return errors


def _pending_length_semantic_defects(candidate: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    """超长且主旨需修正时，仅生成继承待核状态的隔离草案。"""
    checks = candidate.get("mechanical_checks") or {}
    errors = [e for e in checks.get("errors", []) if isinstance(e, dict)]
    codes = {e.get("code") for e in errors}
    gates = decision.get("gates") or {}
    if (decision.get("status") != "needs_review" or decision.get("policy_action")
            or checks.get("policy_hits") or "G4_LENGTH_EXCEEDED" not in codes
            or not codes <= {"G4_LENGTH_EXCEEDED", "G4_ENDING_QUESTION_MARK", "G1_MISSING_LIMIT"}
            or (gates.get("G2") or {}).get("status") != "pass"
            or (gates.get("G3") or {}).get("status") != "revise"
            or (gates.get("G1") or {}).get("status") not in {"pass", "needs_review", "revise"}):
        return []
    return errors


def _rewrite_depth(candidate: dict[str, Any], candidates: list[dict[str, Any]]) -> int:
    """沿 parent 链计算已用轮数；缺失或循环的链一律视作预算耗尽。"""
    by_id = {str(c.get("candidate_id")): c for c in candidates}
    seen: set[str] = set()
    depth = 0
    used = 0
    while True:
        marker = candidate.get("rewrite_budget_depth", 0)
        if not isinstance(marker, int) or marker < 0:
            return 2
        used = max(used, depth + marker)
        if not candidate.get("parent_candidate_id"):
            return max(depth, used)
        parent = str(candidate["parent_candidate_id"])
        if parent in seen or parent not in by_id:
            return 2
        seen.add(parent)
        depth += 1
        candidate = by_id[parent]


def _merge_program_policy_hits(gates: dict[str, Any], mechanical: dict[str, Any]) -> None:
    """把程序侧确认的业务政策命中并进 G2，而不是另开一个维度。

    程序只能给出「标题里确实出现了 P9 禁用词」这一事实证据；它要不要拦、拦到什么程度，
    由 business_policy 里该规则的 action 决定（与 P1–P8 同一张表）。所以这里只做三件事：
    把命中的 rule_id 记进 G2 的 rule_ids、单列 `violated_rule_ids`、把 G2 从 pass 拉到
    revise（不覆盖更严的状态）。模型可能因为「只是转述网友」而放行 G2——程序这一路就是
    补上那个漏洞。

    `violated_rule_ids` 必须与 rule_ids 分开：rule_ids 里既有「违反了哪条」，也有模型顺带
    列出的「检查过哪条」。拿整份清单去取最严动作，会让一条 P9（rewrite）被清单里的
    P1（reject）顶成判毙。
    """
    hits = [h for h in (mechanical.get("policy_hits") or []) if isinstance(h, dict) and h.get("rule_id")]
    if not hits:
        return
    gate = dict(gates.get("G2") or {})
    # 第一次合并时记下「模型自己怎么判的」：合并会把 pass 改成 revise，
    # 之后再从状态反推就分不清是模型判出的还是程序拉下来的。
    gate.setdefault("model_flagged", str(gate.get("status") or "needs_review") in {"revise", "needs_review", "reject"})
    rule_ids = [str(x) for x in (gate.get("rule_ids") or [])]
    violated = [str(x) for x in (gate.get("violated_rule_ids") or [])]
    for hit in hits:
        rid = str(hit["rule_id"])
        if rid not in rule_ids:
            rule_ids.append(rid)
        if rid not in violated:
            violated.append(rid)
    reasons = [str(h.get("reason") or "") for h in hits if h.get("reason")]
    gate["rule_ids"] = rule_ids
    gate["violated_rule_ids"] = violated
    gate["program_hits"] = hits
    if str(gate.get("status") or "needs_review") == "pass":
        gate["status"] = "revise"
        gate["reason"] = "；".join(reasons) or "程序侧发现业务政策禁用内容"
    else:
        # 已经是 revise/needs_review/reject：保留更严的那个，只补上程序侧发现的理由。
        gate["reason"] = "；".join([str(gate.get("reason") or "")] + reasons).strip("；") or gate.get("reason")
    gates["G2"] = gate


def _g3_hard_failure(g3: Any) -> bool:
    """G3 的硬伤形态：标题承诺视频没兑现 / 承诺错配。

    判据是「兑现性」而不是「覆盖完整性」：V 集人工评审 3/3 判 approved 的候选
    恰恰都是 core_covered=False + delivery_supported=True + matched——覆盖不全
    （列了菜名没写「可爱日常」、写了主要步骤没写全部步骤）但承诺兑现，人工认为
    可用。而 delivery_supported=False（视频里没有标题承诺的东西）与 mismatched
    没有任何人工反例，是必须重写的真错位。
    """
    if not isinstance(g3, dict):
        return False
    if g3.get("delivery_supported") is False:
        return True
    comparison = g3.get("promise_comparison")
    return isinstance(comparison, dict) and comparison.get("alignment") == "mismatched"


def _fresh_g3_comparison_errors(review: dict[str, Any], candidate: dict[str, Any], evidence: list[Any]) -> list[str]:
    """只核对形式与摘录来源，不以文本匹配替代语义判断。"""
    gates = review.get("gates")
    g3 = gates.get("G3") if isinstance(gates, dict) else None
    comparison = g3.get("promise_comparison") if isinstance(g3, dict) else None
    if not isinstance(comparison, dict):
        return ["G3 promise_comparison missing or invalid"]
    errors = []
    for field in ("delivery_supported", "core_covered"):
        if not isinstance(g3.get(field), bool):
            errors.append(f"G3 {field} missing or invalid")
    for field in ("title_promise", "trigger_span", "video_delivery", "evidence_excerpt", "evidence_answered_question", "explanation"):
        if not isinstance(comparison.get(field), str) or not comparison[field].strip():
            errors.append(f"G3 promise_comparison.{field} missing or empty")
    if comparison.get("alignment") not in {"matched", "mismatched", "uncertain"}:
        errors.append("G3 promise_comparison.alignment missing or invalid")
    span = comparison.get("trigger_span")
    if isinstance(span, str) and span.strip() and span.strip() not in str(candidate.get("rendered_title") or candidate.get("headline") or ""):
        errors.append("G3 trigger_span is not present in candidate headline")
    excerpt = comparison.get("evidence_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        refs = g3.get("evidence_ids") if isinstance(g3.get("evidence_ids"), list) else []
        texts = [getattr(e, "text", "") for e in evidence if getattr(e, "evidence_id", None) in refs]
        if not any(isinstance(text, str) and excerpt.strip() in text for text in texts):
            errors.append("G3 evidence_excerpt not found in cited evidence text")
    return errors


def _explicit_policy_rejection(review: dict[str, Any], policy: dict[str, Any]) -> bool:
    if policy.get("status") != "approved":
        return False
    gates = review.get("gates")
    g2 = gates.get("G2") if isinstance(gates, dict) else None
    if not isinstance(g2, dict) or g2.get("status") not in {"reject", "revise", "needs_review"}:
        return False
    ids = [str(x) for x in (g2.get("rule_ids") or [])]
    known = {str(r.get("rule_id")) for r in policy.get("rules", []) if isinstance(r, dict)}
    return _policy_action(ids, policy) == "reject" or (g2.get("status") == "reject" and bool(known.intersection(ids)))


def _decide_candidate(review: dict[str, Any], mechanical: dict[str, Any], *, schema_errors: list[str], business_policy: dict[str, Any]) -> dict[str, Any]:
    gates = dict(review.get("gates")) if isinstance(review.get("gates"), dict) else {}
    issues = list(review.get("issues")) if isinstance(review.get("issues"), list) else []
    if schema_errors:
        # 评审模型没按 schema 返回，是「评审侧不可用」，不是标题的机械缺陷。
        # 旧实现把它写成 gates["G4"]=fail，于是「模型漏写一个字段」被判成「标题不合规」：
        # 已确认人工判可用的 126 条里，19 条（G4_REPAIRABLE_WITH_INVALID_REVIEW 10、
        # REVIEW_SCHEMA_INVALID 9）就是这么被拦的，标题本身没有任何问题。
        cid = str(review.get("candidate_id") or "")
        unavailable = {"reason": "评审输出结构校验失败", "errors": schema_errors}
        # 评审输出坏了，但机械校验已经给出了确定结论：可修的错误先让它进重写，
        # 否则每条"模型输出超长"都会绕过重写直接塞进人审队列（人审是最贵的一环）。
        if business_policy.get('status') == 'approved' and business_policy.get('rules'):
            hit_ids = [str(h.get('rule_id')) for h in mechanical.get('policy_hits', []) if isinstance(h, dict) and h.get('rule_id')]
            if hit_ids and _policy_action(hit_ids, business_policy) == 'reject':
                _merge_program_policy_hits(gates, mechanical)
                return {'candidate_id': review.get('candidate_id'), 'status': 'reject', 'reason': 'POLICY_ACTION_REJECT', 'gates': gates, 'quality_sum': None, 'issues': issues, 'repairable': False, 'review_unavailable': unavailable}
        repairable_mechanical = [e for e in mechanical.get("errors", []) if e.get("repairable")]
        if mechanical.get("status") == "fail" and repairable_mechanical:
            # 重写产物再次评审崩，不再叠加轮次：在坏评审上继续重写只会把
            # 「一条候选评审失败」放大成整条视频反复调用模型（实测单视频 16 次
            # model_error、7 次重写循环、30 分钟不收敛）。这一类直接待核。
            if "-r" in cid:
                return {"candidate_id": cid, "status": "needs_review", "reason": "REVIEW_SCHEMA_INVALID_ON_REWRITE", "gates": gates, "quality_sum": None, "issues": issues, "repairable": False, "review_unavailable": unavailable}
            return {"candidate_id": cid, "status": "rewrite", "reason": "G4_REPAIRABLE_WITH_INVALID_REVIEW", "gates": gates, "quality_sum": None, "issues": issues, "repairable": True, "review_unavailable": unavailable}
        # 评审不可用时，挂着「必须逐条回答的限定」的候选不能被机械 pass 放行：
        # 没人判过这些限定，等于把可能未核实的说法直接送进交付（旧契约由词法
        # limit_pending 拦，新契约改看 limit_requirements——判定权给评审，评审
        # 不在的时候就得留给人）。
        if [r for r in (mechanical.get("limit_requirements") or []) if isinstance(r, dict)]:
            return {"candidate_id": cid, "status": "needs_review", "reason": "LIMIT_CHECK_UNANSWERED", "gates": gates, "quality_sum": None, "issues": issues, "repairable": False, "review_unavailable": unavailable}
        # 机械没有确定缺陷：只报「评审不可用」，不把评审故障算成标题的问题。
        return {"candidate_id": cid, "status": "needs_review", "reason": "REVIEW_UNAVAILABLE", "gates": gates, "quality_sum": None, "issues": issues, "repairable": False, "review_unavailable": unavailable}
    gates["G4"] = {"status": mechanical["status"], "reason": "程序机械校验", "errors": mechanical["errors"]}
    # 限定不再由词法否决，改由评审逐条判定：violated→revise，unclear/没答→needs_review。
    # 词法层只负责「把要回答的限定列出来」，判定权和判定能力对齐。
    requirements = [r for r in (mechanical.get("limit_requirements") or []) if isinstance(r, dict)]
    if requirements:
        checks = _limit_check_verdicts(review)
        violated = [r for r in requirements if checks.get(_limit_key(r)) == "violated"]
        unclear = [r for r in requirements if checks.get(_limit_key(r)) not in {"satisfied", "violated"}]
        g1 = dict(gates.get('G1') or {})
        if violated or unclear:
            # 未决限定优先于可重写的违反；原有拒绝或待核结论及理由不能被降级。
            if g1.get('status') not in {'reject', 'needs_review'}:
                if unclear:
                    g1['status'] = 'needs_review'
                    g1['reason'] = "限定判定不明确或缺失，需人工核实"
                else:
                    g1['status'] = 'revise'
                    g1['reason'] = "标题未满足限定：" + "；".join(str(r.get("limit"))[:60] for r in violated)
            g1['limit_checks'] = ([dict(r, verdict="violated") for r in violated]
                                  + [dict(r, verdict="unclear") for r in unclear])
            gates['G1'] = g1
    # 暂缓核验引用不再由程序一票否决：评审已看到 blocked_claims 清单，
    # G1 pass 表示它确认标题没有把未核实主张写成断言；G1 unclear/revise/reject
    # 仍按 gate 走。blocked_claim_refs 保留在 gates 里供人工追溯。
    blocked_refs = [x for x in (mechanical.get("blocked_claim_refs") or []) if isinstance(x, str)]
    if blocked_refs:
        g1 = dict(gates.get('G1') or {})
        g1['blocked_claim_refs'] = blocked_refs
        gates['G1'] = g1
    if business_policy.get("status") != "approved" or not business_policy.get("rules"):
        gates["G2"] = {"status": "needs_review", "reason": "缺少已批准的业务安全规则", "rule_ids": []}
    _merge_program_policy_hits(gates, mechanical)
    scores = [review.get("quality", {}).get(q, {}).get("score") for q in QUALITY_IDS]
    quality_sum = sum(x for x in scores if isinstance(x, int)) if all(isinstance(x, int) for x in scores) else None
    quality_min = int(rubric().get("thresholds", {}).get("quality_min_each", 2))
    statuses = {key: str((gates.get(key) or {}).get("status", "needs_review")) for key in (*GATE_IDS, "G4")}
    all_pass = all(statuses[k] == "pass" for k in statuses)
    g2_status = statuses.get("G2")
    g2_gate = gates.get("G2") or {}
    g2_rule_refs = [str(x) for x in (g2_gate.get("rule_ids") or [])]
    policy_rule_ids = {str(x.get("rule_id")) for x in business_policy.get("rules", []) if isinstance(x, dict) and x.get("rule_id")}
    # 处置动作只看「违反了哪条」，不看「检查过哪条」：G2=pass 时列出的规则是检查清单，
    # 不能拿它算最严动作；程序侧命中的规则单列在 violated_rule_ids 里。
    # 只有模型自己判出 G2 问题时，它列出的业务规则才算「违反」——否则「模型放行、程序命中」
    # 会把模型整份检查清单（含 P1 reject）算进去，一条 P9 就变成判毙。
    violated = [str(x) for x in (g2_gate.get("violated_rule_ids") or [])]
    model_flagged = g2_gate.get("model_flagged")
    if model_flagged is None:
        model_flagged = g2_status in {"revise", "needs_review", "reject"}
    if model_flagged:
        violated += [rid for rid in g2_rule_refs if rid in policy_rule_ids]
    violated = list(dict.fromkeys(violated))
    policy_action = _policy_action(violated, business_policy) if violated else None
    if policy_action == "reject":
        status, reason = "reject", "POLICY_ACTION_REJECT"
    elif policy_action == "review":
        status, reason = "needs_review", "POLICY_ACTION_REVIEW"
    elif g2_status == "reject":
        status, reason = ("reject", "EXPLICIT_BUSINESS_POLICY_REJECTION") if policy_rule_ids.intersection(g2_rule_refs) else ("needs_review", "REJECT_WITHOUT_POLICY_RULE")
    elif any(status == "needs_review" for status in statuses.values()):
        # 事实/政策未决必须人看，压过任何自动改写（含 G2 命中 rewrite 处置动作）。
        status, reason = "needs_review", "CORE_FACT_OR_POLICY_UNRESOLVED"
    elif policy_action == "rewrite":
        status, reason = "rewrite", "POLICY_ACTION_REWRITE"
    elif (statuses["G1"] == "pass" and statuses["G2"] == "pass" and statuses["G3"] == "revise"
          and statuses["G4"] == "pass" and all(isinstance(x, int) and x >= quality_min for x in scores)
          and not _g3_hard_failure(gates.get("G3") or {})):
        # 只剩 G3「核心覆盖」一条**软** revise 时不再自动重写：V 集人工评审这种组合
        # 4/4 判 approved（「列菜名」人工够用，评审要求覆盖「可爱日常核心」——评审的
        # 核心覆盖标准比人严）。自动重写每条烧 2 次调用还未必更好，降为 needs_review
        # 交人工。G3 硬伤（core_covered=False / delivery_supported=False / 承诺错配）
        # 与 G1/G4 的 revise 仍走重写——那是可定向修复的真问题。
        status, reason = "needs_review", "G3_REVISION_REQUIRES_HUMAN_CALL"
    elif statuses["G4"] == "fail" or any(status == "revise" for status in statuses.values()) or any(isinstance(x, int) and x < quality_min for x in scores):
        status, reason = "rewrite", "REPAIRABLE_GATE_OR_QUALITY_ISSUE"
    elif all_pass and all(isinstance(x, int) and x >= quality_min for x in scores):
        status, reason = "ready_for_human_review", "ALL_GATES_PASS_AND_QUALITY_MEETS_MINIMUM"
    else:
        status, reason = "needs_review", "REVIEW_RESULT_INCOMPLETE"
    return {"candidate_id": review.get("candidate_id"), "status": status, "reason": reason, "gates": gates, "quality_sum": quality_sum, "issues": issues, "policy_action": policy_action, "repairable": status == "rewrite"}


def _verification_hold(risk: dict[str, Any] | None) -> dict[str, Any] | None:
    """把核查结论翻译成「转人工」的两种理由，其余情况返回 None。

    两种都要停：外部事实被证伪（HIGH_EXTERNAL_FACT_RISK），以及已判定必须核查、
    却没有权威来源可查（NO_WHITELIST_FOR_REQUIRED_CHECK）——后者若静默继续，
    强制核查清单在这类品类上等于一句空话。
    """
    if not risk:
        return None
    if risk.get("level") == "high":
        return {"reason": "HIGH_EXTERNAL_FACT_RISK", "claim_ids": risk.get("refuted_claim_ids", [])}
    if risk.get("pending_human"):
        return {"reason": "NO_WHITELIST_FOR_REQUIRED_CHECK", "claim_ids": risk.get("unverified_claim_ids", [])}
    return None


class Pipeline:
    def __init__(self, dataset: Dataset, storage: Storage, adapter: ModelAdapter, config: RunConfig, review_adapter: ModelAdapter | None = None, search_provider=None) -> None:
        self.dataset, self.storage, self.adapter, self.review_adapter, self.config = dataset, storage, adapter, review_adapter or adapter, config
        self.memory = MemoryStore(storage.case_conn)
        self.search_provider = search_provider

    def _verify_understanding(self, task_id: str, video: Any, understanding: Understanding, state: dict[str, Any]) -> dict[str, Any] | None:
        # 查不查这件事统一由 resolve_verification 决定，并把「谁做的决定」写进事件：
        # 回退到静态路由的比例、模型说不查的那些视频，都要能事后审计。
        decision = resolve_verification(video, understanding.claims, state.get("verification_suggestion"), self.config)
        state["verification_decision"] = decision
        self.storage.event(task_id, "N03V", "verification_decision", decision)
        if not decision["needed"]:
            return None
        if self.search_provider is None and self.config.verification_provider.strip().lower() in {"", "none", "unconfigured"}:
            raise RuntimeError("verification is enabled but verification_provider is not configured")
        provider = self.search_provider or create_search_provider(
            self.config.verification_provider,
            secret_id_env=self.config.verification_secret_id_env,
            secret_key_env=self.config.verification_secret_key_env,
        )
        suggestion = state.get("verification_suggestion") or {}
        claims = pick_core_claims(understanding.claims, self.config, suggestion.get("claim_ids"))
        if not claims:
            summary = {"level": "low", "refuted_claim_ids": [], "reason": "no_verifiable_claims"}
            state["verification_risk"] = summary
            return summary
        sites = whitelist_for(video.domain)
        # dynamic 下模型点名要查、但该品类没有权威白名单时，不能退回「搜全网」：
        # 搜来的来源不可控，等于用不确定结果去裁决事实。改为留痕并按未核查处理。
        if not sites and normalize_verification_mode(self.config) == "dynamic" and getattr(self.config, "verification_require_whitelist", True):
            summary = {
                "level": "low",
                "refuted_claim_ids": [],
                "reason": "no_whitelist_for_domain",
                "unverified_claim_ids": [c.claim_id for c in claims],
                # 已判定「必须核查」却没有权威来源可查时不能静默继续：那等于「必须核查」
                # 在这类品类上是一句空话。转人看一眼，由人决定是补来源还是降级表达。
                "pending_human": True,
            }
            state["verification_risk"] = summary
            self.storage.event(task_id, "N03V", "verification_skipped_no_whitelist", {
                "domain": getattr(video, "domain", None) or {},
                "claim_ids": [c.claim_id for c in claims],
            })
            return summary
        audit_rows = []
        for claim in claims:
            query = build_verification_query(claim)
            if not query:
                continue
            results = provider.search(query, sites, 10)
            # Search is retrieval, not the judge. Empty/unusable result sets remain unknown.
            if not results:
                claim.verification = parse_verification({"verdict": "unverifiable", "sources": [], "confidence": 0, "query": query}, claim.claim_id)
                audit_rows.append({"claim_id": claim.claim_id, "query": query, "results": [], "verdict": "unverifiable"})
                continue
            judged = self._call(task_id, "N03V", "verify", {
                "claim": {"claim_id": claim.claim_id, "text": claim.text, "claim_type": claim.claim_type},
                "search_results": results,
            }, "VerificationResult", adapter=self.review_adapter)
            result = parse_verification(judged, claim.claim_id)
            if judged.get("claim_id") is not None and str(judged["claim_id"]) != claim.claim_id:
                result.verdict = "unverifiable"
                result.confidence = 0.0
            available_urls = {str(item.get("url", "")) for item in results}
            result.sources = [url for url in result.sources if url in available_urls]
            # URL count alone is not source independence: syndicated pages can differ by path.
            independent_hosts = {
                (parsed.hostname or "").lower().removeprefix("www.")
                for url in result.sources
                if (parsed := urlsplit(url)).scheme in {"http", "https"} and parsed.hostname
            }
            if result.verdict == "refuted" and (
                len(independent_hosts) < self.config.refute_min_sources
                or result.confidence < self.config.refute_confidence_threshold
            ):
                # 不把判定抹成 unverifiable：有来源认为这是错的，即使证据不足也要留痕，
                # 交给下游降级为限定表达，而不是当作「没核查过」。
                result.downgraded = True
            claim.verification = result
            audit_rows.append({"claim_id": claim.claim_id, "query": query, "results": results, "verdict": result.verdict, "confidence": result.confidence, "sources": result.sources, "independent_source_count": len(independent_hosts), "downgraded": result.downgraded})
        summary = compute_risk(claims, self.config.refute_min_sources, self.config.refute_confidence_threshold)
        state["verification"] = audit_rows
        state["verification_risk"] = summary
        state["understanding"] = to_dict(understanding)
        self.storage.event(task_id, "N03V", "verification_complete", {"risk": summary, "claim_count": len(audit_rows)})
        self.storage.checkpoint(task_id, "generating", state, "N03V", "verification", {"risk_level": summary["level"], "claim_count": len(audit_rows)})
        return summary

    def _call(self, task_id: str, node: str, name: str, user: dict[str, Any], schema: str, adapter: ModelAdapter | None = None, rules: dict[str, Any] | None = None) -> dict[str, Any]:
        # 规则全文进 system 的公共前缀段，不进 user：同一个任务的 5 个节点共用一份
        # 规则，只有「从第一个 token 起完全一致」才吃得到前缀缓存，而 user 里每个
        # 节点拼的载荷各不相同，放那里等于逐次重付 8.3K 字符（实测 review 一份
        # 载荷里 27.6% 是规则全文）。
        system_base = prompt_for(name, schema)
        rules_block = prompt_text_from_context(rules) if rules else ""
        system = prompt_for(name, schema, rules_block)
        selected_adapter = adapter or self.adapter
        canonical_request = json.dumps({"system": system, "user": user, "schema": schema, "model": str(getattr(selected_adapter, "model", "mock"))}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
        prompt_file = str(Path(__file__).with_name("prompts.py"))
        prompt_constant = {"understand": "UNDERSTAND_PROMPT", "boundary": "BOUNDARY_PROMPT", "highlight": "HIGHLIGHT_PROMPT", "generate": "GENERATION_PROMPT", "rewrite": "REWRITE_PROMPT", "review": "REVIEW_PROMPT", "verify": "VERIFY_PROMPT"}.get(name, "SHARED_RULES")
        selected_adapter = adapter or self.adapter
        model_name = str(getattr(selected_adapter, "model", self.config.review_model if name == "review" else self.config.generation_model))
        started = time.monotonic()
        self.storage.event(task_id, node, "node_started", {"task": name, "schema": schema, "model": model_name, "request_hash": request_hash})
        # 快照存「不含规则块」的 system + 规则块指纹，不存全文：规则 8.3K 字符 × 每任务
        # 8–12 次调用会把事件表撑大一倍，而规则可以按 rule_ids 从 rulepacks/*.md 逐字重建，
        # rule_bundle_hash 已随 N04 落库。prompt_hash/request_hash 仍按完整 system 计算，
        # 与真实发给模型的请求一致。
        self.storage.event(task_id, node, "prompt_snapshot", {
            "task": name, "schema": schema, "model": model_name, "prompt_file": prompt_file,
            "prompt_constant": prompt_constant, "prompt_hash": prompt_hash, "request_hash": request_hash,
            "system_prompt": system_base, "system_chars": len(system),
            "rules_block_chars": len(rules_block),
            "rules_block_hash": hashlib.sha256(rules_block.encode("utf-8")).hexdigest() if rules_block else None,
            "user_payload": user,
        })
        candidate_ids = [str(c.get("candidate_id") or "") for c in user.get("candidate_titles", [])] if name == "review" and schema == "ReviewResultBatch" else None
        cached = self.storage.cached_model_reply(task_id, request_hash)
        if cached is not None and (candidate_ids is not None or name == "rewrite"):
            if not isinstance(cached, dict):
                error = "cached reply must be an object"
            elif candidate_ids is not None:
                error = _review_batch_structure_error(cached, candidate_ids)
            else:
                error = _rewrite_structure_error(cached)
            if error:
                self.storage.event(task_id, node, "model_cache_invalid", {"task": name, "request_hash": request_hash, "error": error})
                cached = None
        if cached is not None:
            self.storage.event(task_id, node, "model_cache_hit", {"task": name, "request_hash": request_hash, "prompt_hash": prompt_hash})
            self.storage.event(task_id, node, "node_completed", {"task": name, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "cached": True})
            return cached
        last: ModelError | None = None
        for attempt in range(self.config.schema_retry_limit + 1):
            call_started = time.monotonic()
            # 重试不能原样再问一遍：把"上次输出不是合法 JSON"告诉模型，才可能真的修好
            attempt_user = user if attempt == 0 else {
                **user,
                "_previous_output_error": {
                    "error": str(last),
                    # 只回错误码，模型会换个位置再坏一次；把断点原文回给它才可能真修好。
                    "previous_output_tail": (getattr(last, "raw", "") or "")[-600:],
                    "instruction": "上一次输出的 JSON 或结构不合要求。请返回严格合法的 JSON 对象，按要求完整覆盖每个候选并保留全部必需字段；不要尾逗号、代码块或解释文字。若内容太长，请压缩说明文字。",
                },
            }
            try:
                reply = selected_adapter.complete(task=name, system=system, user=attempt_user, schema=schema, attempt=attempt)
                if reply.status != "ok" or not isinstance(reply.data, dict): raise ModelError("invalid model response", True)
                structure_error = None
                if candidate_ids is not None:
                    structure_error = _review_batch_structure_error(reply.data, candidate_ids)
                elif name == "rewrite":
                    structure_error = _rewrite_structure_error(reply.data)
                if structure_error:
                    # 无效回复不缓存；原文供下一次结构重试使用。
                    broken = reply.raw_text or json.dumps(reply.data, ensure_ascii=False)
                    self.storage.event(task_id, node, "model_reply_invalid", {"task": name, "attempt": attempt, "request_hash": request_hash, "usage": reply.usage, "latency_ms": reply.latency_ms, "error": structure_error, "raw_output": broken[:6000]})
                    raise ModelError(f"invalid {name} structure: {structure_error}", True, raw=broken)
                self.storage.event(task_id, node, "model_reply", {"task": name, "attempt": attempt, "request_hash": request_hash, "prompt_hash": prompt_hash, "usage": reply.usage, "latency_ms": reply.latency_ms, "data": reply.data})
                self.storage.event(task_id, node, "node_completed", {"task": name, "attempt": attempt, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "model_latency_ms": reply.latency_ms, "cached": False})
                return reply.data
            except ModelError as exc:
                last = exc
                self.storage.event(task_id, node, "model_error", {"task": name, "attempt": attempt, "request_hash": request_hash, "error": str(exc), "retryable": exc.retryable, "raw_output": (getattr(exc, "raw", "") or "")[:6000], "duration_ms": int((time.monotonic() - call_started) * 1000)})
                if not exc.retryable: break
        self.storage.event(task_id, node, "node_failed", {"task": name, "request_hash": request_hash, "duration_ms": int((time.monotonic() - started) * 1000), "error": str(last or "model call failed")})
        raise last or ModelError("model call failed", False)

    def _review_with_fallback(self, task_id: str, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
        """一次评审多个候选；整批解析失败、或显式配置时退化为逐条评审。

        真实故障：三个候选一起评，模型输出的 JSON 在第二个候选处结构坏掉，
        重试三次仍然坏，整条视频被判 technical_failed——三个候选一条都没留下。
        逐条评审让小请求更不容易坏，也让"坏一条"不再等于"全灭"。

        2026-11-04 大批次实测补充：整批请求的失败不是偶发。V 集 20 条累计 50 次
        无效调用，一次评 3 个候选时模型要么只写 1100–2300 tokens 把必填字段漏掉，
        要么失控写到 max_tokens=16384 被砍断（26%）；同一条输入拆成单候选后输出
        降到 900–2400 tokens 且一次通过。所以逐条不只是"兜底"，在长载荷下它才是
        默认该走的路——`review_per_candidate` 打开后不再先赌整批。
        """
        if getattr(self.config, "review_per_candidate", False) and len(candidates) > 1:
            return self._review_per_candidate(task_id, video, state, candidates, checks, reason="configured")
        try:
            return self._call(task_id, "R01", "review", self._review_input(video, state, candidates, checks), "ReviewResultBatch", self.review_adapter, rules=state.get("rules"))
        except ModelError as exc:
            # 付款、鉴权等不可重试错误不能降级成逐候选请求，否则只会重复失败。
            if not exc.retryable:
                raise
            self.storage.event(task_id, "R01", "review_batch_degraded", {"candidates": len(candidates), "error": str(exc)[:300]})
        return self._review_per_candidate(task_id, video, state, candidates, checks, reason="batch_failed")

    def _review_per_candidate(self, task_id: str, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], checks: list[dict[str, Any]], *, reason: str) -> dict[str, Any]:
        """每个候选单独一次请求，坏一条不再连坐其余候选。"""
        merged: dict[str, Any] = {"reviews": []}
        failures: list[str] = []
        for candidate in candidates:
            cid = str(candidate.get("candidate_id") or "")
            single_checks = [c for c in checks if str(c.get("candidate_id") or "") == cid]
            try:
                one = self._call(task_id, "R01", "review", self._review_input(video, state, [candidate], single_checks), "ReviewResultBatch", self.review_adapter, rules=state.get("rules"))
            except ModelError as exc:
                if not exc.retryable:
                    raise
                # 整批先坏、逐条再坏，实测会停在「只差一条」：cand-1 连续两次漏 quality，
                # 整条视频因此零推荐。逐条请求本身很小，再给一次机会仍是有界的。
                self.storage.event(task_id, "R01", "review_single_retry", {"candidate_id": cid, "error": str(exc)[:200]})
                try:
                    one = self._call(task_id, "R01", "review", self._review_input(video, state, [candidate], single_checks), "ReviewResultBatch", self.review_adapter, rules=state.get("rules"))
                except ModelError as retry_exc:
                    if not retry_exc.retryable:
                        raise
                    failures.append(cid)
                    self.storage.event(task_id, "R01", "review_single_failed", {"candidate_id": cid, "error": str(retry_exc)[:200]})
                    continue
            merged["reviews"].extend(_normalize_reviews(one, [cid]))
            # 逐条请求会独立给出任务级判断，全部留存，不能让首条掩盖后续异议。
            conclusion = one.get("thesis_consistency")
            if isinstance(conclusion, dict):
                merged.setdefault("thesis_consistency_per_candidate", []).append(
                    {"candidate_id": cid, "conclusion": conclusion}
                )
                # 保留旧字段供已有消费者读取；裁决历史使用完整列表。
                merged.setdefault("thesis_consistency", conclusion)
            if one.get("rubric_version") and "rubric_version" not in merged:
                merged["rubric_version"] = one["rubric_version"]
        merged["degraded_to_per_candidate"] = True
        merged["per_candidate_reason"] = reason
        merged["degraded_failures"] = failures
        self.storage.event(task_id, "R01", "review_batch_degraded_complete", {"recovered": len(merged["reviews"]), "failed": failures, "reason": reason})
        return merged

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
            Path(__file__).with_name("memory.py"), Path(__file__).with_name("labels.py"), Path(__file__).with_name("signals.py"),
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
        if self.config.target_count != len(SLOTS):
            raise ValueError("三槽生成要求 target_count=3")
        video = self.dataset.get(video_id)
        task_id = self.storage.create_task(video_id, asdict(self.config))
        manifest = self._manifest(task_id)
        manifest["launch_source"] = launch_source
        material = assess_material(video)
        state: dict[str, Any] = {"video": to_dict(video), "rewrite_round": 0, "reviews": [], "candidates": [], "business_policy": self._business_policy(), "run_manifest": manifest, "prompt_hashes": {}, "input_quality": material}
        self.storage.event(task_id, "N00", "run_manifest", state["run_manifest"])
        self.storage.event(task_id, "N00", "input_snapshot", state["video"])
        self.storage.event(task_id, "N01", "input_quality", material)
        self.storage.checkpoint(task_id, "prepared", state, "N00", "input", {"video_id": video_id, "evidence_count": len(video.evidence), "input_quality": material["level"]})
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
        # 三槽是当前唯一受支持的交付契约；配置与之冲突时明确停下并留痕，
        # 不进入生成再产出少槽/多槽的候选（resume 旧配置也走这条）。
        if self.config.target_count != len(SLOTS):
            state["config_conflict"] = {"target_count": self.config.target_count, "supported": len(SLOTS)}
            return self._stop(task_id, state, "TARGET_COUNT_UNSUPPORTED")
        try:
            gate_dir = self.config.extra.get("release_gate_dir")
            if gate_dir:
                from .release_gate import check_active_release
                root = Path(__file__).resolve().parents[1]
                gate = check_active_release(root, gate_dir)
                if gate.get("status") == "active" and gate.get("proposal_ids"):
                    from .repair_release import reviewed_ids
                    try:
                        reviewed_ids(str(self.config.extra.get("repair_ledger") or "runtime/repair/repair.db"), gate["proposal_ids"])
                    except Exception as exc:
                        gate = {**gate, "allowed": False, "reason": f"提案账本不可核验：{exc}"}
                state["release_gate"] = gate
                if not gate["allowed"] or gate["status"] != "active":
                    return self._stop(task_id, state, "RELEASE_GATE_NOT_APPROVED")
            self.storage.set_state(task_id, "generating", state)
            material = state.get("input_quality") or assess_material(video)
            state["input_quality"] = material
            if material.get("level") == "low" and bool(self.config.extra.get("stop_on_low_quality_input")):
                return self._stop(task_id, state, "INSUFFICIENT_MATERIAL")
            state["risk_signals"] = extract_risk_signals(video)
            self.storage.event(task_id, "N02", "risk_signals_extracted", state["risk_signals"])
            source_evidence_ids = sorted(e.evidence_id for e in video.evidence if e.quality == "source")
            understanding_raw = self._call(task_id, "N03", "understand", {
                "video": to_dict(video),
                "source_title": {
                    "text": video.title, "quality": "source",
                    "note": "唯一一手文本。可作改写基线（压缩/规整/去标题党），但不是事实依据；与材料冲突时以材料为准。",
                },
                "input_quality": material,
                "risk_signals": state["risk_signals"],
            }, "Understanding")
            understanding = _normalize_understanding(understanding_raw)
            # 快速失败：语义单元引用了不存在的 claim_id，说明理解输出自相矛盾——
            # 单元声明了 c1..cN，却没有任何 claim 对象定义它们。若放行，边界节点会拿到空
            # claim 集合后自行编造一套 claim_id，引用审计再把它们全部判成悬空引用，整条
            # 视频最后停在「引用不全」这个误导性理由上（真实故障，见 _claims_nested_in_units）。
            # 这类输出可以救：带着明确要求重问一次，而不是让下游替它编造事实。
            if not understanding.claims and understanding.semantic_units:
                unit_refs = _unit_claim_refs(understanding_raw.get("semantic_units")
                                             or (understanding_raw.get("Understanding") or {}).get("semantic_units")
                                             or (understanding_raw.get("understanding") or {}).get("semantic_units")
                                             or [])
                if not unit_refs:
                    unit_refs = {cid for u in understanding.semantic_units for cid in u.claim_ids}
                if unit_refs:
                    self.storage.event(task_id, "N03", "understanding_claims_missing", {
                        "referenced_claim_ids": sorted(unit_refs)[:20],
                        "semantic_units": len(understanding.semantic_units),
                        "reply_keys": sorted(understanding_raw.keys())[:12],
                    })
                    understanding_raw = self._call(task_id, "N03", "understand", {
                        "video": to_dict(video),
                        "source_title": {
                            "text": video.title, "quality": "source",
                            "note": "唯一一手文本。可作改写基线（压缩/规整/去标题党），但不是事实依据；与材料冲突时以材料为准。",
                        },
                        "input_quality": material,
                        "risk_signals": state["risk_signals"],
                        "_previous_output_error": {
                            "error": "上一次输出里 semantic_units 声明了 " + ",".join(sorted(unit_refs)[:8]) + " 等 claim_id，"
                                     "但顶层没有任何 candidate_claims 数组定义它们，claim 集合为空。",
                            "instruction": "必须在顶层（与 semantic_units 同级）输出 candidate_claims 数组，"
                                           "为每条被引用的 claim_id 给出完整的 claim 对象；"
                                           "semantic_units[*].claim_ids 只能引用该数组中已定义的 ID，不得出现未定义的 cN。",
                        },
                    }, "Understanding")
                    understanding = _normalize_understanding(understanding_raw)
            if not understanding.claims:
                # 重问后仍拿不到任何陈述：这是理解节点失败，不是"内容不可写"。
                # 此时必须停在这里，不能把空集合交给边界节点替它编造事实。
                state["understanding"] = to_dict(understanding)
                state["understanding_failure"] = {
                    "reason": "CLAIMS_EMPTY",
                    "semantic_units": len(understanding.semantic_units),
                    "reply_keys": sorted(understanding_raw.keys())[:12],
                }
                self.storage.event(task_id, "N03", "understanding_empty", state["understanding_failure"])
                return self._stop(task_id, state, "UNDERSTANDING_CLAIMS_MISSING")
            semantic_bindings = _bind_semantic_use_to_evidence(understanding, video)
            if semantic_bindings:
                state["semantic_use_bindings"] = semantic_bindings
                self.storage.event(task_id, "N03", "semantic_use_downgraded", {"units": semantic_bindings})
            state["understanding"] = to_dict(understanding)
            # 一手材料使用率：claim 是否引用了 quality=source 的证据（诊断 D1 的可观测化）
            claims_using_source = [c.claim_id for c in understanding.claims if source_evidence_ids and set(c.evidence_refs) & set(source_evidence_ids)]
            state["source_evidence_usage"] = {"source_evidence_ids": source_evidence_ids, "claims_using_source": claims_using_source, "covered": bool(claims_using_source), "claim_total": len(understanding.claims)}
            sanitized = _sanitize_event_actors(understanding.events, video)
            if sanitized:
                self.storage.event(task_id, "N03", "event_actor_neutralized", {"count": sanitized, "reason": "事件缺一手文本证据，主语降级为「一方」"})
            state["reference_audit"] = audit_evidence(understanding, video)
            evidence_types = {e.evidence_id: e.quality for e in video.evidence}
            state["source_only_claim_ids"] = [c.claim_id for c in understanding.claims if c.evidence_refs and all(evidence_types.get(r) == "source" for r in c.evidence_refs)]
            state["evidence_contract_version"] = "evidence-v1"
            primary_audit = _primary_evidence_audit(understanding, video)
            state["primary_evidence_audit"] = primary_audit
            state["narrative_risk"] = _narrative_risk(video, primary_audit)
            if state["narrative_risk"]["level"] != "none":
                self.storage.event(task_id, "N03", "narrative_risk", state["narrative_risk"])
            if primary_audit.get("applicable") and primary_audit.get("flagged"):
                self.storage.event(task_id, "N03", "primary_evidence_missing", primary_audit)
            state["prompt_hashes"]["understand"] = self._last_prompt_hash(task_id, "N03", "understand")
            self.storage.event(task_id, "N03", "source_evidence_usage", state["source_evidence_usage"])
            # 理解节点可选输出核查建议（仅 dynamic 模式使用）。解析失败一律当作「无建议」，
            # 由 resolve_verification 回退静态路由——绝不把解析失败读成「模型说不用查」。
            state["verification_suggestion"] = extract_verification_suggestion(understanding_raw)
            self.storage.checkpoint(task_id, "generating", state, "N03", "understanding", {"claim_count": len(understanding.claims), "source_evidence_covered": bool(claims_using_source)})
            verification_risk = self._verify_understanding(task_id, video, understanding, state)
            hold = _verification_hold(verification_risk)
            if hold:
                state["verification_hold"] = hold
                state["understanding"] = to_dict(understanding)
                self.storage.checkpoint(task_id, "pending_human", state, "N03V", "verification_hold", state["verification_hold"])
                self.storage.event(task_id, "N03V", "verification_human_hold", state["verification_hold"])
                return task_id
            rule_bundle = load_rule_bundle(domain=video.domain, form=video.format, attributes=understanding.attributes, version=self.config.prompt_pack_version)
            state["rules"] = rule_bundle.as_context()
            state["rule_bundle_hash"] = hashlib.sha256(json.dumps(state["rules"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self.storage.event(task_id, "N04", "rule_match", rule_bundle.as_context())
            self.storage.checkpoint(task_id, "generating", state, "N04", "rules", {"rule_ids": state["rules"].get("rule_ids", []), "rule_bundle_hash": state["rule_bundle_hash"]})
            boundary_payload = {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "primary_evidence_audit": primary_audit, "rules": [asdict(x) for x in load_rules()], "matched_rule_ids": rule_bundle.rule_ids}
            boundary_raw = self._call(task_id, "N05", "boundary", boundary_payload, "BoundarySet", rules=rule_bundle.as_context())
            claim_text_map = {c.claim_id: c.text for c in understanding.claims}
            claim_evidence_map = {c.claim_id: list(c.evidence_refs) for c in understanding.claims}
            boundary = _normalize_boundary(boundary_raw, claim_text_map, claim_evidence_map)
            missing_boundary_ids = _missing_boundary_claims(boundary)
            if (not boundary.decisions or missing_boundary_ids) and understanding.claims:
                # 两种情况都是模型把决策写丢了，不是"真的没有可写内容"：
                # 一条决策都没给，或给了部分 claim 却漏掉其余（后者过去被静默兜底成 excluded）。
                # 直接停机等于白烧一次 20K token 的调用，所以带着"必须逐条表态"的要求再问一次。
                self.storage.event(task_id, "N05", "boundary_empty_retry", {
                    "claims": len(understanding.claims),
                    "received": len(boundary.decisions),
                    "missing_claim_ids": missing_boundary_ids[:20],
                    "reply_keys": sorted(boundary_raw.keys())[:12],
                })
                boundary_payload = {**boundary_payload, "_previous_output_error": {
                    "error": f"上一次返回里有 {len(missing_boundary_ids)} 条 claim 没有给出 decision（收到 {len(boundary.decisions)} 条决策）。",
                    "instruction": "必须为输入里的每一条 claim 都给出 decision（usable / limited / excluded / verification_required），放在顶层 decisions 或 claims 数组里；不要只给 claims_summary 这类摘要。",
                }}
                boundary_raw = self._call(task_id, "N05", "boundary", boundary_payload, "BoundarySet", rules=rule_bundle.as_context())
                boundary = _normalize_boundary(boundary_raw, claim_text_map, claim_evidence_map)
                if _missing_boundary_claims(boundary):
                    # 二次仍漏答：不再重试，但把漏答记进 state，事后能区分「模型漏答」与「材料不可用」。
                    state["boundary_missing_decisions"] = _missing_boundary_claims(boundary)
            # 未知 ID 不能按顺序映射为其他陈述，否则会把不同事实的边界串换。
            reference_audit = audit_evidence(understanding, video, boundary)
            state["reference_audit"] = reference_audit
            self.storage.event(task_id, "N05", "reference_audit", reference_audit)
            # 时间码偏差只记录不阻断：原材料允许误差，定位不准不改写它的可信度。
            locator_notes = [i for i in reference_audit.get("issues", []) if str(i.get("code") or "") in LOCATOR_ISSUE_CODES]
            if locator_notes:
                state["reference_locator_notes"] = locator_notes[:20]
                self.storage.event(task_id, "N05", "reference_locator_tolerated", {
                    "count": len(locator_notes),
                    "codes": sorted({str(i.get("code") or "") for i in locator_notes}),
                })
            # 程序兜底：材料里有一手文本、但陈述只引用模型描述/画面路径的，一律降级为不可写。
            # 这一步必须在 allowed 计算之前，否则编造的陈述会直接进入看点与生成。
            boundary, guarded = _apply_primary_evidence_guard(boundary, primary_audit, understanding)
            if guarded:
                self.storage.event(task_id, "N05", "primary_evidence_downgraded", {"downgraded_claims": guarded, "flagged_total": len(primary_audit.get("claims") or {})})
            # 冲突闸门：冲突记录不能只躺在 state 里，必须下沉到 boundary 降级 claim。
            before_conflict = {d.claim_id: d.decision for d in boundary.decisions}
            boundary = _apply_conflict_gate(understanding, boundary)
            boundary = _apply_numeric_conflict_gate(video, understanding, boundary)
            conflict_downgraded = sorted(cid for cid, dec in before_conflict.items() if dec == "usable" and next((d.decision for d in boundary.decisions if d.claim_id == cid), dec) != "usable")
            if conflict_downgraded:
                self.storage.event(task_id, "N05", "conflict_downgraded", {"downgraded_claims": conflict_downgraded})
            claim_ids = {c.claim_id for c in understanding.claims}
            blocking_audit = _blocking_reference_issues(reference_audit["issues"], boundary)
            if blocking_audit:
                # 不能因为某条陈述缺引用就让整条视频零产出：把「可写但引用不全」的陈述降级为
                # excluded（缺证据就不该写），其余陈述继续走生成，交给评审与人工判断。
                demoted = _demote_claims_without_refs(boundary, blocking_audit)
                pruned = _prune_dangling_unit_refs(understanding, boundary, blocking_audit)
                empty_units = _exclude_units_with_empty_refs(understanding, boundary, blocking_audit)
                if empty_units:
                    state["reference_excluded_units"] = empty_units
                    self.storage.event(task_id, "N05", "reference_units_excluded", {"unit_ids": empty_units})
                    pruned = pruned + [{"unit_id": uid, "dropped": [], "excluded": True} for uid in empty_units]
                pruned_turns = _prune_invalid_turns(understanding, blocking_audit)
                state["boundary"] = to_dict(boundary)
                state["reference_demoted_claims"] = demoted
                if pruned:
                    state["understanding"] = to_dict(understanding)
                    state["reference_pruned_units"] = pruned
                    self.storage.event(task_id, "N05", "reference_units_pruned", {"units": pruned})
                if pruned_turns:
                    state["understanding"] = to_dict(understanding)
                    state["reference_pruned_turns"] = pruned_turns
                    self.storage.event(task_id, "N05", "reference_turns_pruned", {"turns": pruned_turns})
                self.storage.event(task_id, "N05", "reference_claims_demoted", {"claim_ids": demoted, "issues": blocking_audit[:10]})
                if demoted or pruned or pruned_turns:
                    # 剪枝/降级后重新审计：残留问题里只有「非单元级」的才继续阻断。
                    # 单元级问题已由剪枝与排除处理，对应单元不会再进入看点集合。
                    reference_audit = audit_evidence(understanding, video, boundary)
                    state["reference_audit"] = reference_audit
                    leftover = [i for i in _blocking_reference_issues(reference_audit["issues"], boundary)
                                if "semantic_units" not in str(i.get("path") or "")
                                and "understanding.turns[" not in str(i.get("path") or "")]
                    if leftover:
                        state["reference_audit_leftover"] = leftover[:10]
                    else:
                        blocking_audit = []
                if blocking_audit:
                    state["route"] = {"action": "needs_review", "reason": "REFERENCE_AUDIT_INVALID", "recommended_candidate_id": None, "eligible_candidate_ids": []}
                    return self._handoff(task_id, state, state["route"])
            constraints = _apply_verification_constraints(
                understanding, boundary,
                self.config.refute_min_sources, self.config.refute_confidence_threshold,
            )
            if constraints["excluded"] or constraints["limited"]:
                state["verification_refuted_claims"] = sorted(constraints["excluded"])
                state["verification_limited_claims"] = sorted(constraints["limited"])
                state["understanding"] = to_dict(understanding)
                self.storage.event(task_id, "N05", "verification_constraint", {
                    "refuted_claim_ids": sorted(constraints["excluded"]),
                    "limited_claim_ids": sorted(constraints["limited"]),
                })
            state["boundary"] = to_dict(boundary)
            state["prompt_hashes"]["boundary"] = self._last_prompt_hash(task_id, "N05", "boundary")
            self.storage.checkpoint(task_id, "generating", state, "N05", "boundary", {"decision_count": len(boundary.decisions), "usable_count": sum(x.decision != "excluded" for x in boundary.decisions)})
            allowed = [x for x in boundary.decisions if x.decision != "excluded" and not x.verification_required]
            if not allowed:
                pending = [x.claim_id for x in boundary.decisions if x.verification_required]
                state["verification_tasks"] = [to_dict(x) for x in boundary.decisions if x.verification_required]
                return self._stop(task_id, state, "PENDING_VERIFICATION" if pending else "NO_USABLE_CLAIM")
            # 结构候选需要独立的关系证据契约；当前没有，因此程序不合成关系单元，
            # 也不把引用重合包装成 limited 单元绕开 C22。原始 events/turns 仍随输入交给独立评审。
            structures = _structural_candidates(understanding, boundary)
            if structures:
                state["structural_candidates"] = structures
                self.storage.event(task_id, "N05", "structural_candidates", {"count": len(structures), "items": [c["struct_id"] for c in structures]})
            fallback_highlights = [Highlight(f"h{i+1}", [d.claim_id], d.supported_statement or "", "由可用陈述覆盖", d.evidence_refs, "main" if i == 0 else "backup", d.supported_statement or "") for i, d in enumerate(allowed[:3])]
            highlight_raw = self._call(task_id, "N07", "highlight", {
                "video": to_dict(video), "claims": to_dict(understanding.claims),
                "semantic_units": to_dict(understanding.semantic_units),
                "unit_decisions": to_dict(boundary.unit_decisions),
                "events": to_dict(understanding.events),
                "turns": to_dict(understanding.turns),
                "structural_candidates": structures,
                "boundary": to_dict(boundary), "matched_rule_ids": rule_bundle.rule_ids,
            }, "HighlightSet", rules=rule_bundle.as_context())
            if bool(highlight_raw.get("abandon") or highlight_raw.get("no_viable_highlight")):
                # 没有结构候选就没有程序兜底：看点缺失即明确停下，不补造主语或关系。
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
            # 主旨契约：从 N07 输出规范化并绑定看点。缺失/悬空只记录，不静默用单条 claim 造主旨。
            thesis_contract, thesis_errors = normalize_thesis_contract(highlight_raw, understanding, boundary)
            demoted_core = _enforce_single_mainline(thesis_contract, understanding, video)
            if demoted_core:
                state["mainline_demoted_units"] = demoted_core
                self.storage.event(task_id, "N07", "mainline_demoted", {"kept": thesis_contract.get("core_unit_ids"), "demoted": demoted_core})
            state["thesis_contract"] = thesis_contract
            state["thesis_contract_errors"] = thesis_errors
            if thesis_errors:
                self.storage.event(task_id, "N07", "thesis_contract_incomplete", {"errors": thesis_errors})
            # 先用模型自报的 thesis_id 做对齐校验，再给未标注的看点补齐——
            # 先覆盖后校验会让"主看点绑错主旨"永远查不出来（等于没校验）。
            alignment_errors = validate_highlight_alignment(highlights, thesis_contract) if not thesis_errors else []
            state["thesis_alignment_errors"] = alignment_errors
            if alignment_errors:
                self.storage.event(task_id, "N07", "thesis_alignment_mismatch", {"errors": alignment_errors})
            # 主看点被明确标成 support/peripheral 正是"边角顶替主旨"的路径：这类候选不能普通推荐。
            # 空 relation 视为旧数据或未标注，只记日志不改路由，避免把历史样本一律打死。
            main_highlight = next((h for h in highlights if h.role == "main"), None)
            state["main_highlight_relation"] = str(getattr(main_highlight, "relation", "") or "")
            state["highlight_selection"] = {"abandoned": False, "eliminated": highlight_raw.get("eliminated", []) if isinstance(highlight_raw.get("eliminated"), list) else []}
            state["highlights"] = to_dict(highlights)
            state["prompt_hashes"]["highlight"] = self._last_prompt_hash(task_id, "N07", "highlight")
            self.storage.checkpoint(task_id, "generating", state, "N07", "highlights", {"highlight_count": len(highlights)})
            candidates = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, 0)
            # 纯机械缺陷先走独立通道修一遍：这一步在「有没有候选合格」之前，
            # 因为超字数被判 repairable 的候选正是被「已有 ready 就 handoff」丢掉的。
            candidates = self._repair_mechanical_pass(task_id, state, video, understanding, boundary, highlights, candidates, 0)
            if candidates["route"]["action"] == "rewrite":
                state["reviews"] = candidates["reviews"]
                for round_no in range(1, min(2, self.config.max_rewrite_rounds) + 1):
                    state["rewrite_round"] = round_no
                    targets = candidates["route"].get("rewrite_targets") or [
                        {"candidate_id": candidates["route"].get("rewrite_candidate_id"), "mode": candidates["route"].get("rewrite_mode", "表达")}
                    ]
                    round_decisions: list[dict[str, Any]] = []
                    rewritten_ids: list[str] = []
                    # 子结果明确要求人审/拒绝的降级信号（内容性质待审、核心事实未决等）不能因外层
                    # 只用 decisions 重新聚合就被 handoff 掩盖：这些信号只落在 route 上，不在 decisions 里。
                    escalations: list[dict[str, Any]] = []
                    if any(t.get("mode") == "看点" for t in targets):
                        if state.get("highlight_reselection_attempted"):
                            candidates["route"] = {"action": "needs_review", "reason": "HIGHLIGHT_RESELECTION_LIMIT", "recommended_candidate_id": None, "eligible_candidate_ids": []}
                            break
                        state["highlight_reselection_attempted"] = True
                        retry_raw = self._call(task_id, "N07", "highlight", {
                            "video": to_dict(video), "claims": to_dict(understanding.claims),
                            "semantic_units": to_dict(understanding.semantic_units), "unit_decisions": to_dict(boundary.unit_decisions),
                            "events": to_dict(understanding.events), "turns": to_dict(understanding.turns),
                            "structural_candidates": structures, "boundary": to_dict(boundary), "matched_rule_ids": rule_bundle.rule_ids,
                            "previous_thesis_contract": state.get("thesis_contract", {}), "independent_thesis_contract": state.get("review_thesis_contract", {}),
                            "review_issues": [i for r in candidates["reviews"] for i in r.get("issues", [])],
                        }, "HighlightSet", rules=rule_bundle.as_context())
                        retry_contract, retry_errors = normalize_thesis_contract(retry_raw, understanding, boundary)
                        retry_highlights = _normalize_highlights(retry_raw, {d.claim_id for d in allowed}, [])
                        retry_demoted = _enforce_single_mainline(retry_contract, understanding, video)
                        retry_errors += validate_highlight_alignment(retry_highlights, retry_contract)
                        if retry_raw.get("abandon") or retry_raw.get("no_viable_highlight") or retry_errors:
                            state["highlight_reselection_errors"] = retry_errors or ["无法选择可交付主看点"]
                            candidates["route"] = {"action": "needs_review", "reason": "HIGHLIGHT_RESELECTION_INVALID", "recommended_candidate_id": None, "eligible_candidate_ids": []}
                            break
                        highlights = retry_highlights
                        # 收敛后的契约已完成对齐校验，不能先校验再改变核心集合。
                        if retry_demoted:
                            state["mainline_demoted_units"] = retry_demoted
                            self.storage.event(task_id, "N07", "mainline_demoted", {"kept": retry_contract.get("core_unit_ids"), "demoted": retry_demoted, "source": "highlight_reselected"})
                        state["thesis_contract"] = retry_contract
                        state["thesis_contract_errors"] = []
                        state["thesis_alignment_errors"] = []
                        retry_main = next((h for h in highlights if h.role == "main"), None)
                        state["main_highlight_relation"] = str(getattr(retry_main, "relation", "") or "")
                        state["highlights"] = to_dict(highlights)
                        self.storage.event(task_id, "N07", "highlight_reselected", {"thesis_id": retry_contract["thesis_id"]})
                        # 主旨发生修正后，旧候选不能在新契约下被复活；重新生成并独立评审。
                        refreshed = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, round_no)
                        candidates = refreshed
                        state["reviews"] = refreshed["reviews"]
                        if refreshed["route"]["action"] != "rewrite":
                            break
                        continue
                    for target in targets:
                        source = next((c for c in candidates["candidates"] if c["candidate_id"] == target.get("candidate_id")), None)
                        if source is None:
                            continue
                        depth = _rewrite_depth(source, candidates["candidates"])
                        if depth >= min(2, self.config.max_rewrite_rounds):
                            continue
                        rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, depth + 1, source, known_candidates=candidates["candidates"])
                        candidates["candidates"] += rewritten["candidates"]
                        candidates["reviews"] += rewritten["reviews"]
                        candidates["decisions"] += rewritten["decisions"]
                        round_decisions += rewritten["decisions"]
                        rewritten_ids += [c.get("candidate_id") for c in rewritten["candidates"]]
                        if rewritten["route"].get("action") in {"needs_review", "reject"}:
                            escalations.append(rewritten["route"])
                    state["reviews"] = candidates["reviews"]
                    if not round_decisions:
                        candidates["route"] = {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "REWRITE_PRODUCED_NO_CANDIDATE", "rewrite_round": round_no}
                        break
                    if escalations:
                        reasons = "；".join(str(e.get("reason")) for e in escalations if e.get("reason"))
                        pending = [s for e in escalations for s in (e.get("pending_signal_ids") or [])]
                        # 一条重写子路由要人工核实，不该把同一轮里已经合格的候选一起拖下水。
                        # 真实故障（2026-11-02 R09 复跑）：cand-1-r1-1 / cand-2-r1-1 两条
                        # G1–G4 全过、质量分满分，只因为同批的 cand-3-r1-1 评审输出结构损坏
                        # （REVIEW_SCHEMA_INVALID）就整批升级，交付面直接清零。
                        # needs_review 的候选本来就能通过 pending_review_candidate_ids 与 ready
                        # 候选同处一个 handoff 路由，交给 _route_candidates 统一裁决即可。
                        # 只有政策驳回（reject）或这一轮一条 ready 都没有时，才整批停下。
                        # pending_signal_ids 是未解决的风险信号（内容性质未披露等），它可以
                        # 否决整批交付——那不是"某条评审崩了"，而是"这批材料有未闭合的风险"。
                        blocked = [e for e in escalations if e.get("action") == "reject"]
                        ready_ids = [d["candidate_id"] for d in round_decisions if d.get("status") == "ready_for_human_review"]
                        if blocked or pending or not ready_ids:
                            candidates["route"] = {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": f"SUBROUTE_ESCALATION: {reasons}" or "SUBROUTE_ESCALATION", "rewrite_round": round_no, "pending_signal_ids": sorted(set(pending))}
                            break
                        self.storage.event(task_id, "Q01", "subroute_escalation_merged", {"reasons": reasons, "pending_signal_ids": sorted(set(pending)), "kept_ready": ready_ids})
                    # 只看本轮：用累积的 candidates["decisions"] 会把轮 0 那些仍是 rewrite 的
                    # 父候选重新选成 rewrite_targets，同一条父候选被反覆重写。
                    candidates["route"] = self._route_candidates(round_decisions, round_no)
                    if candidates["route"]["action"] != "rewrite":
                        break
                if candidates["route"]["action"] == "rewrite":
                    candidates["route"] = {**candidates["route"], "action": "stop", "rewrite_candidate_id": None, "rewrite_targets": [], "reason": "REWRITE_BUDGET_EXHAUSTED"}
            # 输入质量闸：材料只够支撑"基线改写"时，不允许自动 handoff 一条自认为合格的标题
            if material.get("level") == "low" and candidates["route"].get("reason") == "INPUT_LOW_CONFIDENCE" and bool(self.config.extra.get("require_human_on_low_quality_input", True)):
                for decision in candidates["decisions"]:
                    if decision.get("status") == "ready_for_human_review":
                        decision["status"] = "needs_review"
                        decision["reason"] = "INPUT_LOW_CONFIDENCE"
                candidates["route"] = {**candidates["route"], "action": "needs_review", "recommended_candidate_id": None, "reason": "INPUT_LOW_CONFIDENCE"}
                self.storage.event(task_id, "Q01", "input_quality_downgrade", {"level": material.get("level"), "reasons": material.get("reasons", [])})
            # 两套独立主旨契约对不上时先尝试按更硬的一侧重出（只有复核侧踩在原始材料上
            # 才会重出），再决定要不要摘推荐章；两层都必须在槽位交付之前，否则「已交付」
            # 的标签会盖在一份系统自己都没读准的主旨上。
            candidates, _regen = self._regenerate_on_review_thesis(task_id, state, video, understanding, boundary, highlights, structures, rule_bundle, allowed, candidates)
            candidates["route"] = self._downgrade_on_contract_disagreement(task_id, state, candidates["route"])
            # 三槽交付：每槽取 ready 候选（按质量分），缺额显式记录，不复制标题或用边角凑数。
            delivery, slot_seen = _slot_delivery(candidates["candidates"], candidates["decisions"], _delivery_thesis_ids(state),
                                                 prefer_basis="review" if (state.get("thesis_regeneration") or {}).get("status") == "ok" else None)
            candidates["route"] = _apply_slot_delivery(candidates["route"], delivery)
            if candidates["route"].get("action") != "handoff":
                delivery, slot_seen = [], set()
            state["delivery_candidates"] = delivery
            state["delivery_slots"] = _slot_status(delivery, slot_seen)
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
        """在新建的独立数据库中重评，返回该数据库路径。"""
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        frozen = _freeze_rereview_candidates(state.get("candidates"))
        snapshot = state.get("video")
        if not isinstance(snapshot, dict) or snapshot.get("video_id") != row["video_id"] or not isinstance(snapshot.get("evidence"), list):
            raise ValueError("原始视频快照不可重评：视频快照缺失或身份不一致")
        try:
            VideoInput(**{**snapshot, "evidence": [Evidence(**item) for item in snapshot["evidence"]]})
        except (TypeError, ValueError) as exc:
            raise ValueError(f"原始视频快照不可重评：{exc}") from exc
        root = Path(self.config.output_dir) / "rereviews"
        root.mkdir(parents=True, exist_ok=True)
        db_path = Path(tempfile.mkdtemp(prefix="rereview-", dir=root)) / "agent.db"
        isolated = Storage(db_path)
        try:
            # 隔离库要能自证来源：记下源库路径与本次使用的模型/提示词版本，
            # 否则事后无法区分「当时重评用的是哪套模型」。
            isolated_state = {**state, "rereview_isolation": {
                "source_database_path": self.storage.path,
                "task_id": task_id,
                "video_snapshot_sha256": hashlib.sha256(
                    json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "generation_model": self.config.generation_model,
                "review_model": self.config.review_model,
                "prompt_pack_version": self.config.prompt_pack_version,
                "rubric_version": self.config.rubric_version,
                "frozen_candidates": frozen,
            }}
            isolated.conn.execute("INSERT INTO tasks(task_id,video_id,status,config_json,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (
                row["task_id"], row["video_id"], row["status"], row["config_json"],
                json.dumps(isolated_state, ensure_ascii=False), row["created_at"], row["updated_at"],
            ))
            isolated.conn.execute("CREATE TABLE rereview_snapshot(task_id TEXT PRIMARY KEY, candidates_json TEXT NOT NULL)")
            isolated.conn.execute("INSERT INTO rereview_snapshot VALUES(?,?)", (task_id, json.dumps(frozen, ensure_ascii=False)))
            isolated.conn.commit()
            isolated_pipeline = copy.copy(self)
            isolated_pipeline.storage = isolated
            isolated_pipeline._rereview_isolated(task_id)
        except BaseException:
            # 重评失败不留半成品库：否则 runtime/rereviews 会被无主目录塞满，
            # 事后也分不清哪个是可用结果、哪个是崩到一半的残留。
            isolated.conn.close()
            shutil.rmtree(db_path.parent, ignore_errors=True)
            raise
        isolated.conn.close()
        return str(db_path)

    def _rereview_isolated(self, task_id: str) -> str:
        """只接受已冻结快照的隔离库；所有模型事件和结果均写入该库。"""
        # 隔离必须是结构性的：只看「表存不存在」意味着任何人往原库手动插一张
        # rereview_snapshot 表，就能让重评把事件和结果直接写回原库，隔离形同虚设。
        root = (Path(self.config.output_dir) / "rereviews").resolve()
        try:
            Path(self.storage.path).resolve().relative_to(root)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"重新评审必须在隔离库中执行，源库不得直接重评：{self.storage.path}") from exc
        try:
            frozen_row = self.storage.conn.execute("SELECT candidates_json FROM rereview_snapshot WHERE task_id=?", (task_id,)).fetchone()
        except Exception as exc:
            raise ValueError("缺少隔离重评快照") from exc
        if not frozen_row:
            raise ValueError("缺少隔离重评快照")
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        try:
            frozen = json.loads(frozen_row[0])
            if not frozen:
                raise ValueError("冻结候选快照为空")
            drift = _rereview_identity_drift(state.get("candidates"), frozen)
            if drift:
                raise ValueError(f"冻结候选身份或标题哈希不一致：{'；'.join(drift)}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("冻结候选快照无效") from exc
        if self.config.extra.get("release_gate_dir"):
            from .release_gate import check_active_release
            from .repair_release import reviewed_ids
            gate = check_active_release(Path(__file__).resolve().parents[1], self.config.extra["release_gate_dir"])
            if not gate['allowed'] or gate['status'] != 'active':
                raise ValueError('重新评审被发布门禁阻止')
            reviewed_ids(str(self.config.extra.get('repair_ledger') or 'runtime/repair/repair.db'), gate['proposal_ids'])
        snapshot = state.get("video")
        try:
            if not isinstance(snapshot, dict) or snapshot.get("video_id") != row["video_id"] or not isinstance(snapshot.get("evidence"), list):
                raise ValueError("视频快照缺失或身份不一致")
            video = VideoInput(**{**snapshot, "evidence": [Evidence(**item) for item in snapshot["evidence"]]})
        except (TypeError, ValueError) as exc:
            raise ValueError(f"原始视频快照不可重评：{exc}") from exc
        signal_report = extract_risk_signals(video)
        state["risk_signals"] = signal_report
        understanding = _normalize_understanding(state.get("understanding", {}))
        # 与初次生成同口径：标签降级必须在重评里重算，不能靠历史记录已经降过。
        semantic_bindings = _bind_semantic_use_to_evidence(understanding, video)
        if semantic_bindings:
            state["semantic_use_bindings"] = semantic_bindings
            self.storage.event(task_id, "N03", "semantic_use_downgraded", {"units": semantic_bindings, "source": "rereview"})
        state["understanding"] = to_dict(understanding)
        required = normalize_prefix_labels([], required_prefixes(format=video.format, attributes=understanding.attributes, domain=video.domain, evidence=video.evidence) + signal_report["required_prefixes"])
        state["required_prefixes"] = required
        boundary = _normalize_boundary(state.get("boundary", {}))
        state["reference_audit"] = audit_evidence(understanding, video, boundary)
        evidence_types = {e.evidence_id: e.quality for e in video.evidence}
        state["source_only_claim_ids"] = [c.claim_id for c in understanding.claims if c.evidence_refs and all(evidence_types.get(r) == "source" for r in c.evidence_refs)]
        boundary, _ = _apply_primary_evidence_guard(boundary, _primary_evidence_audit(understanding, video), understanding)
        state["boundary"] = to_dict(boundary)
        # 重评同样要按边界收窄可引用单元，否则已排除单元会在重评里被放行。
        # 集合构造与初评共用 _allowed_unit_ids：两边各写一套时，结构片段的 turn-N 只在
        # 一边被认，候选就被判成「任务范围外」（2026-11-03 复测的 4 条误拦）。
        rereview_units = _allowed_unit_ids(understanding, boundary, state.get("highlights") or [])
        for c in state["candidates"]:
            # 重评必须与初次生成共用同一个有效集合：待核验的 claim 不能因为"再评一次"就重新进白名单
            c["mechanical_checks"] = _mechanical_check(c, allowed_claims={d.claim_id for d in boundary.decisions if d.decision != "excluded" and not d.verification_required}, allowed_highlights={h["highlight_id"] for h in state.get("highlights", [])}, evidence_ids={e.evidence_id for e in video.evidence}, max_chars=self.config.max_chars, allowed_units=rereview_units, required_prefixes=required, claim_decisions=boundary.decisions)
            if c.get("slot_id") == "core_hook":
                snapshot_highlights = state.get("highlights") or []
                main_hl = next((h.get("highlight_id") for h in snapshot_highlights if h.get("role") == "main"), None)
                if main_hl and not _core_hook_is_aligned(c, snapshot_highlights, state.get("thesis_contract") or {}):
                    c["mechanical_checks"]["errors"].append({
                        "code": "G4_HOOK_OFF_MAINLINE", "repairable": True,
                        "reason": f"核心钩子必须绑定主看点 {main_hl}，不得换成其它片段（当前 {c.get('highlight_id')}）"})
            # 只要 mechanical_checks 里出现判否项，status 就必须跟着变 fail：
            # 早先 G4_HOOK_OFF_MAINLINE 只往 errors 里塞一条、没改 status，
            # 于是评审台标「判否」、决策却当「合格可交付」，两处口径互相打架。
            if c["mechanical_checks"].get("errors"):
                c["mechanical_checks"]["status"] = "fail"
            refs = {r for claim in understanding.claims if claim.claim_id in c.get("claim_ids", []) for r in claim.evidence_refs}
            errors = check_signal_constraints(c.get("headline", ""), signal_report, understanding.claims, c.get("claim_ids", []))
            texts = [e.text for e in video.evidence if e.evidence_id in refs and e.field != "frame" and e.quality not in {"model_derived_uncalibrated", "unverified_source_metadata"}]
            if unsupported_numeric_facts(c.get("headline", ""), texts):
                errors.append({"code": "G4_NUMERIC_CONFLICT", "repairable": True, "reason": "重新评审发现数字、单位或金额口径没有引用证据支持（如原文「400亿美元」写成「400亿」）"})
            c["mechanical_checks"]["errors"].extend(errors)
            if errors:
                c["mechanical_checks"]["status"] = "fail"
            required_claims = {d.claim_id for d in boundary.decisions if d.attribution_required}
            c["programmatic_g1_flags"] = []
            rr_phrases = [d.required_source_phrase for d in boundary.decisions
                          if d.attribution_required and d.claim_id in set(c.get("claim_ids", []))]
            if _declares_narration(c.get("prefix_labels")) and rr_phrases and all(_carrier_only_source(x) for x in rr_phrases):
                required_claims = set()
            if required_claims.intersection(c.get("claim_ids", [])) and not _has_explicit_source_relation(c.get("headline", ""), rr_phrases) \
                    and not _attribution_identity_present(c.get("headline", ""), rr_phrases):
                c["programmatic_g1_flags"].append({"code": "G1_SOURCE_ATTRIBUTION_MISSING", "repairable": True, "reason": "本条边界明确要求来源归属，但标题未表达转述关系"})
        checks = [{"candidate_id": c.get("candidate_id"), **c["mechanical_checks"], "programmatic_g1_flags": c.get("programmatic_g1_flags", [])} for c in state["candidates"]]
        review_raw = self._review_with_fallback(task_id, video, state, state["candidates"], checks)
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in state["candidates"]])
        reviews, decisions = self._validate_and_decide_reviews(video, state, state["candidates"], reviews, checks, task_id)
        route = self._route_candidates(decisions, state.get("rewrite_round", 0))
        # 重评只重新裁决既有候选，不生成新标题：与初次生成一致，重写预算用尽即停下，
        # 不能因为「再评一次」又重新打开重写循环。
        if route.get("action") == "rewrite":
            route = {**route, "action": "stop", "rewrite_candidate_id": None, "rewrite_targets": [], "reason": "REWRITE_BUDGET_EXHAUSTED"}
        signal_report = extract_risk_signals(video)
        state["risk_signals"] = signal_report
        route = self._guard_handoff(route, state, state["candidates"])
        drift = _rereview_identity_drift(state.get("candidates"), frozen)
        if drift:
            raise ValueError(f"重评期间候选身份或标题发生变化：{'；'.join(drift)}")
        # 交付口径与初次生成同一套：状态里旧的交付清单不能留着继续声称「已交付」。
        delivery, slot_seen = _slot_delivery(state["candidates"], decisions, state.get("thesis_contract", {}).get("thesis_id"))
        route = _apply_slot_delivery(route, delivery)
        if route.get("action") != "handoff":
            delivery, slot_seen = [], set()
        state["delivery_candidates"] = delivery
        state["delivery_slots"] = _slot_status(delivery, slot_seen)
        state["reviews"], state["route"] = reviews, route
        state["candidate_decisions"] = decisions
        self.storage.save_reviews(task_id, reviews)
        self.storage.set_state(task_id, "pending_human" if route["action"] in {"handoff", "needs_review"} else "stopped", state)
        self.storage.event(task_id, "R01", "rereview_complete", {"route": route})
        return task_id

    def _review_input(self, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], mechanical_checks: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "video_materials": {"title": video.title, "author": video.author, "domain": video.domain, "format": video.format, "description": video.description, "caption": video.caption, "asr_segments": video.asr_segments, "ocr_segments": video.ocr_segments},
            "evidence": to_dict(video.evidence),
            "claims": state.get("understanding", {}).get("claims", []),
            "risk_signals": state.get("risk_signals", {}),
            "boundary": state.get("boundary", {}),
            # 暂缓核验/已排除的陈述清单：生成侧一直有，评审侧此前缺失——模型不知道
            # 哪些主张被暂缓，无法判断「标题是否把它写成了断言」。G1 若 pass，
            # 程序还会把 needs_review 压回去（_decide_candidate 的 blocked_refs 分支），
            # 但评审自己看到清单才能给出有意义的理由。
            "blocked_claims": [
                {"claim_id": d.get("claim_id"), "text": next((c.get("claim_text") or c.get("text") or "" for c in state.get("understanding", {}).get("claims", []) if c.get("claim_id") == d.get("claim_id")), ""),
                 "reason": "暂缓核验" if d.get("verification_required") else "已排除", "unresolved": bool(d.get("verification_required"))}
                for d in (state.get("boundary") or {}).get("decisions", [])
                if d.get("decision") == "excluded" or d.get("verification_required")
            ],
            "highlights": state.get("highlights", []),
            "thesis_contract": state.get("thesis_contract", {}),
            "review_thesis_contract": state.get("review_thesis_contract", {}),
            "matched_rule_ids": (state.get("rules") or {}).get("rule_ids", []),
            "business_policy": state.get("business_policy", {"status": "missing", "rules": [], "version": None}),
            "display_config": {"max_chars": self.config.max_chars, "prefix_labels": state.get("required_prefixes", []), "counting": "前缀与正文合计不超过 max_chars", "ending_question_mark_forbidden": True},
            "mechanical_checks": mechanical_checks,
            # 限定必答项：机械层登记的每条限定，评审必须在 limit_checks 里逐条给出
            # satisfied / violated / unclear + reason。这是 2026-11-04 契约变更的对称面：
            # 词法不再判语义，漏答在结构上不允许。
            "limit_instructions": {
                "requirement": "对 mechanical_checks[*].limit_requirements 里的每一条限定，在对应候选的 limit_checks 数组中逐条回答",
                "verdicts": "satisfied（标题已满足）/ violated（标题违反）/ unclear（证据不足无法判定）",
                "note": "词法层已不再代答；判定权在评审，漏答会被单独重问",
                "requirements_by_candidate": [
                    {"candidate_id": c.get("candidate_id"),
                     "limits": [r for r in (next((m for m in mechanical_checks if str(m.get("candidate_id") or "") == str(c.get("candidate_id") or "")), {}).get("limit_requirements") or [])]}
                    for c in candidates
                ],
            },
            "candidate_titles": [{"candidate_id": c.get("candidate_id"), "title": c.get("headline"), "prefix_labels": c.get("prefix_labels", []), "rendered_title": c.get("rendered_title") or render_title(c), "claim_ids": c.get("claim_ids", []), "highlight_id": c.get("highlight_id"), "evidence_ids": c.get("evidence_ids", []), "strategy": c.get("expression_strategy"), "qualifiers_used": c.get("qualifiers_used", [])} for c in candidates],
            "rubric": rubric(),
        }
        if getattr(self.config, "review_input_scope", "full") == "compact":
            payload = _compact_review_payload(payload)
        return payload

    def _retry_single_review(self, task_id: str, video: Any, state: dict[str, Any], candidate: dict[str, Any], checks: dict[str, Any] | None, errors: list[str]) -> dict[str, Any] | None:
        """整批通过但某条候选结构不合格时，单独重问这一条一次。

        实测：三条一起评时模型整体返回合法，cand-1 却漏了 quality；整批校验按「一条坏、整批降级」
        处理会直接丢掉一条机械检查干净的候选。这里只重问坏的那条，并把它自己的错误带回去。
        """
        try:
            user = self._review_input(video, state, [candidate], [checks] if checks else [])
            user["_previous_output_error"] = "；".join(str(e) for e in errors)[:800]
            user["_instruction"] = ("上一次回复校验失败（见 _previous_output_error）。补齐 gates 和 quality；"
                                    "G3 对照摘录必须逐字来自其引用的原始证据，触发片段来自完整标题。"
                                    "证据不足就标待核，不得编造原文。")
            raw = self._call(task_id, "R01", "review", user, "ReviewResultBatch", self.review_adapter, rules=state.get("rules"))
        except ModelError as exc:
            if not exc.retryable:
                raise
            self.storage.event(task_id, "R01", "review_candidate_retry_failed",
                               {"candidate_id": candidate.get("candidate_id"), "error": str(exc)[:200]})
            return None
        normalized = _normalize_reviews(raw, [str(candidate.get("candidate_id"))])
        self.storage.event(task_id, "R01", "review_candidate_retry", {"candidate_id": candidate.get("candidate_id")})
        return normalized[0] if normalized else None

    def _validate_and_decide_reviews(self, video: Any, state: dict[str, Any], candidates: list[dict[str, Any]], reviews: list[dict[str, Any]], mechanical_checks: list[dict[str, Any]], task_id: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
            cited_evidence = {r for claim in state.get("understanding", {}).get("claims", []) if claim.get("claim_id") in candidate.get("claim_ids", []) for r in claim.get("evidence_refs", [])}
            candidate["signal_review_reasons"] = signal_review_reasons(candidate.get("headline", ""), state.get("risk_signals") or extract_risk_signals(video), cited_evidence)
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
                previous_g1 = review["gates"].get("G1") or {}
                if previous_g1.get("status") not in {"reject", "needs_review"}:
                    review["gates"]["G1"] = {**previous_g1, "status": "revise", "reason": "可识别信源缺少明确转述关系"}
            # 限定必答项的覆盖校验：机械层登记的每条 requirement 都必须有评审判定。
            # 漏答作为 validation_error 进已有重问通道（单条重问，带错误说明），
            # 重问后仍缺 → unclear 语义，_decide_candidate 会给 needs_review。
            limit_gaps = _limit_check_coverage_errors(review, mechanical)
            validation_errors = validate_review(review, evidence_ids=evidence_ids, rule_ids=rule_ids, issue_codes=set(rubric().get("issue_codes", [])), policy_rule_ids=policy_rule_ids) + limit_gaps
            comparison_errors = _fresh_g3_comparison_errors(review, candidate, video.evidence) if task_id else []
            policy = state.get("business_policy", {"status": "missing"})
            explicit_policy_reject = _explicit_policy_rejection(review, policy)
            if (validation_errors or comparison_errors) and task_id and not review.get("_candidate_retried") and not explicit_policy_reject:
                # 对照错误也只重问当前候选，不把来源不明当作标题的机械缺陷。
                retried = self._retry_single_review(task_id, video, state, candidate, checks_by_id.get(cid), validation_errors + comparison_errors)
                if retried is not None:
                    retried["_candidate_retried"] = True
                    review = retried
                    review.setdefault("_normalization_errors", [])
                    if not isinstance(review.get("issues"), list):
                        review["issues"] = []
                    if not isinstance(review.get("rewrite_guidance"), dict):
                        review["rewrite_guidance"] = {"preserve": [], "modify": [], "forbidden": []}
                    if not isinstance(review.get("gates"), dict):
                        review["gates"] = {}
                    if any(x.get("code") == "G1_SOURCE_ATTRIBUTION_MISSING" for x in programmatic_g1_flags):
                        previous_g1 = review["gates"].get("G1") or {}
                        if previous_g1.get("status") not in {"reject", "needs_review"}:
                            review["gates"]["G1"] = {**previous_g1, "status": "revise", "reason": "可识别信源缺少明确转述关系"}
                    validation_errors = validate_review(review, evidence_ids=evidence_ids, rule_ids=rule_ids, issue_codes=set(rubric().get("issue_codes", [])), policy_rule_ids=policy_rule_ids) + _limit_check_coverage_errors(review, mechanical)
                    comparison_errors = _fresh_g3_comparison_errors(review, candidate, video.evidence)
                    explicit_policy_reject = _explicit_policy_rejection(review, policy)
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
            # 程序侧命中的业务政策（目前只有 P9 人身攻击词）：登记在 G2 下，与模型报出的
            # 业务政策问题同一个维度、同一个 issue_code（G2_POLICY_VIOLATION），不另开一档。
            if mechanical.get("policy_hits"):
                _merge_program_policy_hits(review.setdefault("gates", {}), mechanical)
            for hit in mechanical.get("policy_hits") or []:
                if not isinstance(hit, dict) or not hit.get("rule_id"):
                    continue
                rid = str(hit["rule_id"])
                issue = {
                    "issue_id": f"{cid}-{hit.get('code') or 'G2_POLICY_VIOLATION'}-{rid}",
                    "primary_dimension": "G2",
                    "issue_code": "G2_POLICY_VIOLATION",
                    "severity": "high",
                    "repairable": bool(hit.get("repairable", True)),
                    "problem_span": candidate.get("headline", ""),
                    "evidence_ids": [], "rule_ids": [rid],
                    "reason": hit.get("reason", "标题命中业务政策禁用内容"),
                    "action": "按业务政策改写：保留可写看点，删除禁用表述后重新完整评审",
                }
                if not any(i.get("issue_code") == issue["issue_code"] and rid in (i.get("rule_ids") or [])
                           for i in review.get("issues", []) if isinstance(i, dict)):
                    review.setdefault("issues", []).append(issue)
                guidance = review.setdefault("rewrite_guidance", {"preserve": [], "modify": [], "forbidden": []})
                guidance.setdefault("modify", []).append(hit.get("reason") or "删除业务政策禁用表述")
                guidance.setdefault("forbidden", []).extend(str(t) for t in (hit.get("terms") or []))
            for flag in programmatic_g1_flags:
                issue = {"issue_id": f"{cid}-{flag.get('code')}", "primary_dimension": "G1", "issue_code": flag.get("code"), "severity": "high", "repairable": bool(flag.get("repairable")), "problem_span": candidate.get("headline", ""), "evidence_ids": [], "rule_ids": [], "reason": flag.get("reason", "G1 程序规则校验失败"), "action": "补充明确的信源转述关系后重新评审"}
                if not any(i.get("issue_code") == issue["issue_code"] for i in review.get("issues", []) if isinstance(i, dict)):
                    review.setdefault("issues", []).append(issue)
            # 两项独立判断都不能与 pass 同时成立；只处理模型明确给出的 False，
            # 不把缺失字段、无法判断或关键词命中冒充语义结论。
            g3_gate = review.get("gates", {}).get("G3", {})
            if isinstance(g3_gate, dict):
                comparison = g3_gate.get("promise_comparison")
                if isinstance(comparison, dict):
                    alignment = comparison.get("alignment")
                    if alignment == "mismatched":
                        g3_gate["delivery_supported"] = False
                    elif alignment == "uncertain" and g3_gate.get("status") == "pass":
                        g3_gate["status"] = "needs_review"
                failures = []
                if g3_gate.get("core_covered") is False:
                    failures.append(("G3_CORE_POINT_OMITTED", "标题未覆盖视频核心观看承诺", "回到看点选择，改用核心主旨表达", "N07"))
                if g3_gate.get("delivery_supported") is False:
                    failures.append(("G3_NOT_DELIVERED", "视频未兑现标题的观看承诺", "调整表达为材料实际交付的内容", "N09"))
                if failures and g3_gate.get("status") == "pass":
                    review["gates"]["G3"] = {**g3_gate, "status": "revise"}
                for code, fallback_reason, action, rollback in failures:
                    issue = {"issue_id": f"{cid}-{code}", "primary_dimension": "G3", "issue_code": code, "severity": "high", "repairable": True, "problem_span": candidate.get("headline", ""), "evidence_ids": g3_gate.get("evidence_ids", []) or [], "rule_ids": g3_gate.get("rule_ids", []) or [], "reason": str(g3_gate.get("reason") or fallback_reason), "action": action, "rollback_to": rollback}
                    if not any(i.get("issue_code") == code for i in review.get("issues", []) if isinstance(i, dict)):
                        review.setdefault("issues", []).append(issue)
            decision = _decide_candidate(review, mechanical, schema_errors=validation_errors, business_policy=policy)
            if explicit_policy_reject:
                decision.update(status="reject", reason="EXPLICIT_BUSINESS_POLICY_REJECTION", repairable=False)
            elif comparison_errors and decision["status"] != "reject":
                decision.update(status="needs_review", reason="G3_COMPARISON_UNVERIFIED", repairable=False)
                decision["gates"]["G3"] = {**(decision["gates"].get("G3") or {}), "status": "needs_review", "reason": "对照或摘录来源未能核实"}
            review["comparison_errors"] = comparison_errors
            # 信号限定词表只作观测，不再一票否决自动交付：
            # 「如果/不能/没有」这类口语假设词出现在 ASR 里不等于标题必须复现；
            # 本批实测 6 条人工判可用被此路径误拦。RELATIVE_TIME_UNVERIFIED 仍保留拦截。
            signal_blocks = [x for x in (candidate.get("signal_review_reasons") or []) if x.get("code") != "QUALIFIER_RETENTION_UNVERIFIED"]
            if signal_blocks and decision["status"] == "ready_for_human_review":
                decision.update(status="needs_review", reason="SIGNAL_SEMANTICS_UNVERIFIED", repairable=False)
                decision["signal_review_reasons"] = candidate["signal_review_reasons"]
            review["signal_review_reasons"] = candidate.get("signal_review_reasons", [])
            review["mechanical_checks"] = mechanical
            review["validation_errors"] = validation_errors
            review["decision"] = decision["status"]
            review["quality_sum"] = decision["quality_sum"]
            review["gates"] = decision["gates"]
            normalized_reviews.append(review)
            decisions.append(decision)
        return normalized_reviews, decisions

    def _reconcile_contracts(self, task_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
        """让模型读一次两套契约的关系，返回判定或 None（调用失败）。

        为什么值得花这次调用（离线实测 46 对契约、15 秒）：词法判据命中 14 条，其中只有
        2 条是真正的互斥——**86% 是误报**，因为真互斥（overlap=0.333）和同义详略
        （overlap=0.333）落在同一个相似度上，没有阈值分得开。同一件事交给模型，它只判了
        2 条互斥、两条全中、零误报，按人工读法 19 条确定样本对上了 18 条；它还能说清
        「漏了哪条要点」，词法只能说「低重叠」。
        """
        left, right = state.get("thesis_contract") or {}, state.get("review_thesis_contract") or {}
        if not left or not right:
            return None
        started = time.monotonic()
        model_name = str(getattr(self.review_adapter, "model", self.config.review_model))
        # 这条独立直调不经过 _call；仍须记录用量，否则批次成本会漏掉判官调用。
        self.storage.event(task_id, "Q01", "node_started", {"task": "thesis_consistency", "model": model_name})
        try:
            reply = self.review_adapter.complete(task="thesis_consistency", system=RECONCILE_SYSTEM,
                                                 user={"thesis_contract": left, "review_thesis_contract": right},
                                                 schema=RECONCILE_SYSTEM)
        except Exception as exc:  # 网络或模型故障：交给调用方按保守口径兜底
            self.storage.event(task_id, "Q01", "model_error", {"task": "thesis_consistency", "model": model_name, "error": str(exc), "duration_ms": int((time.monotonic() - started) * 1000)})
            self.storage.event(task_id, "Q01", "thesis_reconcile_failed", {"error": str(exc)})
            return None
        data = reply.data if isinstance(reply.data, dict) else {}
        self.storage.event(task_id, "Q01", "model_reply", {"task": "thesis_consistency", "model": model_name, "usage": reply.usage, "latency_ms": reply.latency_ms, "data": data})
        self.storage.event(task_id, "Q01", "node_completed", {"task": "thesis_consistency", "duration_ms": int((time.monotonic() - started) * 1000), "model_latency_ms": reply.latency_ms, "cached": False})
        return _normalize_thesis_consistency({"thesis_consistency": data})

    def _regenerate_on_review_thesis(self, task_id: str, state: dict[str, Any], video: Any, understanding: Understanding,
                                     boundary: BoundarySet, highlights: list[Highlight], structures: list[Any],
                                     rule_bundle: Any, allowed: list[Any], candidates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """两套解读分歧、且只有一侧踩在原始材料上时，按那一侧重选看点再出一组候选。

        判定谁更硬的口径是**证据是不是原始材料**：引用了 source 级证据（上传者原文/原题）
        的一侧，比整组都建在 raw_asr / model_derived_uncalibrated 上的一侧更接近事实。
        两侧同等硬（包括都只在猜）时不重出——那正是「不替谁翻案」的适用场景。

        为什么不是直接摘章了事（2026-11-02 人工复核，BV1u9HA6JEdw）：生成侧把「网友：
        她真的很欠扁样子」这条贯穿全片的叠加评价整条漏掉，复核侧抓到了、也与上传者原题
        一致；摘章让这条视频三个槽位全空，人工拿到的信息反而更少。
        """
        if not bool(self.config.extra.get("regenerate_on_review_thesis", True)):
            return candidates, None
        left, right = state.get("thesis_contract") or {}, state.get("review_thesis_contract") or {}
        if not left or not right:
            return candidates, None
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in ((state.get("video") or {}).get("evidence") or [])
            if isinstance(item, dict) and item.get("evidence_id")
        }
        verdict = contracts_disagree(left, right, evidence_by_id)
        declared = [d for d in (state.get("thesis_consistency_history") or []) if isinstance(d, dict)]
        contradiction = next((d for d in declared if d.get("relation") == "contradiction"), None)
        missing_point = next((d for d in declared if d.get("relation") == "missing_point"), None)
        if not verdict and not contradiction and not missing_point:
            return candidates, None
        reconcile = self._cached_reconcile(task_id, state)
        relation = (reconcile or {}).get("relation") or ""
        if relation in {"aligned", "detail"}:
            return candidates, None

        def source_hits(contract: dict[str, Any]) -> int:
            return sum(1 for eid in (contract.get("evidence_ids") or [])
                       if str((evidence_by_id.get(str(eid)) or {}).get("quality") or "") == "source")

        generated_hits, review_hits = source_hits(left), source_hits(right)
        state["thesis_regeneration"] = {"relation": relation or "unavailable",
                                        "generated_source_evidence": generated_hits,
                                        "review_source_evidence": review_hits}
        if review_hits <= generated_hits:
            # 生成侧不比复核侧软，或两侧一样软：不动候选，交给下游按原口径处置。
            state["thesis_regeneration"]["status"] = "not_regenerated"
            self.storage.event(task_id, "Q01", "thesis_regeneration_skipped", state["thesis_regeneration"])
            return candidates, state["thesis_regeneration"]
        retry_raw = self._call(task_id, "N07", "highlight", {
            "video": to_dict(video), "claims": to_dict(understanding.claims),
            "semantic_units": to_dict(understanding.semantic_units), "unit_decisions": to_dict(boundary.unit_decisions),
            "events": to_dict(understanding.events), "turns": to_dict(understanding.turns),
            "structural_candidates": structures, "boundary": to_dict(boundary), "matched_rule_ids": rule_bundle.rule_ids,
            "previous_thesis_contract": left, "independent_thesis_contract": right,
            "basis": "review",
            "basis_note": "以 independent_thesis_contract（复核侧独立读法）为主旨基准重新选看点；若上一版看点漏掉了该读法认为不可删的要素，必须选出承载它的看点。",
            "review_issues": [],
        }, "HighlightSet", rules=state.get("rules"))
        contract2, errors2 = normalize_thesis_contract(retry_raw, understanding, boundary)
        highlights2 = _normalize_highlights(retry_raw, {d.claim_id for d in allowed}, [])
        errors2 += validate_highlight_alignment(highlights2, contract2)
        if bool(retry_raw.get("abandon") or retry_raw.get("no_viable_highlight")) or errors2 or not highlights2:
            state["thesis_regeneration"].update({"status": "highlight_reselection_failed", "errors": errors2[:3]})
            self.storage.event(task_id, "Q01", "thesis_regeneration_failed", state["thesis_regeneration"])
            return candidates, state["thesis_regeneration"]
        _enforce_single_mainline(contract2, understanding, video)
        keep_contract, keep_highlights = state.get("thesis_contract"), state.get("highlights")
        state["thesis_contract"] = contract2
        state["highlights"] = to_dict(highlights2)
        try:
            regenerated = self._generate_and_review(task_id, state, video, understanding, boundary, highlights2, 100,
                                                    skip_thesis_review=True, thesis_basis="review")
        finally:
            state["thesis_contract"] = keep_contract
            state["highlights"] = keep_highlights
        if not regenerated.get("candidates"):
            state["thesis_regeneration"].update({"status": "no_candidate"})
            self.storage.event(task_id, "Q01", "thesis_regeneration_failed", state["thesis_regeneration"])
            return candidates, state["thesis_regeneration"]
        # 两组都会产出 cand-1/cand-2 这种模型自带 id：不拉开命名，评审台、决策表和
        # 槽位交付会按 id 互相覆盖（真跑里 6 条决策只剩 4 条候选，槽位全空）。
        taken = {str(c.get("candidate_id") or "") for c in (candidates.get("candidates") or [])}
        remap: dict[str, str] = {}
        for index, candidate in enumerate(regenerated["candidates"], 1):
            old_id = str(candidate.get("candidate_id") or "")
            new_id = f"review-{index}"
            while new_id in taken:
                new_id = f"review-{index}-{len(remap)}"
            taken.add(new_id)
            remap[old_id] = new_id
            candidate["candidate_id"] = new_id
        for item in list(regenerated.get("reviews") or []) + list(regenerated.get("decisions") or []):
            current = str(item.get("candidate_id") or "")
            if current in remap:
                item["candidate_id"] = remap[current]
            parent = str(item.get("parent_candidate_id") or "")
            if parent in remap:
                item["parent_candidate_id"] = remap[parent]
        for candidate in regenerated["candidates"]:
            candidate["thesis_basis"] = "review"
        merged = {
            "candidates": list(candidates.get("candidates") or []) + list(regenerated["candidates"]),
            "reviews": list(candidates.get("reviews") or []) + list(regenerated.get("reviews") or []),
            "decisions": list(candidates.get("decisions") or []) + list(regenerated.get("decisions") or []),
            "route": candidates.get("route") or {},
        }
        # 外层 route 是按旧组候选算的：handoff 时它的 eligible 只有旧组 id，重出组进不了
        # 交付（真实跑里 4 条 ready、槽位全空，最后判成 NO_VALID_DELIVERY_SLOT）；
        # 停在 stop/rewrite 时更是连交付资格都没有。合并后必须按合并的决策重算一次。
        # 业务性降级（待核、输入质量低）不推翻——那是业务判断，不是这一层能改的。
        previous_route = merged["route"] or {}
        if previous_route.get("action") in {"handoff", "stop", "rewrite"}:
            recomputed = self._route_candidates(merged["decisions"], 0)
            if recomputed.get("action") == "handoff" or previous_route.get("action") != "handoff":
                merged["route"] = recomputed
                state["thesis_regeneration"]["route_recovered_from"] = previous_route.get("reason") or previous_route.get("action")
        state["thesis_regeneration"].update({"status": "ok", "review_thesis_id": contract2.get("thesis_id"),
                                             "review_candidate_ids": [c.get("candidate_id") for c in regenerated["candidates"]],
                                             "review_highlights": [h.highlight_id for h in highlights2]})
        self.storage.event(task_id, "Q01", "thesis_regenerated_on_review", state["thesis_regeneration"])
        return merged, state["thesis_regeneration"]

    def _cached_reconcile(self, task_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
        """一次任务只问判官一次：重出与降级两条路都要用它，重复问会拿到两个答案。"""
        cached = state.get("thesis_reconciliation")
        if isinstance(cached, dict) and cached.get("relation"):
            return cached
        result = self._reconcile_contracts(task_id, state)
        if result:
            state["thesis_reconciliation"] = result
        return result

    def _downgrade_on_contract_disagreement(self, task_id: str, state: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        """两套独立主旨契约讲的是两件事时，摘掉推荐，但不改候选、也不替谁翻案。

        真实故障（2026-11-01，BV1PwsR6oEKW）：N07 读「女生夜里送吃的、叮嘱他休息」，
        R00 独立读出「女生睡过头、男生体贴不追究」。两套契约都传给了 R01，提示词也写明
        「不一致就标 needs_review」，但 R01 只拿生成侧契约核对标题（理由是「与视频 ASR
        呈现的关心互动一致」），程序侧又没有任何跨契约比对——于是主体被读反的那条候选
        顶着「系统推荐（已交付）」交到了人工面前。

        这里只摘推荐章：候选照旧全列给人评。理由是不做「按某一侧重写」——当两侧站在
        同样硬的地基上（或都只建在 model_derived_uncalibrated 上）时，按谁重写都只是
        把一次猜测换成另一次猜测，而这条视频恰好两边都没读对。
        """
        if not bool(self.config.extra.get("downgrade_on_contract_disagreement", True)):
            return route
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in ((state.get("video") or {}).get("evidence") or [])
            if isinstance(item, dict) and item.get("evidence_id")
        }
        verdict = contracts_disagree(state.get("thesis_contract") or {}, state.get("review_thesis_contract") or {}, evidence_by_id)
        declared = [d for d in (state.get("thesis_consistency_history") or []) if isinstance(d, dict)]
        contradiction = next((d for d in declared if d.get("relation") == "contradiction"), None)
        if not verdict and not contradiction:
            # 词法说一致、R01 也没提异议：不花这次调用。
            return route
        # 词法只当筛子（省钱），判官是这一次语义比对：词法触发里 86% 是误报，直接采信会把
        # 同义详略误判成互斥；R01 自己在 canary 里漏报过一次，也不单独作数。
        reconcile = self._cached_reconcile(task_id, state)
        relation = (reconcile or {}).get("relation") or ""
        if relation in {"aligned", "detail"}:
            # 语义判官推翻告警：不降级，但留事件备查，便于事后核这次推翻对不对。
            state["contract_reconciliation"] = {"relation": relation, "reason": (reconcile or {}).get("reason", ""),
                                                "lexical_overlap": (verdict or {}).get("semantics_overlap"),
                                                "outcome": "no_downgrade"}
            self.storage.event(task_id, "Q01", "thesis_contract_reconciled", state["contract_reconciliation"])
            return route
        if reconcile is None:
            self.storage.event(task_id, "Q01", "thesis_reconcile_fallback",
                               {"reason": "语义比对不可用，按保守口径降级", "lexical_overlap": (verdict or {}).get("semantics_overlap")})
        verdict = dict(verdict or {
            "semantics_overlap": None, "promise_similarity": None,
            "generated_promise": str((state.get("thesis_contract") or {}).get("core_promise") or ""),
            "review_promise": str((state.get("review_thesis_contract") or {}).get("core_promise") or ""),
            "generated_evidence": {}, "review_evidence": {}, "better_grounded": None,
        })
        verdict["reconcile_relation"] = relation or None
        verdict["reconcile_reason"] = (reconcile or {}).get("reason", "")
        verdict["reconcile_point"] = (reconcile or {}).get("contradiction_point", "")
        verdict["reconcile_missing"] = (reconcile or {}).get("review_missing", [])
        verdict["reconcile_missing_generated"] = (reconcile or {}).get("generated_missing", [])
        verdict["declared_relation"] = (declared[-1].get("relation") if declared else None)
        verdict["declared_contradiction_point"] = (contradiction or {}).get("contradiction_point", "")
        verdict["triggered_by"] = "both" if (verdict.get("semantics_overlap") is not None and contradiction) else ("review" if contradiction else "lexical")
        verdict["relation"] = relation or "unavailable"
        regeneration = state.get("thesis_regeneration") or {}
        if regeneration.get("status") == "ok":
            # 已按复核侧重出：分歧照样摊在台面上，但不摘推荐章——两组候选一并交付人工对照。
            verdict["resolution"] = "regenerated_on_review"
            verdict["review_candidate_ids"] = regeneration.get("review_candidate_ids") or []
            state["contract_disagreement"] = verdict
            self.storage.event(task_id, "Q01", "thesis_contract_disagreement_disclosed", verdict)
            return route
        state["contract_disagreement"] = verdict
        self.storage.event(task_id, "Q01", "thesis_contract_disagreement", verdict)
        if route.get("action") != "handoff":
            # 本来就没在推荐（待核/重写/拒绝），已记录即达到目的；不改写既有 reason，
            # 否则会丢掉「这条本来因为别的原因没过」这个信息。
            return route
        # 降级而不是清空：eligible_candidate_ids 原本装着这一轮所有可交付候选，
        # 直接抹掉等于让整组候选从交付面消失，人工连「要看哪条」都不知道
        # （R08 三条 ready 候选就是这样没进交付面的）。
        # 转 pending_review：仍交给人工，但不自动放行。
        return {**route, "action": "needs_review", "recommended_candidate_id": None,
                "eligible_candidate_ids": [],
                "pending_review_candidate_ids": list(route.get("eligible_candidate_ids") or []),
                "reason": "THESIS_CONTRACT_DISAGREEMENT",
                "contract_disagreement": verdict}

    def _guard_handoff(self, route: dict[str, Any], state: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if route.get("action") != "handoff":
            return route
        reason = None
        if state.get("evidence_contract_version") != "evidence-v1":
            reason = "EVIDENCE_CONTRACT_VERSION_MISSING"
        elif any(str(i.get("code") or "") not in LOCATOR_ISSUE_CODES for i in state.get("reference_audit", {}).get("issues", [])):
            reason = "REFERENCE_AUDIT_INVALID"
        elif state.get("risk_signals", {}).get("pending_signal_ids") and not _nature_disclosure_applied(state):
            reason = "CONTENT_NATURE_SIGNAL_UNRESOLVED"
        elif state.get("thesis_contract_errors") or state.get("thesis_alignment_errors") or state.get("review_thesis_contract_errors"):
            reason = "THESIS_CONTRACT_INVALID"
        elif state.get("input_quality", {}).get("level") == "low" and bool(self.config.extra.get("require_human_on_low_quality_input", True)):
            reason = "INPUT_LOW_CONFIDENCE"
        elif any(set(c.get("claim_ids", [])) & set(state.get("source_only_claim_ids", [])) for c in candidates if c.get("candidate_id") in route.get("eligible_candidate_ids", [])):
            reason = "SOURCE_TITLE_ONLY_UNVERIFIED"
        elif state.get("main_highlight_relation") != "core":
            reason = "MAIN_HIGHLIGHT_NOT_CORE"
        elif any(u.get("semantic_use", "unknown") in {"unknown", "unsupported_inference"} or u.get("grounding", "unknown") in {"unknown", "speculative"} for u in state.get("understanding", {}).get("semantic_units", []) if u.get("unit_id") in state.get("thesis_contract", {}).get("core_unit_ids", [])):
            reason = "CORE_EVIDENCE_CLASSIFICATION_UNRESOLVED"
        elif not state.get("thesis_contract", {}).get("thesis_id") or any(
            c.get("thesis_id") != state["thesis_contract"]["thesis_id"]
            for c in candidates if c.get("candidate_id") in route.get("eligible_candidate_ids", [])
        ):
            reason = "CANDIDATE_THESIS_MISMATCH"
        if reason:
            return {**route, "action": "needs_review", "reason": reason, "recommended_candidate_id": None, "eligible_candidate_ids": [], "pending_signal_ids": state.get("risk_signals", {}).get("pending_signal_ids", [])}
        return route

    def _repair_mechanical_pass(self, task_id: str, state: dict[str, Any], video: Any, understanding: Understanding, boundary: BoundarySet, highlights: list[Highlight], candidates: dict[str, Any], round_no: int) -> dict[str, Any]:
        """先修可安全压缩的候选；每个后代沿父链共享最多两轮。"""
        limit = min(2, max(0, int(getattr(self.config, "max_rewrite_rounds", 2))))
        if not getattr(self.config, "max_mechanical_repairs", 0) or not limit:
            return candidates
        reviews = {r.get("candidate_id"): r for r in candidates.get("reviews", [])}
        targets = []
        for decision in candidates.get("decisions", []):
            source = next((c for c in candidates.get("candidates", []) if c.get("candidate_id") == decision.get("candidate_id")), None)
            if source is None:
                continue
            defects = (_compressible_defects(source, decision, reviews.get(decision.get("candidate_id")))
                       or _pending_length_semantic_defects(source, decision))
            if defects:
                targets.append((source, defects))
        if not targets:
            return candidates
        state["mechanical_repair_targets"] = [c["candidate_id"] for c, _ in targets]
        repaired: list[str] = []
        original_route = candidates["route"]
        queue = sorted(targets, key=lambda item: not any(e.get("code") == "G4_LENGTH_EXCEEDED" for e in item[1]))
        attempted = {e.get("parent") for e in state.get("mechanical_repair_attempts", [])}
        while queue:
            source, defects = queue.pop(0)
            if source.get("candidate_id") in attempted:
                continue
            depth = _rewrite_depth(source, candidates["candidates"])
            if depth >= limit:
                continue
            parent_decision = next(d for d in candidates["decisions"] if d.get("candidate_id") == source.get("candidate_id"))
            attempt = depth + 1
            state.setdefault("mechanical_repair_attempts", []).append({"parent": source["candidate_id"], "round": attempt})
            attempted.add(source["candidate_id"])
            state["reviews"] = candidates.get("reviews", [])
            rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, -attempt, source, repair_mode="压缩", known_candidates=candidates["candidates"])
            # 去重没有产生后代时，消费一次预算并仅允许同一来源再试一轮。
            if (not rewritten.get("candidates") and
                    (rewritten.get("route") or {}).get("reason") == "REWRITE_NO_NEW_TITLE" and
                    not (rewritten.get("route") or {}).get("pending_signal_ids") and attempt < limit):
                self.storage.event(task_id, "N09", "mechanical_repair", {
                    "parent": source["candidate_id"], "attempt": attempt, "mode": "压缩",
                    "defects": [e.get("code") for e in defects], "parent_title": source.get("rendered_title"),
                    "repaired_titles": [], "repaired_ids": [], "reason": "REWRITE_NO_NEW_TITLE",
                })
                attempt += 1
                state["mechanical_repair_attempts"].append({"parent": source["candidate_id"], "round": attempt})
                rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, -attempt, source, repair_mode="压缩", known_candidates=candidates["candidates"])
            for child in rewritten.get("candidates", []):
                child["rewrite_budget_depth"] = max(attempt, child.get("rewrite_budget_depth", 0))
            # 原本待核的事实或政策不能因为改写后的复评通过而消失。
            # 但「限定语义待核」是按旧表达判的：重写后用另一种方式把限定说清了，
            # 子候选 G1 已经 pass，再压回待核就是把父的问题复制给已经解决的标题
            # （实测白菜 cand-2-r-1-1：G1/G2/G3/G4 全 pass、质量分 8、issues 为空，
            # 仍被标 needs_review，人工判为可用）。事实类待核仍照旧继承。
            parent_g1 = (parent_decision.get("gates") or {}).get("G1") or {}
            parent_limit_only = bool(parent_g1.get("limit_pending"))
            if parent_decision.get("status") == "needs_review":
                for decision in rewritten.get("decisions", []):
                    if decision.get("status") == "reject":
                        continue
                    child_g1 = str(((decision.get("gates") or {}).get("G1") or {}).get("status") or "")
                    if parent_limit_only and child_g1 == "pass":
                        decision["parent_review_carried"] = False
                        continue
                    decision["status"] = "needs_review"
                    decision["reason"] = parent_decision.get("reason", "PARENT_REQUIRES_REVIEW")
                    decision["repairable"] = False
            candidates["candidates"] += rewritten.get("candidates", [])
            candidates["reviews"] += rewritten.get("reviews", [])
            candidates["decisions"] += rewritten.get("decisions", [])
            repaired += [c.get("candidate_id") for c in rewritten.get("candidates", [])]
            self.storage.event(task_id, "N09", "mechanical_repair", {
                "parent": source["candidate_id"], "attempt": attempt, "mode": "压缩",
                "defects": [e.get("code") for e in defects], "parent_title": source.get("rendered_title"),
                "repaired_titles": [c.get("rendered_title") for c in rewritten.get("candidates", [])],
                "repaired_ids": [c.get("candidate_id") for c in rewritten.get("candidates", [])],
            })
            child_reviews = {r.get("candidate_id"): r for r in rewritten.get("reviews", [])}
            for child in rewritten.get("candidates", []):
                decision = next((d for d in rewritten.get("decisions", []) if d.get("candidate_id") == child.get("candidate_id")), {})
                remaining = (_compressible_defects(child, decision, child_reviews.get(child.get("candidate_id")))
                             or _pending_length_semantic_defects(child, decision))
                if remaining:
                    queue.append((child, remaining))
            subroute = rewritten.get("route") or {}
            if (subroute.get("action") == "reject" or subroute.get("pending_signal_ids") or
                    (subroute.get("action") == "needs_review" and subroute.get("reason") != "core_fact_or_business_policy_requires_human_verification")):
                pending = sorted(set((original_route.get("pending_signal_ids") or []) + (subroute.get("pending_signal_ids") or [])))
                reasons = [str(r) for r in (original_route.get("reason"), subroute.get("reason")) if r]
                original_route = {**subroute, "action": "needs_review", "recommended_candidate_id": None,
                                  "eligible_candidate_ids": [], "pending_signal_ids": pending,
                                  "reason": "；".join(dict.fromkeys(reasons))}
        if original_route.get("action") in {"needs_review", "reject"} or original_route.get("pending_signal_ids"):
            candidates["route"] = original_route
        else:
            attempted = {e["parent"] for e in state.get("mechanical_repair_attempts", [])}
            routable = [d for d in candidates["decisions"] if d.get("status") != "rewrite" or (
                d.get("candidate_id") not in attempted and
                _rewrite_depth(next(c for c in candidates["candidates"] if c.get("candidate_id") == d.get("candidate_id")), candidates["candidates"]) < limit)]
            candidates["route"] = self._route_candidates(routable, state.get("rewrite_round", 0))
        self.storage.event(task_id, "Q01", "quality_route_after_mechanical_repair", {
            "action": candidates["route"].get("action"), "reason": candidates["route"].get("reason"),
            "targets": state["mechanical_repair_targets"], "repaired": repaired,
        })
        return candidates

    def _route_candidates(self, decisions: list[dict[str, Any]], rewrite_round: int) -> dict[str, Any]:
        ranked = sorted((d for d in decisions if d["status"] == "ready_for_human_review"), key=lambda d: d.get("quality_sum") or 0, reverse=True)
        rewrites = [d for d in decisions if d["status"] == "rewrite"]
        uncertain = [d for d in decisions if d["status"] == "needs_review"]
        rejected = [d for d in decisions if d["status"] == "reject"]
        if ranked:
            # needs_review 的语义是「这条需要人工核实」，核实当然要看得见它。
            # 只返回 ranked 会把同任务里 needs_review 的候选整组丢掉
            # （R16 的 cand-1 被判「需核实」后没进交付面，人工判它其实可用）。
            return {"action": "handoff", "recommended_candidate_id": ranked[0]["candidate_id"], "eligible_candidate_ids": [d["candidate_id"] for d in ranked], "pending_review_candidate_ids": [d["candidate_id"] for d in uncertain], "rewrite_candidate_id": None, "reason": "eligible_candidates_ranked_by_Q1_Q2_Q3", "rewrite_round": rewrite_round}
        if uncertain:
            return {"action": "needs_review", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "core_fact_or_business_policy_requires_human_verification", "candidate_ids": [d["candidate_id"] for d in uncertain], "rewrite_round": rewrite_round}
        if rewrites:
            ranked = sorted(rewrites, key=lambda d: (d.get("quality_sum") or 0, -len(d.get("issues", []))), reverse=True)
            limit = max(1, int(getattr(self.config, "max_rewrite_candidates", 3)))
            targets = []
            for item in ranked[:limit]:
                issues = item.get("issues", []) or []
                mode = "看点" if any(i.get("issue_code") == "G3_CORE_POINT_OMITTED" or i.get("rollback_to") == "N07" for i in issues) else "表达"
                targets.append({"candidate_id": item["candidate_id"], "mode": mode})
            return {"action": "rewrite", "recommended_candidate_id": None, "eligible_candidate_ids": [],
                    "rewrite_candidate_id": targets[0]["candidate_id"], "rewrite_targets": targets,
                    "reason": "repairable_gate_or_quality_issues", "rewrite_round": rewrite_round}
        return {"action": "reject" if rejected else "stop", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "no_eligible_or_repairable_candidate", "rewrite_round": rewrite_round}

    def _compress_request_payload(self, *, video: Any, understanding: Understanding, boundary: BoundarySet,
                                  source: dict[str, Any], parent_review: dict[str, Any], required: list[str],
                                  budget: int, identity: dict[str, Any], rewrite_round: int) -> dict[str, Any]:
        """超长修复的小载荷：只回答「这句话该删哪几个字」。

        压缩原先走完整重写载荷（42–63K 字符：video 全量 + claims + boundary + 规则），
        但这一步的边际信息只有「父标题、预算、哪些字不能删」。全库 549 条候选里
        G4_LENGTH_EXCEEDED 出现 103 次（19%），每次都要为「少两个字」重付一遍全部
        材料。评审仍然读全量材料，所以压缩改义会在下游被抓出来。
        """
        claim_by_id = {claim.claim_id: claim for claim in understanding.claims}
        decision_by_id = {decision.claim_id: decision for decision in boundary.decisions}
        required_claims = []
        for claim_id in source.get("claim_ids") or []:
            claim = claim_by_id.get(str(claim_id))
            if claim is None:
                continue
            item: dict[str, Any] = {"claim_id": claim.claim_id, "text": claim.text}
            decision = decision_by_id.get(str(claim_id))
            if decision is not None:
                if decision.required_source_phrase:
                    item["required_source_phrase"] = decision.required_source_phrase
                if decision.attribution_required:
                    item["attribution_required"] = True
                if decision.required_limits:
                    item["required_limits"] = list(decision.required_limits)
            required_claims.append(item)
        head_body = str(source.get("headline") or "")
        rendered = str(source.get("rendered_title") or render_title(source))
        # REWRITE_PROMPT 的压缩段落直接引用 length_repair（模型自己估字数系统性偏低，
        # 必须把「超了几个字、预算多少」算好给它）；小载荷换掉了整份 user，
        # 这个字段不带上就等于让模型盲压。
        length_repair = {
            "over_by": max(0, len(rendered) - self.config.max_chars),
            "current_body_chars": len(head_body),
            "max_chars": self.config.max_chars,
            "max_chars_body": budget,
            "parent_rendered_length": len(rendered),
            "defects": [{"code": e.get("code"), "reason": e.get("reason")} for e in ((source.get("mechanical_checks") or {}).get("errors") or []) if isinstance(e, dict)],
            "instruction": "优先满足长度；不得虚构或丢失必要归属和限定。未决事实仍需核实，不得视为已确认。",
        }
        payload: dict[str, Any] = {
            "task": "compress_only",
            "video": {"title": video.title, "author": video.author, "format": video.format, "domain": video.domain},
            "parent_candidate": {
                "candidate_id": source.get("candidate_id"),
                "rendered_title": str(source.get("rendered_title") or render_title(source)),
                "headline": source.get("headline"),
                "prefix_labels": list(source.get("prefix_labels") or []),
                "slot_id": source.get("slot_id"), "thesis_id": source.get("thesis_id"),
                "highlight_id": source.get("highlight_id"),
            },
            "defects": [{"code": e.get("code"), "reason": e.get("reason")} for e in ((source.get("mechanical_checks") or {}).get("errors") or []) if isinstance(e, dict)],
            "length_repair": length_repair,
            "review_issues": [{"issue_code": i.get("issue_code"), "action": i.get("action")} for i in (parent_review.get("issues") or []) if isinstance(i, dict)],
            "required_claims": required_claims,
            "display_config": {"max_chars": self.config.max_chars, "max_chars_body": budget, "prefix_labels": required,
                               "prefix_rendered": render_labels(required), "identity": identity,
                               "counting": "前缀与正文合计不超过 max_chars"},
            "rewrite_mode": "压缩",
            "target_count": 1,
            "rewrite_round": rewrite_round,
            "instruction": ("只做文字压缩：把 parent_candidate 压到 display_config.max_chars_body 以内，"
                            "不得改事实、归属、限定、否定或范围。只返回 1 条 candidates，每条只写 headline 与 change_reason；"
                            "claim_ids/evidence_ids/highlight_id/slot_id/thesis_id/prefix_labels 由系统继承父候选，"
                            "不要输出也不要改。"),
        }
        attack_hits = find_personal_attacks(rendered or head_body)
        if attack_hits:
            # 与完整重写载荷同一契约：P9 命中词必须随请求带走，压字数不能把攻击词原样保留。
            payload["forbidden_terms"] = {"rule_id": "P9", "terms": attack_hits, "instruction": attack_rewrite_hint(attack_hits)}
        return payload

    def _generate_and_review(self, task_id: str, state: dict[str, Any], video: Any, understanding: Understanding, boundary: BoundarySet, highlights: list[Highlight], rewrite_round: int, source: dict[str, Any] | None = None, repair_mode: str | None = None, skip_thesis_review: bool = False, thesis_basis: str | None = None, known_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        signal_report = extract_risk_signals(video)
        state["risk_signals"] = signal_report
        required = normalize_prefix_labels([], required_prefixes(format=video.format, attributes=understanding.attributes, domain=video.domain, evidence=video.evidence) + signal_report["required_prefixes"])
        budget = body_budget(self.config.max_chars, required)
        state["required_prefixes"] = required
        state["body_budget"] = budget
        identity = route_identity(
            author=video.author,
            creator_known=None,
            delivery_type=(state.get("thesis_contract") or {}).get("delivery_type", ""),
            domain=video.domain,
            category=str(((video.provenance or {}).get("metadata") or {}).get("category") or ""),
            public_figures=self.config.extra.get("public_figures"),
        )
        state["identity_route"] = identity
        # 暂缓核验的陈述不产生「来源/限定」要求：它本来就不该被写进标题
        source_requirements = [to_dict(decision) for decision in boundary.decisions if decision.decision != "excluded" and not decision.verification_required and (decision.attribution_required or decision.required_source_phrase or decision.required_limits or decision.conditional_constraints or decision.forbidden_inferences)]
        blocked_claims = [
            {"claim_id": decision.claim_id, "forbidden_inferences": decision.forbidden_inferences,
             "reason": "暂缓核验" if decision.verification_required else "已排除", "unresolved": decision.verification_required}
            for decision in boundary.decisions
            if decision.decision == "excluded" or decision.verification_required
        ]
        user = {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "unit_decisions": to_dict(boundary.unit_decisions), "boundary": to_dict(boundary), "source_requirements": source_requirements, "blocked_claims": blocked_claims, "unresolved_issues": boundary.unresolved_issues, "highlights": to_dict(highlights), "thesis_contract": state.get("thesis_contract", {}), "matched_rule_ids": (state.get("rules") or {}).get("rule_ids", []), "input_quality": state.get("input_quality", {}), "source_evidence_usage": state.get("source_evidence_usage", {}), "display_config": {"max_chars": self.config.max_chars, "max_chars_body": budget, "prefix_labels": required, "prefix_rendered": render_labels(required), "identity": identity, "counting": "前缀与正文合计不超过 max_chars", "ending_question_mark_forbidden": True}, "strategy_enum": rubric().get("title_policy", {}).get("strategy_enum", []), "strategy_triggers": rubric().get("title_policy", {}).get("strategy_triggers", {}), "target_count": self.config.rewrite_candidate_count if source else self.config.target_count, "rewrite_round": rewrite_round}
        user["risk_signals"] = signal_report
        if thesis_basis:
            # 两套解读分歧后按其中一侧重出：契约里的承诺/必保语义换成该侧原文，
            # 看点池仍是这一侧重新选过的，不做跨侧拼接。
            user["thesis_basis"] = thesis_basis
            basis_contract = (state.get("review_thesis_contract") if thesis_basis == "review" else state.get("thesis_contract")) or {}
            user["delivery_basis"] = {
                "basis": thesis_basis,
                "label": "复核侧独立读法" if thesis_basis == "review" else "生成侧读法",
                "core_promise": basis_contract.get("core_promise", ""),
                "required_semantics": basis_contract.get("required_semantics", []),
                "evidence_ids": basis_contract.get("evidence_ids", []),
                "instruction": "以 delivery_basis 的承诺与必保语义作为本次交付基准；thesis_contract 只用于看点绑定与机械校验。不得把两套读法混写成一条标题。",
            }
        parent_review = next((r for r in state.get("reviews", []) if r.get("candidate_id") == (source or {}).get("candidate_id")), {})
        rewrite_mode = "表达"
        if source:
            parent_issues = parent_review.get("issues", []) or []
            rewrite_mode = repair_mode or ("看点" if any(i.get("issue_code") == "G3_CORE_POINT_OMITTED" or i.get("rollback_to") == "N07" for i in parent_issues) else "表达")
            user.update({
                "parent_candidate_id": source["candidate_id"], "parent_headline": source.get("headline"), "parent_slot_id": source.get("slot_id"), "parent_thesis_id": source.get("thesis_id"),
                "parent_highlight_id": source.get("highlight_id"), "review_issues": parent_issues,
                "rewrite_guidance": parent_review.get("rewrite_guidance", {}),
                "rewrite_mode": rewrite_mode, "allow_highlight_change": rewrite_mode == "看点",
            })
            # 重写携带同任务历史失败标题及理由，防止换皮重复（BV1Bgxw6eEMn 12条全拒教训）
            failed_history = []
            for prev in state.get("reviews", []):
                prev_cid = str(prev.get("candidate_id") or "")
                if prev_cid and prev_cid != source["candidate_id"]:
                    prev_issues = [i.get("issue_code") for i in (prev.get("issues") or []) if isinstance(i, dict)]
                    if prev_issues:
                        prev_cand = next((c for c in (known_candidates or []) if c.get("candidate_id") == prev_cid), None)
                        if prev_cand:
                            failed_history.append({"title": prev_cand.get("rendered_title") or prev_cand.get("headline"), "issues": prev_issues})
            if failed_history:
                user["failed_sibling_titles"] = failed_history[:5]
            attack_hits = find_personal_attacks(str(source.get("rendered_title") or source.get("headline") or ""))
            if attack_hits:
                user["forbidden_terms"] = {"rule_id": "P9", "terms": attack_hits, "instruction": attack_rewrite_hint(attack_hits)}
            if rewrite_mode == "压缩":
                # 只给「超了多少、预算是多少」，不让模型自己估：它对中文字数的估计系统性偏低。
                head_body = str(source.get("headline") or "")
                user["length_repair"] = {
                    "over_by": max(0, len(str(source.get("rendered_title") or "")) - self.config.max_chars),
                    "current_body_chars": len(head_body),
                    "max_chars": self.config.max_chars,
                    "max_chars_body": budget,
                    "parent_rendered_length": len(str(source.get("rendered_title") or "")),
                    "defects": [{"code": e.get("code"), "reason": e.get("reason")} for e in ((source.get("mechanical_checks") or {}).get("errors") or []) if isinstance(e, dict)],
                    "instruction": "优先满足长度，同时根据 review_issues 修复语义缺陷；不得虚构或丢失必要归属和限定。未决事实仍需核实，不得视为已确认。",
                }
        # 纯文字压缩不需要全量材料：切到小载荷，并在事件里留下体积对比，
        # 这样"压缩到底省了多少"是查得到的，不靠事后推断。
        if source and rewrite_mode == "压缩":
            full_payload_chars = len(json.dumps(user, ensure_ascii=False))
            user = self._compress_request_payload(
                video=video, understanding=understanding, boundary=boundary, source=source,
                parent_review=parent_review, required=required, budget=budget,
                identity=identity, rewrite_round=rewrite_round,
            )
            self.storage.event(task_id, "N09", "compress_small_request", {
                "parent": source.get("candidate_id"),
                "full_payload_chars": full_payload_chars,
                "compress_payload_chars": len(json.dumps(user, ensure_ascii=False)),
            })
        # 仅显式定向重写可携带已校验的人评；放在压缩载荷重建之后，避免被丢弃。
        human_feedback = state.get("targeted_rewrite_feedback")
        if source and isinstance(human_feedback, dict):
            title = str(source.get("rendered_title") or source.get("headline") or "")
            if (human_feedback.get("task_id") != task_id
                    or human_feedback.get("candidate_id") != source.get("candidate_id")
                    or human_feedback.get("title_hash") != hashlib.sha256(title.encode("utf-8")).hexdigest()):
                raise ValueError("人工反馈与重写父候选身份不一致")
            user["human_rewrite_feedback"] = human_feedback
        memory_calls = int(state.get("memory_calls", 0))
        if rewrite_mode != "压缩" and memory_calls < self.config.memory_call_limit:
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
            same_video = self.memory.search_for_video(video.video_id, self.config.prompt_pack_version)
            if same_video:
                user["same_video_cases"] = same_video
                state.setdefault("memory_trace", []).append({"query": video.video_id, "status": "same_video", "used_case_ids": [c.get("case_id") for c in same_video]})
        generation_task = "rewrite" if source else "generate"
        draft = self._call(task_id, "N09", generation_task, user, "AgentDraft", rules=state.get("rules"))
        if source and rewrite_mode == "压缩":
            draft = _fill_compress_draft(draft, source)
        state["prompt_hashes"][generation_task] = self._last_prompt_hash(task_id, "N09", generation_task)
        candidates = _normalize_candidates(draft, rewrite_round, source["candidate_id"] if source else None, required_prefixes=required)
        if thesis_basis:
            for candidate in candidates:
                candidate["thesis_basis"] = thesis_basis
        candidates = _dedupe_similar_candidates(candidates)
        if source:
            _enforce_rewrite_thesis(candidates, source, state.get("thesis_contract", {}))
            _enforce_rewrite_slot(candidates, source)
            for index, candidate in enumerate(candidates, 1):
                if candidate.get("parent_candidate_id") != source.get("candidate_id"):
                    candidate.setdefault("generation_schema_errors", []).append("rewrite changed parent_candidate_id")
                if rewrite_mode == "表达" and candidate.get("highlight_id") != source.get("highlight_id"):
                    candidate.setdefault("generation_schema_errors", []).append("expression rewrite changed highlight_id")
                candidate["candidate_id"] = f"{source['candidate_id']}-r{rewrite_round}-{index}"
            # 重写最常见的失败不是"改坏了"，是"根本没改"：标题与已有候选逐字相同。
            # 重复标题对人工没有新信息，对评审只是放大调用量，对下一轮重写是重新掷同一个骰子。
            # 比较池必须包含"本轮之前已经改写出来的候选"，不能只看 state["candidates"]——
            # 那份快照在整个重写循环里都是首轮那几条（循环结束才回写），只看它会让
            # 第 2 轮与第 1 轮的逐字重复逃过判重（真实故障 2026-11-02，R09 白菜：
            # 第 2 轮三个标题与第 1 轮一字不差，rewrite_duplicates 却是空的）。
            pool = list(known_candidates or []) + list(state.get("candidates", []))
            existing = {_title_key(c.get("rendered_title") or c.get("headline")) for c in pool}
            kept = [c for c in candidates if _title_key(c.get("rendered_title") or c.get("headline")) not in existing]
            dropped_duplicates = [c.get("candidate_id") for c in candidates if c not in kept]
            if dropped_duplicates:
                state.setdefault("rewrite_duplicates", []).append({
                    "parent": source.get("candidate_id"), "round": rewrite_round,
                    "dropped": dropped_duplicates,
                    "titles": sorted({str(c.get("headline") or "") for c in candidates if c not in kept}),
                })
                self.storage.event(task_id, "N09", "rewrite_duplicates_dropped", state["rewrite_duplicates"][-1])
            candidates = kept
            if not candidates:
                # 一轮重写没产出任何新标题：再烧一轮也是同一个骰子，直接停。
                route = {"action": "stop", "recommended_candidate_id": None, "eligible_candidate_ids": [], "rewrite_candidate_id": None, "reason": "REWRITE_NO_NEW_TITLE", "rewrite_round": rewrite_round}
                self.storage.event(task_id, "Q01", "quality_route", route)
                return {"candidates": [], "reviews": [], "decisions": [], "route": route}
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
        allowed_claims = {d.claim_id for d in boundary.decisions if d.decision != "excluded" and not d.verification_required}; allowed_highlights = {h.highlight_id for h in highlights}
        # 允许集合统一走 _allowed_unit_ids：只有被明确判 excluded 的单元不可写，边界「没提到」
        # 的单元是没表态、不是不在范围（真实故障：边界只给 u6 excluded 时曾把其余单元算成
        # 空集，所有候选的 unit_id=u1 全被判 G4_INVALID_REFERENCE，整条视频零交付）。
        # 结构片段（turn-N/axis-N）与语义单元（su-N）命名不同，但只要它被抽帧或边界点到，
        # 就属于本任务范围，不能因为名字不在 semantic_units 里就判候选越界。
        allowed_units = _allowed_unit_ids(understanding, boundary, highlights)
        boundary_by_claim = {d.claim_id: d for d in boundary.decisions}
        # 候选未自带 unit_id 时，从其 highlight 反查所属语义单元补齐（仅用于定位/归因，不参与事实校验）
        unit_by_highlight = {h.highlight_id: h.unit_id for h in highlights if h.unit_id}
        for c in candidates:
            if not c.get("unit_id"):
                c["unit_id"] = unit_by_highlight.get(c.get("highlight_id", ""), "")
        for c in candidates:
            c["mechanical_checks"] = _mechanical_check(c, allowed_claims=allowed_claims, allowed_highlights=allowed_highlights, evidence_ids={e.evidence_id for e in video.evidence}, max_chars=self.config.max_chars, allowed_units=allowed_units, required_prefixes=required, claim_decisions=boundary.decisions)
            c["rendered_title"] = c["mechanical_checks"]["rendered_title"]
            c["title_length"] = c["mechanical_checks"]["title_length"]
            signal_errors = check_signal_constraints(c.get("headline", ""), signal_report, understanding.claims, c.get("claim_ids", []))
            cited_evidence = {r for claim in understanding.claims if claim.claim_id in c.get("claim_ids", []) for r in claim.evidence_refs}
            c["signal_review_reasons"] = signal_review_reasons(c.get("headline", ""), signal_report, cited_evidence)
            numeric_support = [e.text for e in video.evidence if e.evidence_id in cited_evidence and e.field != "frame" and e.quality not in {"model_derived_uncalibrated", "unverified_source_metadata"}]
            if unsupported_numeric_facts(c.get("headline", ""), numeric_support):
                signal_errors.append({"code": "G4_NUMERIC_CONFLICT", "repairable": True, "reason": "标题数值、单位或指标没有对应引用证据支持（金额须带原单位，如「400亿美元」不能写成「400亿」）；不得用原题数字白名单替代核验"})
            if c.get("slot_id") == "core_hook":
                main_hl = next((h.highlight_id for h in highlights if getattr(h, "role", "") == "main"), None)
                if main_hl and not _core_hook_is_aligned(c, highlights, state.get("thesis_contract") or {}):
                    c["mechanical_checks"]["errors"].append({
                        "code": "G4_HOOK_OFF_MAINLINE", "repairable": True,
                        "reason": f"核心钩子必须绑定主看点 {main_hl}，不得换成其它片段（当前 {c.get('highlight_id')}）"})
            c["mechanical_checks"]["errors"].extend(signal_errors)
            # 同上：判否项一律让 status 跟随，避免「errors 有、status 还是 pass」。
            if c["mechanical_checks"]["errors"]:
                c["mechanical_checks"]["status"] = "fail"
            c["programmatic_g1_flags"] = []
            source_required = any(
                boundary_by_claim.get(claim_id)
                and boundary_by_claim[claim_id].attribution_required
                for claim_id in c.get("claim_ids", [])
            )
            # 边界要的归属若只是"旁白/剧情叙述"，前缀已声明叙述属性，不该再逼正文写载体。
            required_phrases = [
                boundary_by_claim[cid].required_source_phrase
                for cid in c.get("claim_ids", [])
                if boundary_by_claim.get(cid) and boundary_by_claim[cid].attribution_required
            ]
            carrier_only = bool(required_phrases) and all(_carrier_only_source(x) for x in required_phrases)
            if _declares_narration(c.get("prefix_labels")) and carrier_only:
                source_required = False
            if source_required and not _has_explicit_source_relation(c.get("headline", ""), required_phrases) \
                    and not _attribution_identity_present(c.get("headline", ""), required_phrases):
                c["programmatic_g1_flags"].append({"code": "G1_SOURCE_ATTRIBUTION_MISSING", "repairable": True, "reason": "本条边界明确要求来源归属，但标题未表达转述关系"})
        state["pending_review_candidates"] = candidates
        self.storage.save_candidates(task_id, candidates)
        self.storage.checkpoint(task_id, "reviewing", state, "N09", "candidates_generated", {"candidate_ids": [c.get("candidate_id") for c in candidates], "candidate_count": len(candidates), "generation_round": rewrite_round})
        self.storage.set_state(task_id, "reviewing", state)
        # 视频级独立主旨复核：只读原材料，不与生成主旨/候选串通。首轮一次，重写不再重复。
        if source is None and not skip_thesis_review:
            try:
                thesis_raw = self._call(task_id, "R00", "thesis_review", {"video": to_dict(video)}, "ThesisContract", self.review_adapter)
                review_thesis, _review_thesis_errors = normalize_thesis_contract(thesis_raw, understanding, boundary, independent=True)
                state["review_thesis_contract"] = review_thesis
                state["review_thesis_contract_errors"] = _review_thesis_errors
                self.storage.event(task_id, "R00", "thesis_review_complete", {"thesis_id": review_thesis.get("thesis_id"), "errors": _review_thesis_errors})
            except ModelError as exc:
                if not exc.retryable:
                    raise
                state["review_thesis_contract"] = {}
                state["review_thesis_contract_errors"] = [f"独立主旨复核失败: {exc}"]
                self.storage.event(task_id, "R00", "thesis_review_failed", {"error": str(exc)})
        checks = [{"candidate_id": c["candidate_id"], **c["mechanical_checks"], "programmatic_g1_flags": c["programmatic_g1_flags"]} for c in candidates]
        review_raw = self._review_with_fallback(task_id, video, state, candidates, checks)
        per_candidate = review_raw.get("thesis_consistency_per_candidate")
        if isinstance(per_candidate, list):
            declarations = [(item.get("candidate_id"), _normalize_thesis_consistency(item.get("conclusion")))
                            for item in per_candidate if isinstance(item, dict)]
        else:
            declarations = [(None, _normalize_thesis_consistency(review_raw))]
        for candidate_id, declared in declarations:
            if declared:
                declared["round"] = rewrite_round
                if candidate_id is not None:
                    declared["candidate_id"] = candidate_id
                state.setdefault("thesis_consistency_history", []).append(declared)
                state["thesis_consistency"] = declared
        state["prompt_hashes"]["review"] = self._last_prompt_hash(task_id, "R01", "review")
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in candidates])
        reviews, decisions = self._validate_and_decide_reviews(video, state, candidates, reviews, checks, task_id)
        route = self._route_candidates(decisions, rewrite_round)
        route = self._guard_handoff(route, state, candidates)
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
        # 待核和待重写候选即使与可推荐候选并存，也应进入人工视野；
        # 只补充待核清单，不改变推荐、交付或风险拒绝的判定。
        surface = (set(route.get("eligible_candidate_ids") or [])
                   | set(route.get("pending_review_candidate_ids") or [])
                   | set(route.get("candidate_ids") or []))
        statuses = {"needs_review", "rewrite"}
        if not surface:
            # 原有空清单兜底：分支遗漏候选时，已通过的候选也只作为待核交出。
            statuses.add("ready_for_human_review")
        pending = list(route.get("pending_review_candidate_ids") or [])
        added = False
        for decision in state.get("candidate_decisions") or []:
            candidate_id = decision.get("candidate_id")
            if candidate_id and decision.get("status") in statuses:
                candidate_id = str(candidate_id)
                if candidate_id not in surface:
                    pending.append(candidate_id)
                    surface.add(candidate_id)
                    added = True
        if added:
            route = {**route, "pending_review_candidate_ids": pending}
            state["route"] = route
        status = "pending_human" if route["action"] in {"handoff", "needs_review"} else "stopped"
        state["uncalibrated"] = True
        self.storage.checkpoint(task_id, status, state, "N12", "human_handoff", {"status": status, "route": route})
        self.storage.event(task_id, "N12", "handoff", {"status": status, "route": route})
        return task_id

    def _stop(self, task_id: str, state: dict[str, Any], reason: str) -> str:
        state["stop_reason"] = reason; self.storage.set_state(task_id, "stopped", state); self.storage.event(task_id, "X02", "business_stop", {"reason": reason}); return task_id

