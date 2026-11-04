"""混合缺陷的压缩通道：待核继承、风险收敛与共享预算。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_system.pipeline import Pipeline, _compressible_defects, _pending_length_semantic_defects, _rewrite_depth


def candidate(cid, parent=None, codes=("G4_LENGTH_EXCEEDED", "G1_MISSING_LIMIT")):
    return {"candidate_id": cid, "parent_candidate_id": parent, "rendered_title": cid,
            "mechanical_checks": {"errors": [{"code": code, "repairable": True} for code in codes]}}


def decision(cid, status="needs_review", g1="revise"):
    return {"candidate_id": cid, "status": status, "reason": "LIMIT_SEMANTICS_UNVERIFIED",
            "issues": [{"issue_code": "G1_MISSING_LIMIT"}],
            "gates": {"G1": {"status": g1}, "G2": {"status": "pass"}, "G3": {"status": "pass"}}}


def pipeline():
    p = Pipeline.__new__(Pipeline)
    p.config = SimpleNamespace(max_rewrite_rounds=2, max_mechanical_repairs=1, max_rewrite_candidates=3)
    p.storage = SimpleNamespace(event=Mock())
    return p


def initial(c, d, route=None):
    return {"candidates": [c], "decisions": [d], "reviews": [],
            "route": route or {"action": "needs_review", "reason": "pending"}}


@pytest.mark.parametrize("status", ["needs_review", "rewrite"])
def test_mixed_candidate_is_eligible(status):
    assert _compressible_defects(candidate("a"), decision("a", status), {})


@pytest.mark.parametrize("codes", [
    ("G1_MISSING_LIMIT",),
    ("G4_LENGTH_EXCEEDED", "G1_MISSING_DISPUTE_MARK"),
    ("G4_LENGTH_EXCEEDED", "G1_MISSING_LIMIT", "G4_INVALID_REFERENCE"),
])
def test_other_defects_cannot_enter(codes):
    assert not _compressible_defects(candidate("a", codes=codes), decision("a"), {})


def test_policy_and_other_gate_cannot_be_bypassed():
    c, d = candidate("a"), decision("a")
    d["policy_action"] = "reject"
    assert not _compressible_defects(c, d, {})
    d.pop("policy_action")
    d["gates"]["G2"]["status"] = "revise"
    assert not _compressible_defects(c, d, {})
    d["gates"]["G2"]["status"] = "pass"
    c["mechanical_checks"]["policy_hits"] = [{"rule_id": "P9"}]
    assert not _compressible_defects(c, d, {})


def test_pending_length_and_semantic_revision_produces_pending_draft():
    p = pipeline()
    parent = candidate("a", codes=("G4_LENGTH_EXCEEDED",))
    dec = decision("a", g1="needs_review")
    dec["gates"]["G3"]["status"] = "revise"
    assert _pending_length_semantic_defects(parent, dec)
    child = candidate("b", "a", ())
    p._generate_and_review = Mock(return_value={"candidates": [child], "reviews": [],
        "decisions": [decision("b", "ready_for_human_review", "pass")],
        "route": {"action": "handoff"}})
    result = p._repair_mechanical_pass("t", {}, None, None, None, [], initial(parent, dec), 0)
    assert p._generate_and_review.call_count == 1
    assert result["decisions"][-1]["status"] == "needs_review"
    assert result["route"]["action"] == "needs_review"


@pytest.mark.parametrize("extra", ["G4_INVALID_REFERENCE", "G4_NUMERIC_CONFLICT"])
def test_pending_semantic_draft_does_not_bypass_other_mechanical_errors(extra):
    c = candidate("a", codes=("G4_LENGTH_EXCEEDED", extra))
    d = decision("a", g1="needs_review")
    d["gates"]["G3"]["status"] = "revise"
    assert not _pending_length_semantic_defects(c, d)


def test_pending_semantic_draft_does_not_bypass_policy():
    c, d = candidate("a", codes=("G4_LENGTH_EXCEEDED",)), decision("a")
    d["gates"]["G3"]["status"] = "revise"
    for policy in ("reject", "review", "rewrite"):
        d["policy_action"] = policy
        assert not _pending_length_semantic_defects(c, d)


def test_two_rounds_follow_children_and_keep_pending():
    p, state, calls = pipeline(), {}, []
    def generate(*args, **kwargs):
        source = args[7]
        calls.append((source["candidate_id"], args[6], kwargs["repair_mode"]))
        child = candidate(source["candidate_id"] + "x", source["candidate_id"],
                          ("G4_LENGTH_EXCEEDED", "G1_MISSING_LIMIT") if len(calls) == 1 else ())
        return {"candidates": [child], "reviews": [],
                "decisions": [decision(child["candidate_id"], "ready_for_human_review", "pass")],
                "route": {"action": "handoff"}}
    p._generate_and_review = generate
    result = p._repair_mechanical_pass("t", state, None, None, None, [], initial(candidate("a"), decision("a")), 0)
    assert calls == [("a", -1, "压缩"), ("ax", -2, "压缩")]
    assert [d["status"] for d in result["decisions"]] == ["needs_review"] * 3
    assert result["route"]["action"] == "needs_review"
    p._repair_mechanical_pass("t", state, None, None, None, [], result, 0)
    assert len(calls) == 2


def test_subroute_risk_and_rejected_child_are_retained():
    p = pipeline()
    child = candidate("b", "a", ())
    p._generate_and_review = Mock(return_value={"candidates": [child], "reviews": [],
        "decisions": [decision("b", "reject", "pass")],
        "route": {"action": "reject", "reason": "POLICY_ACTION_REJECT", "pending_signal_ids": ["risk"]}})
    result = p._repair_mechanical_pass("t", {}, None, None, None, [],
                                       initial(candidate("a"), decision("a"), {"action": "handoff"}), 0)
    assert result["decisions"][-1]["status"] == "reject"
    assert result["route"]["action"] == "needs_review"
    assert result["route"]["pending_signal_ids"] == ["risk"]


def test_existing_parent_chain_counts_toward_budget():
    p = pipeline()
    root, child = candidate("a"), candidate("b", "a")
    data = initial(child, decision("b"))
    data["candidates"].insert(0, root)
    def generate(*args, **kwargs):
        grandchild = candidate("c", "b")
        return {"candidates": [grandchild], "reviews": [], "decisions": [decision("c")], "route": {"action": "needs_review", "reason": "core_fact_or_business_policy_requires_human_verification"}}
    p._generate_and_review = Mock(side_effect=generate)
    p._repair_mechanical_pass("t", {}, None, None, None, [], data, 0)
    assert p._generate_and_review.call_count == 1


def test_duplicate_retries_once_and_uses_shared_budget():
    p, state, calls = pipeline(), {}, []
    def generate(*args, **kwargs):
        calls.append(args[6])
        if len(calls) == 1:
            state.setdefault("rewrite_duplicates", []).append({"parent": "a", "round": -1})
            return {"candidates": [], "reviews": [], "decisions": [],
                    "route": {"action": "stop", "reason": "REWRITE_NO_NEW_TITLE"}}
        child = candidate("b", "a")
        return {"candidates": [child], "reviews": [],
                "decisions": [decision("b", "ready_for_human_review", "pass")],
                "route": {"action": "handoff"}}
    p._generate_and_review = generate
    result = p._repair_mechanical_pass("t", state, None, None, None, [], initial(candidate("a"), decision("a")), 0)
    assert calls == [-1, -2]
    assert state["mechanical_repair_attempts"] == [{"parent": "a", "round": 1}, {"parent": "a", "round": 2}]
    assert state["rewrite_duplicates"]
    assert _rewrite_depth(result["candidates"][-1], result["candidates"]) == 2
    assert result["decisions"][-1]["status"] == "needs_review"
    assert result["route"]["action"] == "needs_review"
    p._repair_mechanical_pass("t", state, None, None, None, [], result, 0)
    assert calls == [-1, -2]


@pytest.mark.parametrize("reason,limit,expected", [("REWRITE_NO_NEW_TITLE", 1, 1), ("GENERATION_SCHEMA_INVALID_OR_NO_CANDIDATES", 2, 1), ("REWRITE_NO_NEW_TITLE", 2, 2)])
def test_empty_rewrite_retry_is_strictly_bounded(reason, limit, expected):
    p, state = pipeline(), {}
    p.config.max_rewrite_rounds = limit
    p._generate_and_review = Mock(return_value={"candidates": [], "reviews": [], "decisions": [],
                                               "route": {"action": "stop", "reason": reason}})
    result = p._repair_mechanical_pass("t", state, None, None, None, [], initial(candidate("a"), decision("a")), 0)
    p._repair_mechanical_pass("t", state, None, None, None, [], result, 0)
    assert p._generate_and_review.call_count == expected
