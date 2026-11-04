"""归属预判：程序先给字面结论，评审不再对同一标题既见「商业观察」又判缺归属。

实证（2026-11-04 v2 小包装复测）：boundary 已把 required_source_phrase 修成「博主」，
生成也写了「商业观察：博主拆解小包装…」，评审仍 9/9 报 G1_SOURCE_ATTRIBUTION_MISSING——
它把短语当逐字关键词。attribution_check 把「有没有归属线索」变成程序事实。
"""
from types import SimpleNamespace

from agent_system.pipeline import _mechanical_check


def _claim(claim_id="c1", *, attribution=True, phrase="博主", limits=None):
    return SimpleNamespace(claim_id=claim_id, decision="usable", required_limits=list(limits or []),
                           attribution_required=attribution, required_source_phrase=phrase,
                           verification_required=False)


def _check(headline, decisions, **over):
    candidate = {"candidate_id": "a1", "headline": headline, "claim_ids": ["c1"],
                 "evidence_ids": ["e1"], "highlight_id": "h1", "prefix_labels": [],
                 "generation_schema_errors": []}
    candidate.update(over)
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                             evidence_ids={"e1"}, max_chars=20, claim_decisions=decisions)


def test_nature_prefix_counts_attribution_present():
    result = _check("商业观察：小包装小样为何走热", [_claim()])
    assert result["attribution_check"] == {"required": True, "phrases": ["博主"], "identity_present": True}


def test_explicit_phrase_match_counts():
    result = _check("博主：白菜不用爆炒", [_claim(phrase="博主")])
    assert result["attribution_check"]["identity_present"] is True


def test_pure_assertion_title_has_no_attribution():
    result = _check("小包装比大包装好卖", [_claim()])
    assert result["attribution_check"]["identity_present"] is False


def test_no_attribution_requirement_skips_check():
    result = _check("新手速学古早娇媚妆容", [_claim(attribution=False, phrase=None)])
    assert result["attribution_check"] is None


def test_institution_phrase_survives():
    result = _check("发改委：能耗双控新政落地", [_claim(phrase="据发改委")])
    assert result["attribution_check"]["identity_present"] is True


from agent_system.pipeline import _attribution_identity_present


def test_identity_fallback_accepts_nature_prefix_and_plain_subject():
    # 评审模型 v2 实测已判 pass，旗标仍把 G1 压成 revise——兜底必须认「商业观察」和裸「博主」。
    assert _attribution_identity_present("商业观察：博主拆解小包装小样为何成热点", ["博主"])
    assert _attribution_identity_present("商业观察：小包装小样为何成消费热点", ["博主"])
    assert _attribution_identity_present("发改委：能耗双控新政落地", ["据发改委"])


def test_identity_fallback_still_blocks_pure_assertion():
    assert not _attribution_identity_present("小包装比大包装好卖", ["博主"])
    assert not _attribution_identity_present("女生居家一周四练day1：哑铃练背", ["博主自述"])
