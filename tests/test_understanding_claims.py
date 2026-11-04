"""理解节点为空时不许让下游替它编造事实。

真实故障（BV1WaHN6GEE1 / BV1Kcso6KEdr，2026-11-01）：理解提示词把 candidate_claims
描述成「语义单元内的原子事实层」，模型于是只在 semantic_units 里写 claim_ids、
整批省略 claim 对象。解析端只认顶层 claims/candidate_claims/facts，得到 0 条 claim，
却照常记录 claim_count=0 继续往下走；边界节点拿到空 claim 集合后自己编了 33 条
claim_id（c1..c33），引用审计发现全部悬空，把每条陈述都判死，整条视频最终停在
X02 business_stop / NO_USABLE_CLAIM 或 PENDING_VERIFICATION——
一个把人引向「引用不全」的误导性死因，而真实原因是理解节点空转。

本文件锁住三件事：
1. 嵌在语义单元里的 claim 对象要被提上来（能救回的输出去救）；
2. 单元声明的 claim_id 要有单独的提取口径，供上层判断输出是否自相矛盾；
3. 提示词必须要求「顶层」candidate_claims 数组，且禁止只写 ID 不写对象。
"""
from __future__ import annotations

from agent_system.pipeline import (
    _claims_nested_in_units,
    _normalize_understanding,
    _unit_claim_refs,
)
from agent_system.prompts import UNDERSTAND_PROMPT


def test_lifts_claims_nested_inside_semantic_units():
    units = [{"unit_id": "u1", "claim_ids": ["c1", "c2"], "candidate_claims": [
        {"claim_id": "c1", "claim_text": "作者慢跑步频175"},
        {"claim_id": "c2", "claim_text": "作者全马PB步频185"},
    ]}]
    assert [x["claim_id"] for x in _claims_nested_in_units(units)] == ["c1", "c2"]


def test_nested_claim_lifting_deduplicates_across_units():
    units = [
        {"unit_id": "u1", "claims": [{"claim_id": "c1", "claim_text": "甲"}]},
        {"unit_id": "u2", "claims": [{"claim_id": "c1", "claim_text": "甲"}]},
    ]
    assert len(_claims_nested_in_units(units)) == 1


def test_nested_claim_lifting_tolerates_garbage_shapes():
    assert _claims_nested_in_units(None) == []
    assert _claims_nested_in_units([None, 3, "x"]) == []
    assert _claims_nested_in_units([{"unit_id": "u1"}]) == []


def test_unit_claim_refs_collects_declared_ids():
    assert _unit_claim_refs([{"claim_ids": ["c1", "c2"]}, {"claim_ids": ["c3"]}]) == {"c1", "c2", "c3"}


def test_unit_claim_refs_ignores_missing_or_malformed_ids():
    assert _unit_claim_refs([{"unit_id": "u1"}]) == set()
    assert _unit_claim_refs([{"claim_ids": "c1"}]) == set()
    assert _unit_claim_refs(None) == set()


def test_normalize_recovers_claims_nested_in_units():
    """模型把 claim 对象嵌在单元里的输出，应当被救回而不是变成 0 条。"""
    raw = {
        "content_mode": "口播",
        "semantic_units": [{
            "unit_id": "u1", "meaning": "步频随配速变化",
            "claim_ids": ["c1", "c2"],
            "candidate_claims": [
                {"claim_id": "c1", "claim_text": "作者慢跑步频175"},
                {"claim_id": "c2", "claim_text": "作者全马PB步频185"},
            ],
        }],
    }
    understanding = _normalize_understanding(raw)
    assert [c.claim_id for c in understanding.claims] == ["c1", "c2"]
    assert [u.unit_id for u in understanding.semantic_units] == ["u1"]


def test_normalize_prefers_top_level_claims_over_nested():
    """顶层已有 claims 时不受嵌套兜底影响，避免把两份陈述混起来。"""
    raw = {
        "claims": [{"claim_id": "c1", "claim_text": "顶层陈述"}],
        "semantic_units": [{"unit_id": "u1", "claim_ids": ["c1"], "candidate_claims": [
            {"claim_id": "c9", "claim_text": "嵌套陈述"},
        ]}],
    }
    understanding = _normalize_understanding(raw)
    assert [c.claim_id for c in understanding.claims] == ["c1"]


def test_normalize_still_empty_when_no_claim_objects_at_all():
    """这正是真实故障：只有 claim_ids，没有任何 claim 对象。修不了就必须能被识别。"""
    raw = {
        "content_mode": "口播",
        "semantic_units": [{"unit_id": "u1", "claim_ids": ["c1", "c2", "c3"]}],
    }
    understanding = _normalize_understanding(raw)
    assert understanding.claims == []
    # 上层据此判定「输出自相矛盾」，而不是当成「内容不可写」。
    assert _unit_claim_refs(raw["semantic_units"]) == {"c1", "c2", "c3"}


def test_understand_prompt_requires_top_level_candidate_claims_array():
    assert "**顶层** candidate_claims 数组" in UNDERSTAND_PROMPT
    assert "与 semantic_units 同级" in UNDERSTAND_PROMPT


def test_understand_prompt_forbids_undefined_claim_ids():
    assert "只能引用该数组中已定义的 claim_id" in UNDERSTAND_PROMPT
    assert "不得只写 c1、c2 这类 ID" in UNDERSTAND_PROMPT


def test_understand_prompt_drops_old_ambiguous_wording():
    """旧措辞「语义单元内的原子事实层」正是模型省略 claim 对象的诱因。"""
    assert "candidate_claims 是语义单元内的原子事实层" not in UNDERSTAND_PROMPT
