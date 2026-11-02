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
from .labels import MIN_BODY_BUDGET, body_budget, extract_inline_prefixes, normalize_prefix_labels, render_labels, render_title, required_prefixes, self_declared_prefixes
from .memory import MemoryStore
from .signals import extract_risk_signals, check_signal_constraints, unsupported_numeric_facts, signal_review_reasons
from .model import ModelAdapter, ModelError
from .rules import load_common_rule_ids, load_rule_bundle, load_rules, rubric
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


def _apply_numeric_conflict_gate(video: Any, understanding: Any, boundary: Any) -> Any:
    """数字必须能落到所引用的原始文本，不恢复被其他守卫排除的陈述。"""
    evidence = {e.evidence_id: e for e in video.evidence}
    decisions = {d.claim_id: d for d in boundary.decisions}
    for claim in understanding.claims:
        refs = [evidence[r].text for r in claim.evidence_refs if r in evidence and evidence[r].quality not in {"model_derived_uncalibrated", "unverified_source_metadata"} and evidence[r].field != "frame"]
        unsupported = unsupported_numeric_facts(claim.text, refs)
        decision = decisions.get(claim.claim_id)
        if not unsupported or decision is None or decision.decision != "usable":
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
    for key in BOUNDARY_PENDING_KEYS:
        value = layer.get(key)
        if isinstance(value, list):
            return {str(x) for x in value if str(x)}
    return set()


def collect_boundary_groups(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """收集所有层级的「按决策分组」结果，作为逐条决策的覆盖层。

    按「最严者胜」合并重复 ID（一条 claim 同时出现在 usable 与 excluded 时取 excluded）。
    """
    merged: dict[str, dict[str, Any]] = {}
    for layer in _iter_boundary_layers(raw):
        for decision, aliases in BOUNDARY_GROUP_ALIASES:
            values = next((layer[a] for a in aliases if isinstance(layer.get(a), list)), None)
            if values is None:
                continue
            for cid in values:
                cid = str(cid)
                if not cid:
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
                if cid is None or str(cid) in ordered:
                    continue
                ordered[str(cid)] = item
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
    known_ids = {str(it.get("claim_id")) for it in items if isinstance(it, dict) and it.get("claim_id")}
    for cid, group in groups.items():
        if cid not in known_ids:
            items.append({"claim_id": cid, "_from_group": group})
    decisions = []
    missing_decision_ids: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        cid = str(item.get("claim_id") or f"c{i+1}")
        group = item.pop("_from_group", None) or groups.get(cid) or {}
        refs = item.get("evidence_refs") or item.get("evidence_ids") or []
        # 边界节点经常不回填 evidence_refs：引用关系在理解阶段已经建立，
        # 这里直接从对应 claim 继承，而不是事后判"引用列表为空"把整条任务废掉。
        if not refs and claim_evidence:
            refs = list(claim_evidence.get(cid) or [])
        decision = _boundary_item_decision(item) or _normalize_boundary_decision(group.get("decision"))
        if decision is None:
            # 模型对这条 claim 完全没表态。excluded 是保守兜底，但必须留痕：
            # 静默兜底会让「模型漏答」看起来像「材料真的不可用」，整任务白跑还没人知道。
            missing_decision_ids.append(cid)
            decision = "excluded"
        decisions.append(ClaimDecision(
            claim_id=cid, supported_statement=item.get("supported_statement") or item.get("statement") or item.get("text") or (claim_texts or {}).get(str(item.get("claim_id") or f"c{i+1}")),
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
            verification_required=bool(item.get("verification_required") or item.get("pending_verification") or group.get("verification_required")),
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
    unresolved = raw.get("unresolved_issues") if isinstance(raw.get("unresolved_issues"), list) else []
    if missing_decision_ids:
        unresolved = list(unresolved) + [{
            "code": BOUNDARY_DECISION_MISSING,
            "claim_ids": sorted(missing_decision_ids),
            "message": f"模型未对 {len(missing_decision_ids)} 条 claim 给出决策，已按 excluded 兜底并留痕。",
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
                        "受访", "讲述", "坦言", "回忆", "提到", "补充", "解释")


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


def _limit_needs_body(limit: Any, prefix_labels: Any) -> bool:
    """这条限定是否必须由标题正文表达。"""
    if _limit_is_prohibition(limit) or _limit_covered_by_narration(limit, prefix_labels):
        return False
    # 归属类永远要表达：它说的就是「把话归给谁」，标题不写就等于把博主说法当成了事实。
    if _limit_is_attribution(limit):
        return True
    return not _is_retention_only_limit(limit)


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


def _has_explicit_source_relation(headline: str) -> bool:
    """Reject generic source labels such as '通报：' without a relation word."""
    text = _strip_attribution_false_friends(str(headline).strip())
    if text.startswith(("通报：", "通报:", "通报称")):
        return False
    if "通报：" in text or "通报:" in text:
        prefix = text.split("通报", 1)[0]
        return any(word in prefix for word in ("据", "称", "显示", "指出", "发布"))
    if "通报" in text:
        return any(word in text for word in ("据", "称", "显示", "指出", "发布"))
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
_META_PHRASE_RE = re.compile(
    # 冒号式（「旁白：」「字幕：」）和「旁白讲」同样是载体元话语，早先只匹配「旁白称」这类带动词的说法，
    # 于是「旁白：狙击手装焦尸」既逃过元话语拦截、又被 G1 判成"没表达归属"，两头都错。
    r"(?:视频|画面|标题|封面|片子|原题|字幕|口播|旁白|文案)(?:里|中)?(?:称|说|讲|提到|显示|写着|只见|却是|透露|表示|[:：])"
    r"|据(?:视频|画面|标题|封面|原题|字幕|片子|旁白|口播)"
    r"|作者称"
)
# 校验结论：标题写「与…不符/不一致」是评审层的话，不该出现在成品标题。
_VERIFICATION_CONCLUSION_RE = re.compile(r"与[^，。！？]{0,12}(?:不符|不一致)")


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
    if len(spans) == 1 and len(core_ids) > len(spans):
        # 只有一个核心单元能定位到 ASR，其余连时间都定不了：它们不能和有证据的主线并列当核心。
        timed = {item[0] for item in spans}
        untimed = [uid for uid in core_ids if uid not in timed]
        demoted = [{"unit_id": uid, "start": None, "end": None,
                    "kind": str((units.get(uid).kind if units.get(uid) else "") or ""),
                    "reason": "无时间证据，不能与有证据的主线并列为核心"} for uid in untimed]
        contract["core_unit_ids"] = [uid for uid in core_ids if uid in timed]
        contract["demoted_core_units"] = demoted
        return demoted
    if len(spans) < 2:
        return []
    earliest = min(spans, key=lambda item: item[1])
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


def _mechanical_check(candidate: dict[str, Any], *, allowed_claims: set[str], allowed_highlights: set[str], evidence_ids: set[str], max_chars: int, allowed_units: set[str] | None = None, required_prefixes: list[str] | None = None, claim_decisions: list[Any] | None = None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
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
    missing_prefixes = [label for label in required if label not in prefix_labels]
    if missing_prefixes:
        errors.append({"code": "G4_PREFIX_MISSING", "repairable": False, "reason": f"缺少平台标注前缀：{'、'.join(missing_prefixes)}"})
    if body_budget(max_chars, required) < MIN_BODY_BUDGET:
        errors.append({"code": "G4_PREFIX_BUDGET_EXHAUSTED", "repairable": False, "reason": f"前缀占用 {len(render_labels(required))} 字，正文预算不足，需要业务决策而非改标题"})
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
    # 元信息表述：标题要陈述视频内容本身，不能写成「视频称/画面却是/标题说/据视频所述……」这类描述载体怎么说的框架
    meta_hit = _META_PHRASE_RE.search(head_body)
    if meta_hit:
        errors.append({"code": "G4_META_PHRASE", "repairable": True, "reason": f"标题含元信息表述「{meta_hit.group(0)}」，应直接陈述视频内容而非描述视频/画面怎么说"})
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
    # 来源前缀完整性：「据X」后须跟归属动词（报道/称/表示…），不能悬空成「据咪咕体育」
    attr = re.match(r"^据([\u4e00-\u9fff]{2,6})(?=\s|[，,])", head_body)
    if attr and not re.search(r"报道|称|表示|显示|透露|介绍|消息|通报|所述|说|力争", attr.group(1)):
        errors.append({"code": "G4_INCOMPLETE_ATTRIBUTION", "repairable": True, "reason": f"来源前缀「据{attr.group(1)}」缺少归属动词（报道/称/表示），应补全"})
    # 限定校验：引用了「limited 且带 required_limits」的陈述时，标题必须表达限定，
    # 否则会把仅见于标题/声称的信息写成确定事实（等评审抓太晚，这里直接拦在生成后）。
    if claim_decisions:
        cited = {str(x) for x in (candidate.get("claim_ids") or [])}
        limited_cited = [
            d for d in claim_decisions
            if str(getattr(d, "decision", "")) == "limited"
            and getattr(d, "required_limits", None)
            and str(getattr(d, "claim_id", "")) in cited
            # 「须归属为旁白/剧情叙述」是载体披露，【剧情演绎】前缀已经写明；
            # 再要求正文重复一次，就与 U-S09 禁止载体元话语直接冲突，形成必死的死锁。
            # 绑定了具体子事实的归属限定（「但病因须归属」）在标题没提该子事实时也不适用。
            and any(_limit_needs_body(x, prefix_labels) and not _limit_attribution_scope_absent(x, head_body)
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
                if _limit_needs_body(x, prefix_labels) and not _limit_attribution_scope_absent(x, head_body)
            ]
            needs_attribution = [x for x in limits_flat if _limit_is_attribution(x)]
            needs_other = [x for x in limits_flat if not _limit_is_attribution(x)]
            satisfied = ((not needs_attribution or _has_explicit_source_relation(head_body))
                         and (not needs_other or all(_limit_is_expressed(x, head_body) for x in needs_other)))
            if not satisfied:
                codes = "、".join(str(getattr(d, "claim_id", "")) for d in limited_cited)
                errors.append({"code": "G1_MISSING_LIMIT", "repairable": True, "reason": f"标题引用了需限定的陈述（{codes}），但未表达限定；须在实际正文保留必要范围、条件或可识别信源归属，不能仅自报 qualifiers_used；无法保留时删除该信息"})
            # 核查争议类限定单独校验：归属语（「某称」）不等于标明争议，
            # 否则「A 称 X」会被判通过，而用户仍把一个有反证的说法当成确定事实。
            disputed = [
                d for d in limited_cited
                if any(DISPUTE_LIMIT_MARK in str(limit) for limit in (getattr(d, "required_limits", None) or []))
            ]
            if disputed and not any(p in head_body for p in DISPUTE_MARK_PHRASES):
                codes = "、".join(str(getattr(d, "claim_id", "")) for d in disputed)
                errors.append({"code": "G1_MISSING_DISPUTE_MARK", "repairable": True, "reason": f"标题引用了存在外部反驳的陈述（{codes}），但未标明争议；须写「存在争议/未经证实/说法不一」等，或改用其他陈述"})
    warnings: list[dict[str, Any]] = []
    if candidate.get("self_declared_prefixes"):
        warnings.append({"code": "G4_PREFIX_SELF_DECLARED", "reason": f"模型自报非必填前缀：{'、'.join(candidate['self_declared_prefixes'])}（已保留，仅观测）"})
    if head_body.startswith(("【", "[")):
        warnings.append({"code": "G4_INLINE_PREFIX_IN_BODY", "reason": "正文仍以方括号开头，可能残留未剥离的标注"})
    strategy = str(candidate.get("expression_strategy") or "")
    allowed_strategies = set(rubric().get("title_policy", {}).get("strategy_enum", []))
    if strategy and allowed_strategies and strategy not in allowed_strategies:
        warnings.append({"code": "Q2_STRATEGY_MISMATCH", "reason": f"策略 {strategy} 不在固定枚举内"})
    return {"status": "fail" if errors else "pass", "errors": errors, "warnings": warnings, "policy_hits": policy_hits, "title_length": len(rendered), "body_length": len(head_body), "prefix_labels": prefix_labels, "rendered_title": rendered, "max_chars": max_chars}


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

# 纯机械、目标确定的缺陷：修法是确定的（砍到预算内、去掉结尾问号），
# 不涉及重新判断主旨、看点或归属。这类候选走独立预算的定向修复通道，
# 不参与 max_rewrite_rounds 的排队，也不会因为同批已有候选合格而被放弃。
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
    """返回这条候选「纯机械、目标确定」的缺陷清单；不属于这一类就返回空。

    只认长度和问号结尾：这两类缺陷的修法是确定的（砍字、去掉问号），
    不涉及重新判断主旨、看点或归属。带 G1/G3 revise 的候选（缺归属、缺限定、
    看点跑了）必须走语义重写——把它们混进来会让「压缩」变成偷偷改题。
    """
    if (decision or {}).get("status") != "rewrite":
        return []
    # 命中了业务政策（含 P9 人身攻击词）就不进压缩通道：压缩只砍字，不会把违规词换掉，
    # 必须走语义重写，让模型按 forbid 清单重新表述。
    if (candidate.get("mechanical_checks") or {}).get("policy_hits"):
        return []
    errors = [e for e in ((candidate.get("mechanical_checks") or {}).get("errors") or []) if isinstance(e, dict)]
    if not errors:
        return []
    if not {str(e.get("code") or "") for e in errors} <= COMPRESSIBLE_MECHANICAL_CODES:
        return []
    gates = (review or {}).get("gates") or {}
    if any(str((gates.get(gate) or {}).get("status") or "needs_review") != "pass" for gate in ("G1", "G2", "G3")):
        return []
    return errors


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


def _decide_candidate(review: dict[str, Any], mechanical: dict[str, Any], *, schema_errors: list[str], business_policy: dict[str, Any]) -> dict[str, Any]:
    gates = dict(review.get("gates")) if isinstance(review.get("gates"), dict) else {}
    issues = list(review.get("issues")) if isinstance(review.get("issues"), list) else []
    if schema_errors:
        gates["G4"] = {"status": "fail", "reason": "评审输出结构校验失败", "errors": schema_errors}
        # 评审输出坏了，但机械校验已经给出了确定结论：可修的错误先让它进重写，
        # 否则每条"模型输出超长"都会绕过重写直接塞进人审队列（人审是最贵的一环）。
        repairable_mechanical = [e for e in mechanical.get("errors", []) if e.get("repairable")]
        if mechanical.get("status") == "fail" and repairable_mechanical:
            return {"candidate_id": review.get("candidate_id"), "status": "rewrite", "reason": "G4_REPAIRABLE_WITH_INVALID_REVIEW", "gates": gates, "quality_sum": None, "issues": issues, "repairable": True}
        return {"candidate_id": review.get("candidate_id"), "status": "needs_review", "reason": "REVIEW_SCHEMA_INVALID", "gates": gates, "quality_sum": None, "issues": issues, "repairable": False}
    gates["G4"] = {"status": mechanical["status"], "reason": "程序机械校验", "errors": mechanical["errors"]}
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

    def _call(self, task_id: str, node: str, name: str, user: dict[str, Any], schema: str, adapter: ModelAdapter | None = None) -> dict[str, Any]:
        system = prompt_for(name, schema)
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
        self.storage.event(task_id, node, "prompt_snapshot", {"task": name, "schema": schema, "model": model_name, "prompt_file": prompt_file, "prompt_constant": prompt_constant, "prompt_hash": prompt_hash, "request_hash": request_hash, "system_prompt": system, "user_payload": user})
        cached = self.storage.cached_model_reply(task_id, request_hash)
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
                    "instruction": "上一次输出不是合法 JSON。请只返回一个严格合法的 JSON 对象：键与字符串都用双引号，字符串内部的引号必须转义成 \" ；不要尾逗号，不要 markdown 代码块，不要任何解释文字。若内容太长被截断，请压缩各字段的说明文字，保留全部必需字段。",
                },
            }
            try:
                reply = selected_adapter.complete(task=name, system=system, user=attempt_user, schema=schema, attempt=attempt)
                if reply.status != "ok" or not isinstance(reply.data, dict): raise ModelError("invalid model response", True)
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
        """一次评审多个候选；整批解析失败时退化为逐条评审。

        真实故障：三个候选一起评，模型输出的 JSON 在第二个候选处结构坏掉，
        重试三次仍然坏，整条视频被判 technical_failed——三个候选一条都没留下。
        逐条评审让小请求更不容易坏，也让"坏一条"不再等于"全灭"。
        """
        try:
            return self._call(task_id, "R01", "review", self._review_input(video, state, candidates, checks), "ReviewResultBatch", self.review_adapter)
        except ModelError as exc:
            # 付款、鉴权等不可重试错误不能降级成逐候选请求，否则只会重复失败。
            if not exc.retryable or len(candidates) <= 1:
                raise
            self.storage.event(task_id, "R01", "review_batch_degraded", {"candidates": len(candidates), "error": str(exc)[:300]})
        merged: dict[str, Any] = {"reviews": []}
        failures: list[str] = []
        for candidate in candidates:
            cid = str(candidate.get("candidate_id") or "")
            single_checks = [c for c in checks if str(c.get("candidate_id") or "") == cid]
            try:
                one = self._call(task_id, "R01", "review", self._review_input(video, state, [candidate], single_checks), "ReviewResultBatch", self.review_adapter)
            except ModelError as exc:
                failures.append(cid)
                self.storage.event(task_id, "R01", "review_single_failed", {"candidate_id": cid, "error": str(exc)[:200]})
                continue
            merged["reviews"].extend(one.get("reviews") or [])
            if one.get("rubric_version"):
                merged["rubric_version"] = one["rubric_version"]
        if not merged["reviews"]:
            raise ModelError(f"批量评审与逐条评审均失败：{','.join(failures) or '无候选'}", retryable=False)
        merged["degraded_to_per_candidate"] = True
        merged["degraded_failures"] = failures
        self.storage.event(task_id, "R01", "review_batch_degraded_complete", {"recovered": len(merged["reviews"]), "failed": failures})
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
            boundary_payload = {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "primary_evidence_audit": primary_audit, "rules": [asdict(x) for x in load_rules()], "matched_rules": rule_bundle.as_context()}
            boundary_raw = self._call(task_id, "N05", "boundary", boundary_payload, "BoundarySet")
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
                boundary_raw = self._call(task_id, "N05", "boundary", boundary_payload, "BoundarySet")
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
                "boundary": to_dict(boundary), "matched_rules": rule_bundle.as_context(),
            }, "HighlightSet")
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
                for round_no in range(1, self.config.max_rewrite_rounds + 1):
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
                            "structural_candidates": structures, "boundary": to_dict(boundary), "matched_rules": rule_bundle.as_context(),
                            "previous_thesis_contract": state.get("thesis_contract", {}), "independent_thesis_contract": state.get("review_thesis_contract", {}),
                            "review_issues": [i for r in candidates["reviews"] for i in r.get("issues", [])],
                        }, "HighlightSet")
                        retry_contract, retry_errors = normalize_thesis_contract(retry_raw, understanding, boundary)
                        retry_highlights = _normalize_highlights(retry_raw, {d.claim_id for d in allowed}, [])
                        retry_errors += validate_highlight_alignment(retry_highlights, retry_contract)
                        if retry_raw.get("abandon") or retry_raw.get("no_viable_highlight") or retry_errors:
                            state["highlight_reselection_errors"] = retry_errors or ["无法选择可交付主看点"]
                            candidates["route"] = {"action": "needs_review", "reason": "HIGHLIGHT_RESELECTION_INVALID", "recommended_candidate_id": None, "eligible_candidate_ids": []}
                            break
                        highlights = retry_highlights
                        # 重选看点会给出全新的主旨契约，旧契约上的主线降级随之丢失：
                        # 不在这里重新收敛一次，core_unit_ids 会退回「合集」，core_hook 又去写尾段。
                        retry_demoted = _enforce_single_mainline(retry_contract, understanding, video)
                        if retry_demoted:
                            state["mainline_demoted_units"] = retry_demoted
                            self.storage.event(task_id, "N07", "mainline_demoted", {"kept": retry_contract.get("core_unit_ids"), "demoted": retry_demoted, "source": "highlight_reselected"})
                        state["thesis_contract"] = retry_contract
                        state["thesis_contract_errors"] = []
                        state["thesis_alignment_errors"] = []
                        state["main_highlight_relation"] = "core"
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
                        rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, round_no, source, known_candidates=candidates["candidates"])
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
        required = normalize_prefix_labels(required_prefixes(format=video.format, attributes=understanding.attributes), signal_report["required_prefixes"])
        state["required_prefixes"] = required
        boundary = _normalize_boundary(state.get("boundary", {}))
        state["reference_audit"] = audit_evidence(understanding, video, boundary)
        evidence_types = {e.evidence_id: e.quality for e in video.evidence}
        state["source_only_claim_ids"] = [c.claim_id for c in understanding.claims if c.evidence_refs and all(evidence_types.get(r) == "source" for r in c.evidence_refs)]
        boundary, _ = _apply_primary_evidence_guard(boundary, _primary_evidence_audit(understanding, video), understanding)
        state["boundary"] = to_dict(boundary)
        # 重评同样要按边界收窄可引用单元，否则已排除单元会在重评里被放行
        if boundary.unit_decisions:
            rereview_units = {d.unit_id for d in boundary.unit_decisions if d.decision != "excluded"}
        else:
            rereview_units = {u.unit_id for u in understanding.semantic_units}
        for c in state["candidates"]:
            # 重评必须与初次生成共用同一个有效集合：待核验的 claim 不能因为"再评一次"就重新进白名单
            c["mechanical_checks"] = _mechanical_check(c, allowed_claims={d.claim_id for d in boundary.decisions if d.decision != "excluded" and not d.verification_required}, allowed_highlights={h["highlight_id"] for h in state.get("highlights", [])}, evidence_ids={e.evidence_id for e in video.evidence}, max_chars=self.config.max_chars, allowed_units=rereview_units, required_prefixes=required, claim_decisions=boundary.decisions)
            if c.get("slot_id") == "core_hook":
                snapshot_highlights = state.get("highlights") or []
                main_hl = next((h.get("highlight_id") for h in snapshot_highlights if h.get("role") == "main"), None)
                if main_hl and c.get("highlight_id") != main_hl:
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
                errors.append({"code": "G4_NUMERIC_CONFLICT", "repairable": True, "reason": "重新评审发现数字缺引用证据支持"})
            c["mechanical_checks"]["errors"].extend(errors)
            if errors:
                c["mechanical_checks"]["status"] = "fail"
            required_claims = {d.claim_id for d in boundary.decisions if d.attribution_required}
            c["programmatic_g1_flags"] = []
            rr_phrases = [d.required_source_phrase for d in boundary.decisions
                          if d.attribution_required and d.claim_id in set(c.get("claim_ids", []))]
            if _declares_narration(c.get("prefix_labels")) and rr_phrases and all(_carrier_only_source(x) for x in rr_phrases):
                required_claims = set()
            if required_claims.intersection(c.get("claim_ids", [])) and not _has_explicit_source_relation(c.get("headline", "")):
                c["programmatic_g1_flags"].append({"code": "G1_SOURCE_ATTRIBUTION_MISSING", "repairable": True, "reason": "本条边界明确要求来源归属，但标题未表达转述关系"})
        checks = [{"candidate_id": c.get("candidate_id"), **c["mechanical_checks"], "programmatic_g1_flags": c.get("programmatic_g1_flags", [])} for c in state["candidates"]]
        review_raw = self._review_with_fallback(task_id, video, state, state["candidates"], checks)
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in state["candidates"]])
        reviews, decisions = self._validate_and_decide_reviews(video, state, state["candidates"], reviews, checks)
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
        return {
            "video_materials": {"title": video.title, "author": video.author, "domain": video.domain, "format": video.format, "description": video.description, "caption": video.caption, "asr_segments": video.asr_segments, "ocr_segments": video.ocr_segments},
            "evidence": to_dict(video.evidence),
            "claims": state.get("understanding", {}).get("claims", []),
            "risk_signals": state.get("risk_signals", {}),
            "boundary": state.get("boundary", {}),
            "highlights": state.get("highlights", []),
            "thesis_contract": state.get("thesis_contract", {}),
            "review_thesis_contract": state.get("review_thesis_contract", {}),
            "matched_rules": state.get("rules", {}),
            "business_policy": state.get("business_policy", {"status": "missing", "rules": [], "version": None}),
            "display_config": {"max_chars": self.config.max_chars, "prefix_labels": state.get("required_prefixes", []), "counting": "前缀与正文合计不超过 max_chars", "ending_question_mark_forbidden": True},
            "mechanical_checks": mechanical_checks,
            "candidate_titles": [{"candidate_id": c.get("candidate_id"), "title": c.get("headline"), "prefix_labels": c.get("prefix_labels", []), "rendered_title": c.get("rendered_title") or render_title(c), "claim_ids": c.get("claim_ids", []), "highlight_id": c.get("highlight_id"), "evidence_ids": c.get("evidence_ids", []), "strategy": c.get("expression_strategy"), "qualifiers_used": c.get("qualifiers_used", [])} for c in candidates],
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
            # 主旨覆盖判定：评审在 G3 里显式给 core_covered=false，即使 gate 状态误写成 pass 也须纠正，
            # 回看点选择修核心，而不是凭「视频里确实出现过该片段」放行。
            g3_gate = review.get("gates", {}).get("G3", {})
            if isinstance(g3_gate, dict) and g3_gate.get("status") == "pass" and g3_gate.get("core_covered") is False:
                review["gates"]["G3"] = {**g3_gate, "status": "revise"}
                core_issue = {"issue_id": f"{cid}-G3_CORE_POINT_OMITTED", "primary_dimension": "G3", "issue_code": "G3_CORE_POINT_OMITTED", "severity": "high", "repairable": True, "problem_span": candidate.get("headline", ""), "evidence_ids": g3_gate.get("evidence_ids", []) or [], "rule_ids": g3_gate.get("rule_ids", []) or [], "reason": str(g3_gate.get("reason") or "标题未覆盖视频核心观看承诺"), "action": "回到看点选择，改用核心主旨表达", "rollback_to": "N07"}
                if not any(i.get("issue_code") == "G3_CORE_POINT_OMITTED" for i in review.get("issues", []) if isinstance(i, dict)):
                    review.setdefault("issues", []).append(core_issue)
            decision = _decide_candidate(review, mechanical, schema_errors=validation_errors, business_policy=state.get("business_policy", {"status": "missing"}))
            if candidate.get("signal_review_reasons") and decision["status"] == "ready_for_human_review":
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
            "structural_candidates": structures, "boundary": to_dict(boundary), "matched_rules": rule_bundle.as_context(),
            "previous_thesis_contract": left, "independent_thesis_contract": right,
            "basis": "review",
            "basis_note": "以 independent_thesis_contract（复核侧独立读法）为主旨基准重新选看点；若上一版看点漏掉了该读法认为不可删的要素，必须选出承载它的看点。",
            "review_issues": [],
        }, "HighlightSet")
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
        """纯机械缺陷的定向修复：独立预算，不受「已有候选合格就交付」影响。

        真实故障（2026-11-01 三条 canary）：五条候选只因超 1–2 个字被判 repairable，
        但 `_route_candidates` 见到任意一条 ready 就直接 handoff，`max_rewrite_rounds`
        一次都没用上——超字数的候选被判了「可修」然后原样丢弃。反过来资讯那条
        一条都不合格，语义重写用满 2 轮也修不动缺归属的问题。

        两件事分开：这里只修「压一下就好」的缺陷，按候选独立计数、压不动就停，
        不占用也没有抢占 max_rewrite_rounds 的语义重写预算。
        """
        limit = int(getattr(self.config, "max_mechanical_repairs", 0) or 0)
        if limit <= 0:
            return candidates
        reviews = {r.get("candidate_id"): r for r in candidates.get("reviews", [])}
        targets = []
        for decision in candidates.get("decisions", []):
            source = next((c for c in candidates.get("candidates", []) if c.get("candidate_id") == decision.get("candidate_id")), None)
            if source is None:
                continue
            defects = _compressible_defects(source, decision, reviews.get(decision.get("candidate_id")))
            if defects:
                targets.append((source, defects))
        if not targets:
            return candidates
        state["mechanical_repair_targets"] = [c["candidate_id"] for c, _ in targets]
        repaired: list[str] = []
        for source, defects in targets:
            for attempt in range(1, limit + 1):
                before = {c.get("candidate_id"): str(c.get("rendered_title") or "") for c in candidates["candidates"]}
                # 每轮换一个 candidate_id 后缀空间，避免与既有候选撞 id；用负 round 段位与语义重写区分开。
                rewritten = self._generate_and_review(task_id, state, video, understanding, boundary, highlights, -(round_no + attempt), source, repair_mode="压缩")
                if not rewritten.get("candidates"):
                    break
                candidates["candidates"] += rewritten["candidates"]
                candidates["reviews"] += rewritten["reviews"]
                candidates["decisions"] += rewritten["decisions"]
                # 这一版的 id 会进重写命名前缀，锚到 parent 上便于界面显示「上一版」。
                fixed = [c for c in rewritten["candidates"] if str(c.get("rendered_title") or "") != before.get(source["candidate_id"])]
                self.storage.event(task_id, "N09", "mechanical_repair", {
                    "parent": source["candidate_id"], "attempt": attempt, "mode": "压缩",
                    "defects": [e.get("code") for e in defects],
                    "parent_title": before.get(source["candidate_id"]),
                    "repaired_titles": [c.get("rendered_title") for c in rewritten["candidates"]],
                    "repaired_ids": [c.get("candidate_id") for c in rewritten["candidates"]],
                    "new_titles": len(fixed),
                })
                if fixed:
                    repaired += [c.get("candidate_id") for c in fixed]
                    break
        if not repaired and not state.get("mechanical_repair_targets"):
            return candidates
        candidates["route"] = self._route_candidates(candidates["decisions"], state.get("rewrite_round", 0))
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

    def _generate_and_review(self, task_id: str, state: dict[str, Any], video: Any, understanding: Understanding, boundary: BoundarySet, highlights: list[Highlight], rewrite_round: int, source: dict[str, Any] | None = None, repair_mode: str | None = None, skip_thesis_review: bool = False, thesis_basis: str | None = None, known_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        signal_report = extract_risk_signals(video)
        state["risk_signals"] = signal_report
        required = normalize_prefix_labels(required_prefixes(format=video.format, attributes=understanding.attributes), signal_report["required_prefixes"])
        budget = body_budget(self.config.max_chars, required)
        state["required_prefixes"] = required
        state["body_budget"] = budget
        # 暂缓核验的陈述不产生「来源/限定」要求：它本来就不该被写进标题
        source_requirements = [to_dict(decision) for decision in boundary.decisions if decision.decision != "excluded" and not decision.verification_required and (decision.attribution_required or decision.required_source_phrase or decision.required_limits or decision.forbidden_inferences)]
        blocked_claims = [
            {"claim_id": decision.claim_id, "forbidden_inferences": decision.forbidden_inferences,
             "reason": "暂缓核验" if decision.verification_required else "已排除", "unresolved": decision.verification_required}
            for decision in boundary.decisions
            if decision.decision == "excluded" or decision.verification_required
        ]
        user = {"video": to_dict(video), "claims": to_dict(understanding.claims), "semantic_units": to_dict(understanding.semantic_units), "unit_decisions": to_dict(boundary.unit_decisions), "boundary": to_dict(boundary), "source_requirements": source_requirements, "blocked_claims": blocked_claims, "unresolved_issues": boundary.unresolved_issues, "highlights": to_dict(highlights), "thesis_contract": state.get("thesis_contract", {}),  "matched_rules": state.get("rules", {}), "input_quality": state.get("input_quality", {}), "source_evidence_usage": state.get("source_evidence_usage", {}), "display_config": {"max_chars": self.config.max_chars, "max_chars_body": budget, "prefix_labels": required, "prefix_rendered": render_labels(required), "counting": "前缀与正文合计不超过 max_chars", "ending_question_mark_forbidden": True}, "strategy_enum": rubric().get("title_policy", {}).get("strategy_enum", []), "target_count": self.config.rewrite_candidate_count if source else self.config.target_count, "rewrite_round": rewrite_round}
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
                }
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
            same_video = self.memory.search_for_video(video.video_id, self.config.prompt_pack_version)
            if same_video:
                user["same_video_cases"] = same_video
                state.setdefault("memory_trace", []).append({"query": video.video_id, "status": "same_video", "used_case_ids": [c.get("case_id") for c in same_video]})
        generation_task = "rewrite" if source else "generate"
        draft = self._call(task_id, "N09", generation_task, user, "AgentDraft")
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
        # 「边界未输出 unit_decisions」与「所有单元均被明确排除」不能混为一谈：后者是闭合的排除结果，
        # 不得因集合为空就把全部单元复活。只有边界确实没给 unit_decisions 时才沿用旧行为。
        if boundary.unit_decisions:
            # 只有被明确判 excluded 的单元不可写。边界「没提到」的单元是没表态，不是不在范围——
            # 真实故障：边界只给了 u6 excluded，这里被算成「只剩空集」，于是所有候选的
            # unit_id=u1 都被判 G4_INVALID_REFERENCE，整条视频零交付。
            excluded_units = {d.unit_id for d in boundary.unit_decisions if d.decision == "excluded"}
            allowed_units = {u.unit_id for u in understanding.semantic_units if u.unit_id not in excluded_units}
        else:
            allowed_units = {u.unit_id for u in understanding.semantic_units}
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
                signal_errors.append({"code": "G4_NUMERIC_CONFLICT", "repairable": True, "reason": "标题数值、单位或指标没有对应引用证据支持；不得用原题数字白名单替代核验"})
            if c.get("slot_id") == "core_hook":
                main_hl = next((h.highlight_id for h in highlights if getattr(h, "role", "") == "main"), None)
                if main_hl and c.get("highlight_id") != main_hl:
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
            if source_required and not _has_explicit_source_relation(c.get("headline", "")):
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
        declared = _normalize_thesis_consistency(review_raw)
        if declared:
            declared["round"] = rewrite_round
            state.setdefault("thesis_consistency_history", []).append(declared)
            state["thesis_consistency"] = declared
        state["prompt_hashes"]["review"] = self._last_prompt_hash(task_id, "R01", "review")
        reviews = _normalize_reviews(review_raw, [c["candidate_id"] for c in candidates])
        reviews, decisions = self._validate_and_decide_reviews(video, state, candidates, reviews, checks)
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
        # 兜底：任何路径都不能让候选从人工视野里消失。
        # 各分支（THESIS_CONTRACT_INVALID / SUBROUTE_ESCALATION / HIGHLIGHT_RESELECTION_LIMIT
        # / REWRITE_BUDGET_EXHAUSTED）各自现拼 route，很容易忘了带候选清单——一旦漏掉，
        # 人工连「要看哪条」都不知道。这里统一检查：清单为空但状态里已有未被业务拒绝的候选，
        # 就整组转为「待人工核实」（交给人工，但不自动放行）。
        surface = (set(route.get("eligible_candidate_ids") or [])
                   | set(route.get("pending_review_candidate_ids") or [])
                   | set(route.get("candidate_ids") or []))
        if not surface:
            # rewrite 也要捞：它表示「系统认为值得改、但重写预算已经用尽」。
            # 走到 _handoff 时流程已终结，这些候选就是终态产物——不交出去，人工看到的
            # 就只有空清单（BV1qBxE6JESv 九条全 rewrite、BV1uWnZ6vEvj 三条全 rewrite 都栽在这）。
            # rejected 仍然排除：那是系统明确判定不可用的。
            kept = [str(d.get("candidate_id")) for d in (state.get("candidate_decisions") or [])
                    if d.get("candidate_id")
                    and d.get("status") in {"ready_for_human_review", "needs_review", "rewrite"}]
            if kept:
                route = {**route, "pending_review_candidate_ids": kept}
                state["route"] = route
        status = "pending_human" if route["action"] in {"handoff", "needs_review"} else "stopped"
        state["uncalibrated"] = True
        self.storage.checkpoint(task_id, status, state, "N12", "human_handoff", {"status": status, "route": route})
        self.storage.event(task_id, "N12", "handoff", {"status": status, "route": route})
        return task_id

    def _stop(self, task_id: str, state: dict[str, Any], reason: str) -> str:
        state["stop_reason"] = reason; self.storage.set_state(task_id, "stopped", state); self.storage.event(task_id, "X02", "business_stop", {"reason": reason}); return task_id

