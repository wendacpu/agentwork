"""主旨保持最后一轮的确定性回归：主旨卡、三槽、规则ID、评审矛盾与归属。

这些测试锁住「已经理解到的核心不能在生成/审核/重写/展示时丢失」的机制行为，
不声称关键字能证明语义正确。
"""
from __future__ import annotations

import unittest

from agent_system.rules.loader import load_common_rule_ids
from agent_system.schemas import validate_review
from agent_system.thesis import normalize_thesis_contract, validate_highlight_alignment, SLOTS
from agent_system.types import BoundarySet, Highlight, SemanticUnit, Understanding, UnitDecision


def _review(g3=None, issues=None):
    return {
        "candidate_id": "a1",
        "expected_takeaway": {"subject": "s", "content_nature": "n", "viewing_promise": "p"},
        "gates": {
            "G1": {"status": "pass", "reason": "r", "evidence_ids": ["e1"], "rule_ids": []},
            "G2": {"status": "pass", "reason": "r", "evidence_ids": [], "rule_ids": []},
            "G3": g3 or {"status": "pass", "reason": "r", "evidence_ids": ["e1"], "rule_ids": [], "delivery_supported": True, "core_covered": True},
        },
        "quality": {"Q1": {"score": 2, "reason": "r"}, "Q2": {"score": 2, "reason": "r"}, "Q3": {"score": 2, "reason": "r"}},
        "issues": issues or [],
        "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
        "memory_query": {"problem_types": [], "query": ""},
    }


def _understanding():
    return Understanding("教程", [], [], [], [], [], [SemanticUnit("u1", "画古早妆", ["c1"], ["e1"])], [], [])


def _boundary():
    return BoundarySet(decisions=[], unit_decisions=[UnitDecision("u1", "limited")])


class RuleIdSuffixTests(unittest.TestCase):
    def test_suffix_rule_ids_are_recognized(self):
        ids = load_common_rule_ids()
        for rid in ("U-B03a", "U-B07a", "U-B07b", "U-B07c", "U-B07d"):
            self.assertIn(rid, ids)


class ThesisContractTests(unittest.TestCase):
    def test_valid_contract_normalizes(self):
        contract, errors = normalize_thesis_contract(
            {"thesis_contract": {"thesis_id": "t1", "topic": "古早妆", "delivery_type": "教程",
                                 "core_promise": "教画古早妆", "required_semantics": ["古早妆", "怎么画"],
                                 "core_unit_ids": ["u1"], "evidence_ids": ["e1"]}},
            _understanding(), _boundary())
        self.assertEqual(errors, [])
        self.assertEqual(contract["thesis_id"], "t1")

    def test_missing_fields_are_reported(self):
        _, errors = normalize_thesis_contract({}, _understanding(), _boundary())
        self.assertTrue(any("缺少字段" in e for e in errors), errors)

    def test_dangling_core_unit_is_reported(self):
        _, errors = normalize_thesis_contract(
            {"thesis_contract": {"thesis_id": "t1", "topic": "x", "delivery_type": "资讯",
                                 "core_promise": "x", "required_semantics": ["x"], "core_unit_ids": ["u999"]}},
            _understanding(), _boundary())
        self.assertTrue(any("不存在" in e for e in errors), errors)

    def test_excluded_core_unit_is_reported(self):
        boundary = BoundarySet(decisions=[], unit_decisions=[UnitDecision("u1", "excluded")])
        _, errors = normalize_thesis_contract(
            {"thesis_contract": {"thesis_id": "t1", "topic": "x", "delivery_type": "资讯",
                                 "core_promise": "x", "required_semantics": ["x"], "core_unit_ids": ["u1"]}},
            _understanding(), boundary)
        self.assertTrue(any("已排除" in e for e in errors), errors)

    def test_empty_required_semantics_is_reported(self):
        _, errors = normalize_thesis_contract(
            {"thesis_contract": {"thesis_id": "t1", "topic": "x", "delivery_type": "资讯",
                                 "core_promise": "x", "required_semantics": [], "core_unit_ids": ["u1"]}},
            _understanding(), _boundary())
        self.assertTrue(any("required_semantics" in e for e in errors), errors)


class HighlightAlignmentTests(unittest.TestCase):
    def test_main_must_be_core(self):
        contract = {"thesis_id": "t1"}
        highlight = Highlight("h1", ["c1"], "v", "r", ["e1"], role="main", relation="support", thesis_id="t1")
        errors = validate_highlight_alignment([highlight], contract)
        self.assertTrue(any("core" in e for e in errors), errors)

    def test_main_thesis_id_must_match(self):
        contract = {"thesis_id": "t1"}
        highlight = Highlight("h1", ["c1"], "v", "r", ["e1"], role="main", relation="core", thesis_id="t2")
        errors = validate_highlight_alignment([highlight], contract)
        self.assertTrue(any("不一致" in e for e in errors), errors)


class ReviewConsistencyTests(unittest.TestCase):
    def _validate(self, review):
        return validate_review(review, evidence_ids={"e1"}, rule_ids={"U", "V01", "F01", "B-FACT"},
                               issue_codes={"G3_CORE_POINT_OMITTED"}, policy_rule_ids=set())

    def test_gate_pass_with_critical_issue_conflicts(self):
        review = _review(issues=[{"issue_id": "a1-G3_CORE_POINT_OMITTED", "primary_dimension": "G3",
                                  "issue_code": "G3_CORE_POINT_OMITTED", "severity": "high", "repairable": True,
                                  "problem_span": "x", "reason": "r", "action": "a", "evidence_ids": [], "rule_ids": []}])
        errors = self._validate(review)
        self.assertTrue(any("pass conflicts" in e for e in errors), errors)

    def test_low_severity_core_omission_conflicts_with_pass(self):
        review = _review(issues=[{"issue_id": "i", "primary_dimension": "G3", "issue_code": "G3_CORE_POINT_OMITTED", "severity": "low", "repairable": True, "problem_span": "x", "reason": "r", "action": "a", "evidence_ids": [], "rule_ids": []}])
        self.assertTrue(any("pass conflicts" in e for e in self._validate(review)))

    def test_low_severity_quality_issue_does_not_conflict_with_gate(self):
        # Q2 的 low 优化问题不应与 G3 pass 冲突（矛盾校验只针对 gate 维度的高/严重问题）
        review = _review(issues=[{"issue_id": "a1-Q2_TOO_GENERIC", "primary_dimension": "Q2",
                                  "issue_code": "Q2_TOO_GENERIC", "severity": "low", "repairable": True,
                                  "problem_span": "x", "reason": "r", "action": "a", "evidence_ids": [], "rule_ids": []}])
        errors = self._validate(review)
        self.assertFalse(any("pass conflicts" in e for e in errors), errors)


class AttributionPromptContractTests(unittest.TestCase):
    def test_generation_and_rewrite_preserve_required_attribution_without_default_persona(self):
        from agent_system.prompts import GENERATION_PROMPT, REWRITE_PROMPT
        for prompt in (GENERATION_PROMPT, REWRITE_PROMPT):
            self.assertIn("不得", prompt)
            self.assertIn("归属", prompt)
            self.assertIn("身份", prompt)
            self.assertNotIn("个人博主的动作主体写「博主」", prompt)
        self.assertIn("不能因追求简洁删除必要归属", GENERATION_PROMPT)
        self.assertIn("不得凭领域虚构职业资质", REWRITE_PROMPT)

    def test_independent_review_checks_necessity_and_specificity(self):
        from agent_system.prompts import REVIEW_PROMPT
        for term in ("不因没有「博主」判缺失", "G1_SOURCE_ATTRIBUTION_MISSING", "G1_UNSUPPORTED_FACT", "Q2_TOO_GENERIC", "不要求机械写出领域标签"):
            self.assertIn(term, REVIEW_PROMPT)


class SlotEnumTests(unittest.TestCase):
    def test_slots_fixed_three(self):
        self.assertEqual(list(SLOTS), ["source_rewrite", "core_summary", "core_hook"])


if __name__ == "__main__":
    unittest.main()
