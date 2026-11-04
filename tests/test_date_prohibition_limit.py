"""日期混淆禁令不能误作日期披露义务；语义风险仍需人工核实。"""
from types import SimpleNamespace

import pytest

from agent_system.pipeline import _decide_candidate, _limit_is_prohibition, _mechanical_check


def check(title, limits):
    candidate = {"candidate_id": "x", "headline": title, "claim_ids": ["c1"],
                 "evidence_ids": ["e1"], "highlight_id": "h1"}
    decision = SimpleNamespace(claim_id="c1", decision="limited", required_limits=limits)
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                             evidence_ids={"e1"}, max_chars=40, claim_decisions=[decision])


def codes(result):
    return {error["code"] for error in result["errors"]}


@pytest.mark.parametrize("limit", [
    "须避免混淆活动日期与发布日期",
    "必须避免混淆活动日期与发布日期",
    "需避免混淆活动日期与发布日期",
    "应避免混淆活动日期与发布日期",
    "务必避免混淆活动日期与发布日期",
    "不得混淆活动日期与发布日期",
])
def test_date_prohibition_without_date_does_not_require_date(limit):
    result = check("活动现场记录", [limit])
    assert _limit_is_prohibition(limit)
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert result["limit_pending"]  # 词法不能证明未混淆，保留语义复核


def test_date_in_title_still_requires_semantic_review():
    result = check("活动于2025年举行", ["须避免混淆活动日期与发布日期"])
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert result["limit_pending"]


@pytest.mark.parametrize("limit", [
    "须写明时间范围，避免混淆活动日期与发布日期",
    "须避免混淆活动日期与发布日期，必须注明时间范围",
])
def test_independent_positive_requirement_is_not_suppressed(limit):
    assert not _limit_is_prohibition(limit)
    # 新契约：时间范围义务登记交评审逐条判定，不再由词法判死。
    result = check("活动现场记录", [limit])
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert [r["limit"] for r in result["limit_requirements"]] == [limit]


@pytest.mark.parametrize("title", ["活动现场记录", "活动现场精彩瞬间"])
def test_exact_report_date_distinction_without_title_date(title):
    result = check(title, ["10月28日为报道日期，非事件发生日期"])
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert result["limit_pending"]


@pytest.mark.parametrize("title", ["10月28日活动现场", "活动于昨天举行", "活动于十月举行"])
def test_exact_report_date_distinction_with_date_remains_pending(title):
    result = check(title, ["10月28日为报道日期，非事件发生日期"])
    assert result["limit_pending"]
    decision = _decide_candidate({"candidate_id": "x"}, result, schema_errors=["invalid"], business_policy={})
    assert decision["status"] == "needs_review"
    # 评审不可用 + 有待答限定 → 留给人（新契约 LIMIT_CHECK_UNANSWERED）。
    assert decision["reason"] == "LIMIT_CHECK_UNANSWERED"


@pytest.mark.parametrize("limit", [
    "10月28日为报道日期，非事件发生日期，须写明时间范围",
    "须写明时间范围；10月28日为报道日期，非事件发生日期",
])
def test_date_distinction_with_positive_obligation_not_exempt(limit):
    # 新契约：带正向义务的复合限定登记交评审，不再由词法判死。
    result = check("活动现场记录", [limit])
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert [r["limit"] for r in result["limit_requirements"]] == [limit]


def test_separate_positive_limit_is_not_suppressed():
    result = check("活动现场记录", ["须避免混淆活动日期与发布日期", "须写明时间范围"])
    assert "G1_MISSING_LIMIT" not in codes(result)
    assert len(result["limit_requirements"]) == 2
    assert len(result["limit_pending"]) == 2
