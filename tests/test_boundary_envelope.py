"""边界节点输出的两种信封形状都要能读出决策。

真实故障（2026-11-01 真实冒烟）：模型把决策放在外层 claims / semantic_units，
只把 claims_summary 摘要放进 BoundarySet 信封。解析器此前是「换成内层」，
于是拿到 0 条 decision，任务被判 NO_USABLE_CLAIM 静默停机（20 秒 + 2 万 token 白跑）。
"""
from __future__ import annotations

from agent_system.pipeline import _normalize_boundary

import pytest

CLAIM_TEXT = {"c1": "甲做了A", "c2": "乙做了B"}
CLAIM_EV = {"c1": ["e-asr-0000"], "c2": ["e-asr-0001"]}


def test_decisions_in_outer_layer_survive_the_envelope():
    raw = {
        "BoundarySet": {
            "claims_summary": {"usable": ["c1"], "limited": ["c2"], "excluded": []},
            "rule_ids": ["U-B01"],
            "global_forbidden_inferences": ["不得补出未获支持的细节"],
            "conflicts": [],
            "gaps": [],
        },
        "claims": [
            {"claim_id": "c1", "decision": "usable", "reason": "有 ASR 支撑"},
            {"claim_id": "c2", "decision": "limited", "reason": "只有转述"},
        ],
        "semantic_units": [{"unit_id": "u1", "decision": "usable"}],
    }
    boundary = _normalize_boundary(raw, CLAIM_TEXT, CLAIM_EV)
    by_id = {d.claim_id: d for d in boundary.decisions}
    assert set(by_id) == {"c1", "c2"}
    assert by_id["c1"].decision == "usable"
    assert by_id["c2"].decision == "limited"


def test_decisions_inside_the_envelope_still_work():
    raw = {"BoundarySet": {"decisions": [{"claim_id": "c1", "decision": "excluded", "reason": "无依据"}]}}
    boundary = _normalize_boundary(raw, CLAIM_TEXT, CLAIM_EV)
    assert [d.claim_id for d in boundary.decisions] == ["c1"]
    assert boundary.decisions[0].decision == "excluded"


def test_summary_only_envelope_yields_nothing():
    """只有摘要没有逐条决策：调用方要能看出这是"写丢了"，而不是"没有可写内容"。"""
    raw = {"BoundarySet": {"claims_summary": {"usable": ["c1"]}}}
    boundary = _normalize_boundary(raw, CLAIM_TEXT, CLAIM_EV)
    assert boundary.decisions == []
