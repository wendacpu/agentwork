import copy
import json
import unittest

from agent_system.evidence_audit import audit_evidence
from agent_system.types import (BoundarySet, Claim, ClaimDecision, EventNode, EventTurn,
                                Evidence, SemanticUnit, Understanding, UnitDecision, VideoInput)


def fixtures():
    video = VideoInput("v", "t", "a", None, "", {}, "", None, 10.0, {}, "", "", [], [], [
        Evidence("a", "asr", "x", "asr:0-10"),
        Evidence("o", "ocr", "x", "ocr:5s"),
        Evidence("d", "description", "x", "description"),
    ], {}, {})
    understanding = Understanding("", [], [Claim("c", "x", "fact", ["a"])], [], [],
                                  semantic_units=[SemanticUnit("u", "x", ["c"], ["o"])],
                                  events=[EventNode("e", "x", "x", evidence_ids=["a"])],
                                  turns=[EventTurn("e", "e")])
    decision = ClaimDecision("c", None, "usable", ["a"], [], [], [], "", source_evidence_refs=["d"])
    boundary = BoundarySet([decision], unit_decisions=[UnitDecision("u", "usable")])
    return understanding, video, boundary


class AuditTests(unittest.TestCase):
    def test_clean_is_read_only_serializable_and_not_truth(self):
        inputs = fixtures()
        original = copy.deepcopy(inputs)
        result = audit_evidence(*inputs)
        self.assertEqual(inputs, original)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["semantic_truth"], "not_evaluated")
        self.assertEqual([e["category"] for e in result["evidence"]], ["primary_text", "primary_text", "context"])
        json.dumps(result, allow_nan=False)

    def test_references_ids_and_object_lookup(self):
        u, v, b = fixtures()
        u.claims.append(Claim("c", "", "", ["", "missing"]))
        u.semantic_units[0].claim_ids = []
        u.events[0].event_id = ""
        u.turns[0].to_event = "missing"
        b.decisions[0].claim_id = "missing"
        b.decisions[0].source_evidence_refs = ["missing"]
        b.unit_decisions[0].unit_id = ""
        result = audit_evidence(u, v, b)
        codes = {i["code"] for i in result["issues"]}
        self.assertTrue({"duplicate_id", "empty_id", "empty_ref", "empty_refs", "dangling_ref"} <= codes)
        for path in ("understanding.turns[0]", "boundary.decisions[0]", "boundary.unit_decisions[0]"):
            self.assertTrue(all(result["issues"][i]["path"] == path for i in result["by_object"][path]))

    def test_location_unknown_invalid_and_duration(self):
        u, v, _ = fixtures()
        v.evidence = [Evidence("a", "asr", "", "asr:None-None"),
                      Evidence("b", "ocr", "", None),
                      Evidence("c", "asr", "", "asr:5-4"),
                      Evidence("d", "ocr", "", "ocr:11s"),
                      Evidence("e", "asr", "", "asr:-1-2"),
                      Evidence("f", "ocr", "", "ocr:nan"),
                      Evidence("g", "other", "", "")]
        result = audit_evidence(u, v)
        self.assertEqual([e["location"] for e in result["evidence"]],
                         ["unknown", "unknown", "invalid", "invalid", "invalid", "invalid", "not_applicable"])
        self.assertEqual(result["evidence"][-1]["category"], "unknown")
        v.duration = None
        self.assertEqual(audit_evidence(u, v)["evidence"][3]["location"], "valid")

    def test_ambiguous_targets_and_duplicate_boundary_decisions(self):
        u, v, b = fixtures()
        v.evidence.append(Evidence("a", "asr", "x", "asr:0-1"))
        u.claims.append(Claim("c", "x", "fact", ["a"]))
        u.semantic_units.append(SemanticUnit("u", "x", ["c"], ["a"]))
        u.events.append(EventNode("e", "x", "x", evidence_ids=["a"]))
        b.decisions.append(copy.deepcopy(b.decisions[0]))
        b.unit_decisions.append(copy.deepcopy(b.unit_decisions[0]))
        issues = audit_evidence(u, v, b)["issues"]
        ambiguous = {(x["path"], x["field"]) for x in issues if x["code"] == "ambiguous_ref"}
        self.assertTrue({("understanding.claims[0]", "evidence_refs"),
                         ("understanding.semantic_units[0]", "claim_ids"),
                         ("understanding.turns[0]", "from_event"),
                         ("boundary.decisions[0]", "claim_id"),
                         ("boundary.unit_decisions[0]", "unit_id")} <= ambiguous)
        duplicates = {x["path"] for x in issues if x["code"] == "duplicate_id"}
        self.assertTrue({"boundary.decisions[1]", "boundary.unit_decisions[1]"} <= duplicates)

    def test_invalid_ids_refs_and_duplicate_refs(self):
        u, v, b = fixtures()
        v.evidence.append(Evidence(" a ", "asr", "x", "asr:0-1"))
        u.claims[0].evidence_refs = ["a", "a", " a ", "", 3, "missing"]
        b.decisions[0].source_evidence_refs = ["missing", "missing"]
        issues = audit_evidence(u, v, b)["issues"]
        codes = {x["code"] for x in issues}
        self.assertTrue({"invalid_id", "invalid_ref", "duplicate_ref", "empty_ref", "dangling_ref"} <= codes)
        self.assertEqual(sum(x["code"] == "duplicate_ref" for x in issues), 2)

    def test_location_extremes_and_malformed_values_are_serializable(self):
        u, v, _ = fixtures()
        v.evidence = [Evidence("a", "asr", "", "asr:0-10"),
                      Evidence("b", "ocr", "", "ocr:10s"),
                      Evidence("c", "asr", "", "asr:0-"),
                      Evidence("d", "asr", "", "asr:0-" + "9" * 400),
                      Evidence(float("nan"), "ocr", "", 123),
                      Evidence("f", [], "", None)]
        result = audit_evidence(u, v)
        self.assertEqual([x["location"] for x in result["evidence"]],
                         ["valid", "valid", "invalid", "invalid", "invalid", "not_applicable"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
