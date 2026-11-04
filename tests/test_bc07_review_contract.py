"""BC07 相关的确定性路由测试；不声称替代模型对采访的语义理解。"""
from copy import deepcopy

import pytest

from agent_system.pipeline import Pipeline
from test_safety_contracts import _review_payload, _video


def decide(g3, title="受访者称自己仍不如对手"):
    video = _video("source_only")
    candidate = {"candidate_id": "c1", "headline": title, "claim_ids": []}
    review = _review_payload("c1")
    review["gates"]["G3"].update(g3)
    state = {"business_policy": {"status": "approved", "rules": [{"rule_id": "P1"}]},
             "understanding": {"claims": []}, "risk_signals": {}}
    checks = [{"candidate_id": "c1", "status": "pass", "errors": []}]
    reviews, decisions = Pipeline.__new__(Pipeline)._validate_and_decide_reviews(
        video, state, [candidate], [deepcopy(review)], checks)
    return reviews[0], decisions[0]


@pytest.mark.parametrize("title", ["受访者称自己仍不如对手", "冠军为何是传奇"])
def test_false_delivery_cannot_be_approved_even_with_good_scores(title):
    review, decision = decide({"status": "pass", "delivery_supported": False, "core_covered": True}, title)
    assert decision["status"] == "rewrite"
    assert decision["gates"]["G3"]["status"] == "revise"
    issue = next(i for i in review["issues"] if i["issue_code"] == "G3_NOT_DELIVERED")
    assert issue["rollback_to"] == "N09"


def test_core_omission_and_delivery_are_separate():
    review, decision = decide({"status": "pass", "delivery_supported": False, "core_covered": False})
    assert decision["status"] == "rewrite"
    assert {i["issue_code"]: i["rollback_to"] for i in review["issues"]} == {
        "G3_NOT_DELIVERED": "N09", "G3_CORE_POINT_OMITTED": "N07"}


def test_revise_with_missing_issue_still_records_core_omission():
    """覆盖不全但承诺兑现（delivery_supported=True）的 G3 revise：记录核心遗漏，交人工。

    2026-11-04 契约更新（V 集人工评审背书）：这种形态 3/3 被人工判 approved——
    「列了菜名没写可爱日常」「写了主要步骤没写全部步骤」是覆盖度问题，人工认为
    主看点抓住即可。自动重写每条烧 2 次调用，改判 needs_review。记录义务不变：
    核心遗漏的 issue 与 rollback_to 必须照写，人工看得见再决定改不改。
    """
    review, decision = decide({"status": "revise", "delivery_supported": True, "core_covered": False})
    assert decision["status"] == "needs_review"
    assert decision["reason"] == "G3_REVISION_REQUIRES_HUMAN_CALL"
    assert any(i["issue_code"] == "G3_CORE_POINT_OMITTED" and i["rollback_to"] == "N07" for i in review["issues"])


@pytest.mark.parametrize("status", ["needs_review", "reject"])
def test_stricter_semantic_status_is_not_weakened(status):
    review, decision = decide({"status": status, "delivery_supported": False, "core_covered": True})
    assert review["gates"]["G3"]["status"] == status
    assert decision["status"] != "ready_for_human_review"


def test_explicit_mismatch_cannot_be_overridden_by_pass():
    review, decision = decide({"status": "pass", "delivery_supported": True, "core_covered": True,
                               "promise_comparison": {"alignment": "mismatched"}})
    assert decision["status"] == "rewrite"
    assert review["gates"]["G3"]["promise_comparison"]["alignment"] == "mismatched"


def test_uncertain_comparison_routes_to_human():
    _, decision = decide({"status": "pass", "delivery_supported": True, "core_covered": True,
                          "promise_comparison": {"alignment": "uncertain"}})
    assert decision["status"] == "needs_review"


@pytest.mark.parametrize("title", ["受访者称自己仍不如对手", "冠军为何是传奇"])
def test_positive_and_question_titles_are_not_blocked_by_wording(title):
    _, decision = decide({"status": "pass", "delivery_supported": True, "core_covered": True}, title)
    assert decision["status"] == "ready_for_human_review"
