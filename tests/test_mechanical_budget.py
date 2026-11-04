"""机械修复与语义重写共用父链预算的确定性测试。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from agent_system.pipeline import Pipeline, _compressible_defects, _rewrite_depth


def candidate(cid, parent=None):
    return {"candidate_id": cid, "parent_candidate_id": parent, "rendered_title": cid,
            "mechanical_checks": {"errors": [{"code": "G4_LENGTH_EXCEEDED"}]}}


def decision(cid, status="rewrite"):
    return {"candidate_id": cid, "status": status, "reason": "UNRESOLVED",
            "gates": {g: {"status": "pass"} for g in ("G1", "G2", "G3")}}


class MechanicalBudgetTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = Pipeline.__new__(Pipeline)
        self.pipeline.config = SimpleNamespace(max_rewrite_rounds=2, max_mechanical_repairs=1, max_rewrite_candidates=3)
        self.pipeline.storage = SimpleNamespace(event=Mock())

    def test_chain_depth_fails_closed(self):
        root = candidate("a")
        child = candidate("b", "a")
        self.assertEqual(_rewrite_depth(child, [root, child]), 1)
        self.assertEqual(_rewrite_depth(candidate("c", "missing"), [root]), 2)
        self.assertEqual(_rewrite_depth(candidate("c", "c"), [candidate("c", "c")]), 2)

    def test_pending_overlength_is_compressible_but_revise_is_not(self):
        d = decision("a", "needs_review")
        d["gates"]["G1"]["status"] = "needs_review"
        self.assertTrue(_compressible_defects(candidate("a"), d, {}))
        d["gates"]["G2"]["status"] = "revise"
        self.assertFalse(_compressible_defects(candidate("a"), d, {}))

    def test_second_attempt_follows_overlong_child_and_preserves_pending(self):
        root = candidate("a")
        initial = {"candidates": [root], "decisions": [decision("a", "needs_review")],
                   "reviews": [], "route": {"action": "needs_review", "reason": "pending"}}
        calls = []

        def generate(*args, **kwargs):
            source = args[7]
            calls.append(source["candidate_id"])
            child = candidate(source["candidate_id"] + "x", source["candidate_id"])
            if len(calls) == 2:
                child["mechanical_checks"]["errors"] = []
            d = decision(child["candidate_id"], "ready_for_human_review")
            return {"candidates": [child], "reviews": [], "decisions": [d], "route": {"action": "handoff"}}

        self.pipeline._generate_and_review = generate
        result = self.pipeline._repair_mechanical_pass("t", {}, None, None, None, [], initial, 0)
        self.assertEqual(calls, ["a", "ax"])
        self.assertEqual([d["status"] for d in result["decisions"]], ["needs_review"] * 3)
        self.assertEqual(result["route"]["action"], "needs_review")

    def test_configured_budget_one_stops_after_one_parent_link(self):
        self.pipeline.config.max_rewrite_rounds = 1
        initial = {"candidates": [candidate("a")], "decisions": [decision("a")], "reviews": [],
                   "route": {"action": "rewrite"}}
        child = candidate("b", "a")
        self.pipeline._generate_and_review = Mock(return_value={"candidates": [child], "reviews": [],
            "decisions": [decision("b")], "route": {"action": "rewrite"}})
        result = self.pipeline._repair_mechanical_pass("t", {}, None, None, None, [], initial, 0)
        self.assertEqual(self.pipeline._generate_and_review.call_count, 1)
        self.assertNotEqual(result["route"]["action"], "rewrite")

    def test_child_route_risk_not_discarded(self):
        initial = {"candidates": [candidate("a")], "decisions": [decision("a")], "reviews": [],
                   "route": {"action": "handoff"}}
        child = candidate("b", "a")
        child["mechanical_checks"]["errors"] = []
        self.pipeline._generate_and_review = Mock(return_value={"candidates": [child], "reviews": [],
            "decisions": [decision("b", "ready_for_human_review")],
            "route": {"action": "needs_review", "reason": "INPUT_LOW_CONFIDENCE"}})
        result = self.pipeline._repair_mechanical_pass("t", {}, None, None, None, [], initial, 0)
        self.assertEqual(result["route"]["reason"], "INPUT_LOW_CONFIDENCE")
        self.assertEqual(result["route"]["action"], "needs_review")


if __name__ == "__main__":
    unittest.main()
