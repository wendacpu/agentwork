"""限定判定契约（2026-11-04 变更）的回归锁。

变更前：词法命中不了的限定（占大头）一律把 G1 压成 needs_review——V 集实测
24 条被覆盖候选里 18 条模型判 pass、标题也确实合规，被这条连坐成零推荐。

变更后：
  1. 机械层只做两件事——登记 limit_requirements；拦「引号被禁词字面命中」。
  2. R01 必须在 limit_checks 里逐条回答（覆盖缺口 → validation_error → 单条重问）。
  3. 裁决跟判定走：violated → revise；unclear/没答 → needs_review；satisfied → 不动。
  4. 评审不可用 + 有待答限定 → LIMIT_CHECK_UNANSWERED 留给人。

四个环各锁一组测试，缺一环就是契约退化。
"""
from __future__ import annotations

from types import SimpleNamespace

from agent_system.pipeline import (
    _decide_candidate,
    _limit_check_coverage_errors,
    _mechanical_check,
    _prohibition_hit,
)


def _decision(claim_id: str = "c1", *limits: str):
    return SimpleNamespace(claim_id=claim_id, decision="limited", required_limits=list(limits),
                           attribution_required=False, required_source_phrase="",
                           verification_required=False)


def _check(headline: str, decisions):
    candidate = {"candidate_id": "a1", "headline": headline, "prefix_labels": [],
                 "claim_ids": [d.claim_id for d in decisions], "evidence_ids": ["e1"],
                 "highlight_id": "h1", "unit_id": "u1", "generation_schema_errors": []}
    return _mechanical_check(candidate, allowed_claims={d.claim_id for d in decisions},
                             allowed_highlights={"h1"}, evidence_ids={"e1"},
                             max_chars=30, allowed_units={"u1"}, claim_decisions=decisions)


def _review(g1_status: str = "pass", limit_checks: list | None = None):
    review = {"candidate_id": "a1",
              "gates": {"G1": {"status": g1_status, "reason": "x", "evidence_ids": [], "rule_ids": []},
                        "G2": {"status": "pass", "reason": "x", "evidence_ids": [], "rule_ids": []},
                        "G3": {"status": "pass", "reason": "x", "evidence_ids": [], "rule_ids": []}},
              "issues": [], "quality": {}}
    if limit_checks is not None:
        review["limit_checks"] = limit_checks
    return review


# --- 环 1：机械层登记 + 禁令字面命中拦截 ---

def test_attribution_limit_is_registered_not_vetoed():
    """归属类限定：登记，不判死。V 集那 18 条误伤的根因锁。"""
    result = _check("博主：钓鱼圈稀奇古怪片段合集", [_decision("c1", "须归属为作者/博主对合集的概括")])
    assert not {e["code"] for e in result["errors"]}
    assert [r["limit"] for r in result["limit_requirements"]] == ["须归属为作者/博主对合集的概括"]


def test_prohibition_with_quoted_hit_is_still_caught():
    """被禁词引号括出且字面命中：词法测得出的违反，保留硬拦。"""
    result = _check("事件已证实", [_decision("c1", "不得写成“已证实”")])
    codes = {e["code"] for e in result["errors"]}
    assert "G1_PROHIBITION_HIT" in codes


def test_prohibition_without_quoted_word_is_not_caught_by_lexical():
    """引号里没写被禁词（「不得推断中毒」）：词法测不出，登记交评审。"""
    result = _check("司机因中毒昏迷", [_decision("c1", "不得推断中毒")])
    codes = {e["code"] for e in result["errors"]}
    assert "G1_PROHIBITION_HIT" not in codes
    assert [r["limit"] for r in result["limit_requirements"]] == ["不得推断中毒"]


def test_prohibition_hit_only_fires_on_quoted_prohibition():
    """非禁令类即使引号词命中也不走 PROHIBITION_HIT（那是归属/标注义务，交评审）。"""
    assert _prohibition_hit("ASR“攻艦步”须标注为转写推断", "运动员做攻艦步") is None
    assert _prohibition_hit("不得写成“已证实”", "事件已证实") == "已证实"


# --- 环 2：覆盖校验（漏答 → validation_error 文案） ---

def test_coverage_error_when_requirements_unanswered():
    mechanical = {"status": "pass", "errors": [],
                  "limit_requirements": [{"claim_id": "c1", "limit": "须归属为博主自述"}]}
    gaps = _limit_check_coverage_errors(_review(limit_checks=[]), mechanical)
    assert gaps and "limit_checks" in gaps[0]


def test_no_coverage_error_when_all_answered():
    mechanical = {"status": "pass", "errors": [],
                  "limit_requirements": [{"claim_id": "c1", "limit": "须归属为博主自述"}]}
    checks = [{"claim_id": "c1", "limit": "须归属为博主自述", "verdict": "satisfied", "reason": "博主：前缀已归属"}]
    assert _limit_check_coverage_errors(_review(limit_checks=checks), mechanical) == []


def test_no_coverage_error_when_no_requirements():
    assert _limit_check_coverage_errors(_review(), {"status": "pass", "errors": []}) == []


# --- 环 3：裁决跟判定走 ---

def _mechanical_with_requirements(*limits):
    return {"status": "pass", "errors": [], "policy_hits": [],
            "limit_requirements": [{"claim_id": "c1", "limit": x} for x in limits]}


def test_satisfied_verdict_leaves_model_g1_untouched():
    mechanical = _mechanical_with_requirements("须归属为博主自述")
    review = _review(g1_status="pass", limit_checks=[
        {"claim_id": "c1", "limit": "须归属为博主自述", "verdict": "satisfied", "reason": "博主：前缀已归属"}])
    decision = _decide_candidate(review, mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "pass"


def test_violated_verdict_forces_g1_revise():
    mechanical = _mechanical_with_requirements("不得写成“已证实”")
    review = _review(g1_status="pass", limit_checks=[
        {"claim_id": "c1", "limit": "不得写成“已证实”", "verdict": "violated", "reason": "标题把说法写成了确定事实"}])
    decision = _decide_candidate(review, mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "revise"


def test_conflicting_duplicate_verdicts_never_auto_pass():
    mechanical = _mechanical_with_requirements("须注明未经证实")
    checks = [{"claim_id": "c1", "limit": "须注明未经证实", "verdict": v, "reason": "r"}
              for v in ("violated", "satisfied", "satisfied")]
    decision = _decide_candidate(_review(g1_status="pass", limit_checks=checks), mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "needs_review"


def test_violated_limit_does_not_weaken_existing_reject():
    mechanical = _mechanical_with_requirements("须注明未经证实")
    review = _review(g1_status="reject", limit_checks=[
        {"claim_id": "c1", "limit": "须注明未经证实", "verdict": "violated", "reason": "r"}])
    decision = _decide_candidate(review, mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "reject"


def test_violated_and_unclear_preserve_both_and_require_review():
    mechanical = _mechanical_with_requirements("限定一", "限定二")
    checks = [{"claim_id": "c1", "limit": limit, "verdict": verdict, "reason": "r"}
              for limit, verdict in (("限定一", "violated"), ("限定二", "unclear"))]
    policy = {"status": "approved", "rules": [{"rule_id": "P1"}]}
    decision = _decide_candidate(_review(limit_checks=checks), mechanical, schema_errors=[], business_policy=policy)
    assert decision["status"] == "needs_review"
    assert decision["gates"]["G1"]["status"] == "needs_review"
    assert [(x["limit"], x["verdict"]) for x in decision["gates"]["G1"]["limit_checks"]] == [
        ("限定一", "violated"), ("限定二", "unclear")]


def test_limit_results_preserve_existing_gate_status_and_reason():
    mechanical = _mechanical_with_requirements("限定一", "限定二")
    checks = [{"claim_id": "c1", "limit": limit, "verdict": verdict, "reason": "r"}
              for limit, verdict in (("限定一", "violated"), ("限定二", "unclear"))]
    for status in ("reject", "needs_review"):
        decision = _decide_candidate(_review(g1_status=status, limit_checks=checks), mechanical,
                                     schema_errors=[], business_policy={})
        gate = decision["gates"]["G1"]
        assert (gate["status"], gate["reason"]) == (status, "x")
        assert {x["verdict"] for x in gate["limit_checks"]} == {"violated", "unclear"}


def test_duplicate_verdict_conflicts_are_order_independent():
    from itertools import permutations
    from agent_system.pipeline import _limit_check_verdicts

    for verdicts in (("satisfied", "violated", "violated"),
                     ("satisfied", "unclear", "satisfied"),
                     ("violated", "unclear", "violated")):
        for ordered in permutations(verdicts):
            checks = [{"claim_id": "c1", "limit": "限定一", "verdict": v} for v in ordered]
            assert _limit_check_verdicts(_review(limit_checks=checks)) == {("c1", "限定一"): "unclear"}
    for verdict in ("satisfied", "violated", "unclear"):
        checks = [{"claim_id": "c1", "limit": "限定一", "verdict": verdict}] * 2
        assert _limit_check_verdicts(_review(limit_checks=checks)) == {("c1", "限定一"): verdict}


def test_unclear_verdict_downgrades_g1_to_needs_review():
    mechanical = _mechanical_with_requirements("须注明未经证实")
    review = _review(g1_status="pass", limit_checks=[
        {"claim_id": "c1", "limit": "须注明未经证实", "verdict": "unclear", "reason": "标题语气无法确定"}])
    decision = _decide_candidate(review, mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "needs_review"


def test_missing_answer_treated_as_unclear():
    mechanical = _mechanical_with_requirements("须注明未经证实")
    decision = _decide_candidate(_review(g1_status="pass", limit_checks=[]), mechanical, schema_errors=[], business_policy={})
    assert decision["gates"]["G1"]["status"] == "needs_review"


# --- 环 4：评审不可用时的兜底 ---

def test_review_unavailable_with_requirements_goes_to_human():
    mechanical = _mechanical_with_requirements("须归属为博主自述")
    decision = _decide_candidate(_review(), mechanical, schema_errors=["invalid"], business_policy={})
    assert decision["status"] == "needs_review"
    assert decision["reason"] == "LIMIT_CHECK_UNANSWERED"


def test_review_unavailable_without_requirements_stays_review_unavailable():
    mechanical = {"status": "pass", "errors": [], "policy_hits": []}
    decision = _decide_candidate(_review(), mechanical, schema_errors=["invalid"], business_policy={})
    assert decision["status"] == "needs_review"
    assert decision["reason"] == "REVIEW_UNAVAILABLE"
