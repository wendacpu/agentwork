"""边界节点把决策拆到不同层级时，两层都必须被读到。

两个 fixture 是 2026-11-02 Flash 对照批次里真实失败的 N05 原始响应（逐字保存）。
改前：A 只解析出 2 条 excluded（5 条可用陈述连同 required_limits 一起被分组摘要覆盖掉），
B 的 13 条 claim 全部默认 excluded（真实决策躺在 nested boundary_set 里没人读）。
两个任务因此都拿不到可写集合，白烧一次 2 万 token 的调用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_system.pipeline import (
    BOUNDARY_DECISION_MISSING,
    _missing_boundary_claims,
    _normalize_boundary,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"]


def _claim_maps(raw: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    texts: dict[str, str] = {}
    evidence: dict[str, list[str]] = {}
    for claim in raw.get("claims") or []:
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            continue
        cid = str(claim["claim_id"])
        texts[cid] = str(claim.get("text") or "")
        evidence[cid] = [str(x) for x in (claim.get("evidence_refs") or [])]
    for unit in raw.get("semantic_units") or []:
        if not isinstance(unit, dict) or not unit.get("unit_id"):
            continue
        evidence.setdefault(str(unit["unit_id"]), [])
    return texts, evidence


# --- 形状 A：顶层 allowed_claims 分组 + 顶层完整 claims 对象 ---

def test_allowed_claims_alias_is_recognized():
    """allowed_claims 是 usable 的同义写法；漏认它会把 5 条可用读成 0。"""
    raw = _load("n05_grouped_allowed_claims.json")
    texts, evidence = _claim_maps(raw)
    boundary = _normalize_boundary(raw, texts, evidence)
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert set(by_id) == {"c1", "c2", "c3", "c4", "c5", "c6", "c7"}
    assert by_id["c1"].decision == "usable"
    assert [cid for cid, d in by_id.items() if d.decision == "limited"] == ["c2", "c4", "c5", "c6"]
    assert by_id["c3"].decision == "excluded"
    assert by_id["c7"].decision == "excluded"
    assert _missing_boundary_claims(boundary) == []


def test_full_claim_objects_survive_the_group_summary():
    """分组摘要只有 decision，不能把完整对象的 required_limits / 来源信息抹掉。"""
    raw = _load("n05_grouped_allowed_claims.json")
    texts, evidence = _claim_maps(raw)
    boundary = _normalize_boundary(raw, texts, evidence)
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert by_id["c2"].required_limits and "放慢手法" in by_id["c2"].required_limits[0]
    assert by_id["c2"].forbidden_inferences
    assert by_id["c2"].rule_ids == ["U-B01", "U-S02", "F01"]
    # 归属要求只落在 c6，不能被全局摊到所有 claim
    assert by_id["c6"].attribution_required is True
    assert by_id["c1"].attribution_required is False


# --- 形状 B：决策分组藏在 nested boundary_set，顶层 claims 没有 decision 字段 ---

def test_nested_boundary_set_groups_are_read():
    raw = _load("n05_nested_boundary_set.json")
    texts, evidence = _claim_maps(raw)
    boundary = _normalize_boundary(raw, texts, evidence)
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert set(by_id) == {f"c{i}" for i in range(1, 14)}
    assert [cid for cid, d in by_id.items() if d.decision == "usable"] == [f"c{i}" for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12)]
    assert [cid for cid, d in by_id.items() if d.decision == "limited"] == ["c11", "c13"]
    assert all(d.decision != "excluded" for d in boundary.decisions)
    assert _missing_boundary_claims(boundary) == []


def test_nested_shape_keeps_claim_text_from_outer_layer():
    """完整对象在外层、决策在内层：合并后两边信息都要在。"""
    raw = _load("n05_nested_boundary_set.json")
    texts, evidence = _claim_maps(raw)
    boundary = _normalize_boundary(raw, texts, evidence)
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert by_id["c1"].supported_statement == texts["c1"]
    assert by_id["c1"].supported_statement


# --- 模型漏答：兜底可以，但必须留痕 ---

def test_missing_decision_is_recorded_not_silently_excluded():
    raw = {"claims": [{"claim_id": "c1", "decision": "usable"}, {"claim_id": "c2", "text": "乙做了B"}]}
    boundary = _normalize_boundary(raw, {"c1": "甲做了A", "c2": "乙做了B"}, {})
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert by_id["c2"].decision == "excluded"  # 保守兜底不变
    assert _missing_boundary_claims(boundary) == ["c2"]
    assert any(isinstance(i, dict) and i.get("code") == BOUNDARY_DECISION_MISSING for i in boundary.unresolved_issues)


def test_boolean_usable_field_is_not_read_as_garbage():
    """{"usable": true} 过去被 str() 成 "True" 再判成 excluded。"""
    raw = {"decisions": [{"claim_id": "c1", "usable": True}, {"claim_id": "c2", "usable": False}]}
    boundary = _normalize_boundary(raw, {"c1": "a", "c2": "b"}, {})
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert by_id["c1"].decision == "usable"
    assert by_id["c2"].decision == "excluded"
    assert _missing_boundary_claims(boundary) == []


def test_unrecognized_decision_value_counts_as_missing():
    raw = {"decisions": [{"claim_id": "c1", "decision": "待定"}]}
    boundary = _normalize_boundary(raw, {"c1": "a"}, {})
    assert boundary.decisions[0].decision == "excluded"
    assert _missing_boundary_claims(boundary) == ["c1"]


@pytest.mark.parametrize("value,expected", [
    ("usable", "usable"),
    ("allowed", "usable"),
    ("eligible", "usable"),
    ("usable_with_limits", "limited"),
    ("verification_required", "limited"),
    ("excluded", "excluded"),
    ("forbidden", "excluded"),
])
def test_decision_enum_aliases(value, expected):
    raw = {"decisions": [{"claim_id": "c1", "decision": value}]}
    boundary = _normalize_boundary(raw, {"c1": "a"}, {})
    assert boundary.decisions[0].decision == expected
