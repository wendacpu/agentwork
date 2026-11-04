"""引号锚点：被限制的原文进了标题就算落实，没进标题的标注义务不成立。

真实误杀（2026-11-03 回放 93 条历史候选时确认）：
- 「需保留“约三分钟”限定」判否了标题已写「约三分钟」的候选；
- 「ASR“攻艦步”…须标注为转写推断」判否了标题根本没提这个动作的候选；
- 「只能写加蒜末与油，不写精确勺数」判否了标题没写任何勺数的候选。
"""
from types import SimpleNamespace

import pytest

from agent_system.pipeline import _limit_is_expressed, _mechanical_check


def _decision(*limits):
    return SimpleNamespace(claim_id="c1", decision="limited", required_limits=list(limits),
                           attribution_required=False, required_source_phrase="",
                           verification_required=False)


def _errors(headline, *limits):
    candidate = {"candidate_id": "a1", "headline": headline, "prefix_labels": [],
                 "claim_ids": ["c1"], "evidence_ids": ["e1"], "highlight_id": "h1", "unit_id": "u1"}
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                             evidence_ids={"e1"}, max_chars=40, allowed_units={"u1"},
                             claim_decisions=[_decision(*limits)])["errors"]


@pytest.mark.parametrize("limit,headline", [
    ('ASR 为“盖上名三分钟”，按上下文理解为盖盖焖三分钟，需保留“约三分钟”限定，不当作实测烹饪时长',
     "博主演示白菜不爆炒，盖盖焖约三分钟"),
    ('ASR“攻艦步”按弓箭步理解须标注为转写推断', "博主称跑前热身每个动作做多少次"),
    ('ASR 为“通常两勺油淨蒜末”，用词含混，只能写加蒜末与油，不写精确勺数',
     "博主称白菜锁死这吃法，加蒜末生抽焖三分钟"),
])
def test_quoted_limit_no_longer_kills_compliant_titles(limit, headline):
    assert _limit_is_expressed(limit, headline) is True
    assert "G1_MISSING_LIMIT" not in {e["code"] for e in _errors(headline, limit)}


def test_retain_limit_still_requires_the_exact_original():
    """要求保留「约三分钟」而标题只写「三分钟」：仍然是缺。"""
    assert _limit_is_expressed("须保留“约三分钟”", "博主演示盖盖焖三分钟") is False


def test_annotation_limit_still_blocks_when_subject_is_present():
    """使用原词不等于完成转写推断标注，必须进入待核。"""
    assert _pending("运动员做攻艦步", "ASR“攻艦步”须标注为转写推断")


def test_limits_without_quotes_keep_previous_behaviour():
    """无引号时行为不变；词法认不出的仍由待核兜底，不当通过。"""
    assert _limit_is_expressed("须标明为传闻而非结论", "某商场发生枪击") is False
    # 年份写错这条，词法层只看到「年」就认了——这是已知不足，靠待核兜住。
    assert _limit_is_expressed("须写明时间范围为2024年", "2025年全行业暴涨") is True
    candidate = {"candidate_id": "a1", "headline": "2025年全行业暴涨", "prefix_labels": [],
                 "claim_ids": ["c1"], "evidence_ids": ["e1"], "highlight_id": "h1", "unit_id": "u1"}
    result = _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                               evidence_ids={"e1"}, max_chars=40, allowed_units={"u1"},
                               claim_decisions=[_decision("须写明时间范围为2024年")])
    assert result["limit_pending"], "词法认不出时必须留待核，不能直接当通过"


def _pending(headline, *limits):
    candidate = {"candidate_id": "a1", "headline": headline, "prefix_labels": [],
                 "claim_ids": ["c1"], "evidence_ids": ["e1"], "highlight_id": "h1", "unit_id": "u1"}
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                             evidence_ids={"e1"}, max_chars=40, allowed_units={"u1"},
                             claim_decisions=[_decision(*limits)])["limit_pending"]


def test_quoted_text_already_in_headline_still_needs_semantic_review():
    """命中部分原文不能证明同一限定中的其他约束得到满足。"""
    assert _pending("博主演示盖盖焖约三分钟", "须保留“约三分钟”限定")


def test_limit_without_any_anchor_stays_pending():
    """没有落点的限定机械层判不了，必须留在待核队列里，不能悄悄算通过。"""
    assert _pending("体测跑前热身动作示范", "动作名称存在转写不确定性，不得写成确定动作名")


def test_pending_reason_is_groupable():
    """待核原因必须能分组，评审台才能批量处理同类。"""
    from agent_system.pipeline import _limit_pending_reason

    assert _limit_pending_reason("须保留“田间南北走向”这一限定", "路的各种古称").startswith("标题未出现限定要求的落点")
    assert _limit_pending_reason("须归属为博主说法", "某地暴雨").startswith("归属类限定")
    assert _limit_pending_reason("不得写成确定动作名", "某地暴雨").startswith("禁止类限定")
    assert _limit_pending_reason("动作名称存在转写不确定性", "某地暴雨").startswith("限定没有任何可核对落点")


def test_review_console_shows_pending_reason():
    """评审台只给一个「待核」标签等于让人重读状态，必须带上原因。"""
    from agent_system.batch_review import _blocking_reasons

    reasons = _blocking_reasons({"mechanical_checks": {"errors": [], "limit_pending": [
        {"claim_id": "c1", "limit": "须保留“约三分钟”", "reason": "标题未出现限定要求的落点：约三分钟"}]}})
    assert len(reasons) == 1
    assert reasons[0]["code"] == "G1_LIMIT_PENDING"
    assert "约三分钟" in reasons[0]["text"] and "待核" in reasons[0]["text"]
