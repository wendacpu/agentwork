"""新鲜评审的候选级对照和摘录来源核验；仅内存测试。"""
from copy import deepcopy

from agent_system.pipeline import Pipeline
from test_safety_contracts import _review_payload, _video


def setup():
    candidate = {"candidate_id": "c1", "headline": "小店开业", "claim_ids": []}
    review = _review_payload("c1")
    review["gates"]["G3"].update(delivery_supported=True, core_covered=True, promise_comparison={
        "title_promise": "店开业", "trigger_span": "小店开业", "video_delivery": "小店开业",
        "evidence_excerpt": "小店开业", "evidence_answered_question": "店发生了什么",
        "alignment": "matched", "explanation": "标题和证据一致",
    })
    state = {"business_policy": {"status": "approved", "rules": [{"rule_id": "P1", "action": "reject"}]},
             "understanding": {"claims": []}, "risk_signals": {}}
    checks = [{"candidate_id": "c1", "status": "pass", "errors": []}]
    return candidate, review, state, checks


def run(review, state, candidate, checks, retry=None, task_id="fresh"):
    p = Pipeline.__new__(Pipeline)
    calls = []
    def attempt(*args):
        calls.append(args[-1])
        return deepcopy(retry)
    p._retry_single_review = attempt
    reviews, decisions = p._validate_and_decide_reviews(_video("source_only"), state, [candidate], [review], checks, task_id)
    return reviews[0], decisions[0], calls


def test_verified_excerpt_passes_without_retry():
    c, r, s, checks = setup()
    review, decision, calls = run(r, s, c, checks)
    assert decision["status"] == "ready_for_human_review"
    assert review["comparison_errors"] == [] and not calls


def test_fabricated_excerpt_retries_only_candidate_and_recovers():
    c, r, s, checks = setup()
    valid = deepcopy(r)
    r["gates"]["G3"]["promise_comparison"]["evidence_excerpt"] = "不存在的原文"
    review, decision, calls = run(r, s, c, checks, valid)
    assert len(calls) == 1 and "evidence_excerpt" in calls[0][0]
    assert decision["status"] == "ready_for_human_review"
    assert not review["comparison_errors"]


def test_invalid_comparison_is_pending_not_mechanical_rewrite():
    c, r, s, checks = setup()
    r["gates"]["G3"]["promise_comparison"]["evidence_excerpt"] = ""
    checks[0].update(status="fail", errors=[{"code": "G4_LENGTH_EXCEEDED", "repairable": True}])
    review, decision, calls = run(r, s, c, checks)
    assert len(calls) == 1 and review["comparison_errors"]
    assert decision["status"] == "needs_review"
    assert decision["gates"]["G3"]["status"] == "needs_review"


def test_explicit_policy_rejection_is_not_retried_away():
    c, r, s, checks = setup()
    r["gates"]["G3"].pop("promise_comparison")
    r["gates"]["G2"].update(status="reject", rule_ids=["P1"])
    _, decision, calls = run(r, s, c, checks)
    assert decision["status"] == "reject" and not calls


def test_uncertain_does_not_pass_and_old_direct_call_remains_compatible():
    c, r, s, checks = setup()
    r["gates"]["G3"]["promise_comparison"]["alignment"] = "uncertain"
    _, decision, _ = run(deepcopy(r), s, c, checks)
    assert decision["status"] == "needs_review"
    r["gates"]["G3"].pop("promise_comparison")
    _, decision, calls = run(r, s, c, checks, task_id=None)
    assert decision["status"] == "ready_for_human_review" and not calls


def test_excerpt_must_match_cited_evidence_not_any_valid_id():
    c, r, s, checks = setup()
    r["gates"]["G3"]["evidence_ids"] = ["desc"]
    review, decision, _ = run(r, s, c, checks)
    assert decision["status"] == "needs_review"
    assert any("evidence_excerpt" in error for error in review["comparison_errors"])


def test_attribution_flag_does_not_erase_g1_unresolved():
    c, r, s, checks = setup()
    r['gates']['G1']['status'] = 'needs_review'
    r['gates']['G1']['reason'] = '来源未核实'
    checks[0]['programmatic_g1_flags'] = [{'code': 'G1_SOURCE_ATTRIBUTION_MISSING'}]
    _, decision, _ = run(r, s, c, checks)
    assert decision['status'] == 'needs_review'
    assert decision['gates']['G1']['reason'] == '来源未核实'


def test_missing_boolean_or_prefix_span_are_handled():
    c, r, s, checks = setup()
    c['rendered_title'] = '【知识科普】小店开业'
    r['gates']['G3']['promise_comparison']['trigger_span'] = '【知识科普】小店开业'
    _, decision, calls = run(deepcopy(r), s, c, checks)
    assert decision['status'] == 'ready_for_human_review' and not calls
    del r['gates']['G3']['core_covered']
    review, decision, calls = run(r, s, c, checks)
    assert decision['status'] == 'needs_review' and calls
    assert any('core_covered' in e for e in review['comparison_errors'])


def test_only_invalid_candidate_is_retried():
    c, r, s, checks = setup()
    c2, r2 = deepcopy(c), deepcopy(r)
    c2["candidate_id"] = r2["candidate_id"] = "c2"
    r2["gates"]["G3"]["promise_comparison"]["trigger_span"] = "不在标题里"
    calls = []
    p = Pipeline.__new__(Pipeline)
    p._retry_single_review = lambda task, video, state, candidate, check, errors: calls.append(candidate["candidate_id"])
    _, decisions = p._validate_and_decide_reviews(_video("source_only"), s, [c, c2], [r, r2], checks + [{"candidate_id": "c2", "status": "pass", "errors": []}], "fresh")
    assert calls == ["c2"]
    assert [d["status"] for d in decisions] == ["ready_for_human_review", "needs_review"]
