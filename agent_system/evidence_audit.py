"""对引用关系做只读结构审计，不判断证据是否支持事实。

结果是 JSON 可序列化字典；issues 中的 path 可精确定位对象，
by_object 以同一对象路径索引问题下标。turn 没有独立 ID，使用列表索引。
"""
from __future__ import annotations

import math
import re
from typing import Any

from .types import BoundarySet, Understanding, VideoInput


_PRIMARY = {"asr", "ocr"}
_CONTEXT = {"title", "description", "caption", "source_description"}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"


def _location(evidence: Any, duration: Any) -> tuple[str, str]:
    """只校验当前数据格式中的秒数定位，不推断文本与片段的语义一致性。"""
    locator = evidence.locator
    if locator is None or not str(locator).strip():
        return "unknown", "无时间码"
    if not isinstance(locator, str):
        return "invalid", "时间码必须是字符串"
    text = locator.strip()
    if text.lower() in {"unknown", "none", "null"}:
        return "unknown", "无时间码"
    field = evidence.field.lower()
    if field == "asr":
        match = re.fullmatch(rf"asr:\s*({_NUMBER})\s*-\s*({_NUMBER})\s*s?", text, re.I)
        missing = re.fullmatch(r"asr:\s*(?:none|null|unknown)?\s*-\s*(?:none|null|unknown)?", text, re.I)
        if missing:
            return "unknown", "无时间码"
        if not match:
            return "invalid", "ASR 时间码格式非法或不完整"
        values = [float(match.group(1)), float(match.group(2))]
        if values[0] > values[1]:
            return "invalid", "ASR 起点晚于终点"
    else:
        match = re.fullmatch(rf"ocr:\s*({_NUMBER})\s*s?", text, re.I)
        if re.fullmatch(r"ocr:\s*(?:none|null|unknown)\s*s?", text, re.I):
            return "unknown", "无时间码"
        if not match:
            return "invalid", "OCR 时间码格式非法"
        values = [float(match.group(1))]
    if any(not math.isfinite(x) or x < 0 for x in values):
        return "invalid", "时间码必须是非负有限数"
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0:
        if any(x > duration for x in values):
            return "invalid", "时间码超出视频时长"
    return "valid", ""


def audit_evidence(understanding: Understanding, video: VideoInput, boundary: BoundarySet | None = None) -> dict[str, Any]:
    """审计 ID、引用和定位；绝不修改输入，也不输出事实真实性结论。"""
    issues: list[dict[str, Any]] = []
    by_object: dict[str, list[int]] = {}

    def safe_value(value: Any) -> Any:
        if not isinstance(value, (str, int, float, bool, type(None))) or isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        return value

    def issue(path: str, field: str, code: str, value: Any, message: str) -> None:
        # 引用字段按数据模型为字符串；异常输入也转换为可序列化值。
        value = safe_value(value)
        by_object.setdefault(path, []).append(len(issues))
        issues.append({"path": path, "field": field, "code": code, "value": value, "message": message})

    def index(items: list[Any], attr: str, root: str) -> tuple[set[str], set[str]]:
        result: set[str] = set()
        duplicates: set[str] = set()
        for i, item in enumerate(items):
            path = f"{root}[{i}]"
            value = getattr(item, attr, None)
            if not isinstance(value, str) or not value.strip():
                issue(path, attr, "empty_id", value, "ID 为空或不是字符串")
            elif value != value.strip():
                issue(path, attr, "invalid_id", value, "ID 首尾不得包含空白")
            elif value in result:
                duplicates.add(value)
                issue(path, attr, "duplicate_id", value, "ID 重复，引用无法唯一定位")
            else:
                result.add(value)
        return result, duplicates

    evidence_ids = index(video.evidence, "evidence_id", "video.evidence")
    claim_ids = index(understanding.claims, "claim_id", "understanding.claims")
    unit_ids = index(understanding.semantic_units, "unit_id", "understanding.semantic_units")
    event_ids = index(understanding.events, "event_id", "understanding.events")
    index(understanding.extracted_sources, "source_id", "understanding.extracted_sources")

    def refs(path: str, field: str, values: Any, targets: tuple[set[str], set[str]], required: bool = False) -> None:
        valid, duplicates = targets
        if not isinstance(values, (list, tuple)):
            issue(path, field, "invalid_refs", values, "引用必须是列表")
            return
        if required and not values:
            issue(path, field, "empty_refs", None, "引用列表为空")
        seen_refs: set[str] = set()
        for value in values:
            if isinstance(value, str) and value in seen_refs:
                issue(path, field, "duplicate_ref", value, "引用重复")
            if isinstance(value, str):
                seen_refs.add(value)
            if not isinstance(value, str) or not value.strip():
                issue(path, field, "empty_ref", value, "引用为空或不是字符串")
            elif value != value.strip():
                issue(path, field, "invalid_ref", value, "引用首尾不得包含空白")
            elif value not in valid:
                issue(path, field, "dangling_ref", value, "引用目标不存在")
            elif value in duplicates:
                issue(path, field, "ambiguous_ref", value, "引用目标 ID 重复，无法唯一定位")

    classifications = []
    for i, evidence in enumerate(video.evidence):
        path = f"video.evidence[{i}]"
        field = evidence.field.lower() if isinstance(evidence.field, str) else ""
        category = "primary_text" if field in _PRIMARY else "frame_path_unseen" if field == "frame" else "context" if field in _CONTEXT else "unknown"
        location = "not_applicable"
        if field in {"asr", "ocr"}:
            location, reason = _location(evidence, video.duration)
            if location != "valid":
                issue(path, "locator", "location_" + location, evidence.locator, reason)
        classifications.append({"path": path, "evidence_id": safe_value(evidence.evidence_id), "field": safe_value(evidence.field),
                                "category": category, "location": location})
        refs(path, "derived_from", evidence.derived_from, evidence_ids)

    for i, claim in enumerate(understanding.claims):
        refs(f"understanding.claims[{i}]", "evidence_refs", claim.evidence_refs, evidence_ids, True)
    for i, unit in enumerate(understanding.semantic_units):
        path = f"understanding.semantic_units[{i}]"
        refs(path, "claim_ids", unit.claim_ids, claim_ids, True)
        refs(path, "evidence_ids", unit.evidence_ids, evidence_ids, True)
        if isinstance(unit.corroboration, dict) and "evidence_ids" in unit.corroboration:
            refs(path, "corroboration.evidence_ids", unit.corroboration["evidence_ids"], evidence_ids)
    for i, event in enumerate(understanding.events):
        refs(f"understanding.events[{i}]", "evidence_ids", event.evidence_ids, evidence_ids, True)
    for i, turn in enumerate(understanding.turns):
        path = f"understanding.turns[{i}]"
        for field in ("from_event", "to_event"):
            refs(path, field, [getattr(turn, field)], event_ids)
    for i, source in enumerate(understanding.extracted_sources):
        refs(f"understanding.extracted_sources[{i}]", "evidence_refs", source.evidence_refs, evidence_ids)
    if boundary is not None:
        # 同一目标多条边界决策会使下游按字典或列表读取时得到不同结论。
        index(boundary.decisions, "claim_id", "boundary.decisions")
        index(boundary.unit_decisions, "unit_id", "boundary.unit_decisions")
        for i, decision in enumerate(boundary.decisions):
            path = f"boundary.decisions[{i}]"
            refs(path, "claim_id", [decision.claim_id], claim_ids)
            refs(path, "evidence_refs", decision.evidence_refs, evidence_ids, decision.decision != "excluded" and not decision.verification_required)
            refs(path, "source_evidence_refs", decision.source_evidence_refs, evidence_ids)
        for i, decision in enumerate(boundary.unit_decisions):
            refs(f"boundary.unit_decisions[{i}]", "unit_id", [decision.unit_id], unit_ids)
    return {"scope": "reference_structure_only", "semantic_truth": "not_evaluated",
            "issues": issues, "by_object": by_object, "evidence": classifications}
