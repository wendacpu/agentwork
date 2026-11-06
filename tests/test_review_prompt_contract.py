"""评审提示词与严格结构及既有安全兜底的一致性。"""
import json
import unittest

from agent_system.pipeline import (
    _limit_check_coverage_errors,
    _limit_check_verdicts,
    _normalize_reviews,
    _review_batch_structure_error,
)
from agent_system.prompts import REVIEW_PROMPT
from agent_system.review_schema import review_result_schema


def _check_schema(test, value, schema):
    kind = schema.get("type")
    if kind == "object":
        test.assertIsInstance(value, dict)
        test.assertTrue(set(schema.get("required", [])) <= set(value))
        if schema.get("additionalProperties") is False:
            test.assertTrue(set(value) <= set(schema["properties"]))
        for key, child in value.items():
            _check_schema(test, child, schema["properties"][key])
    elif kind == "array":
        test.assertIsInstance(value, list)
        for child in value:
            _check_schema(test, child, schema["items"])
    elif kind == "string":
        test.assertIsInstance(value, str)
    elif kind == "integer":
        test.assertIs(type(value), int)
    elif kind == "boolean":
        test.assertIs(type(value), bool)
    # 示例中的枚举用竖线罗列选项，实际输出必须选其中一个。
    if "enum" in schema:
        values = value.split("|") if isinstance(value, str) else [value]
        test.assertTrue(all(item in schema["enum"] for item in values))


class ReviewPromptContractTest(unittest.TestCase):
    def test_example_matches_strict_schema(self):
        example = next(line for line in REVIEW_PROMPT.splitlines() if line.startswith('{"thesis_consistency"'))
        payload = json.loads(example)
        _check_schema(self, payload, review_result_schema())
        self.assertEqual(payload["reviews"][0]["limit_checks"], [])
        self.assertIsNone(_review_batch_structure_error(payload, ["候选ID"]))

    def test_issue_contract_and_default_routing(self):
        issue_schema = review_result_schema()["properties"]["reviews"]["items"]["properties"]["issues"]["items"]
        self.assertFalse(issue_schema["additionalProperties"])
        self.assertTrue({"issue_id", "rollback_to"}.isdisjoint(issue_schema["properties"]))
        self.assertIn("不要输出 issue_id 或 rollback_to", REVIEW_PROMPT)
        self.assertIn("G3 回 N07", REVIEW_PROMPT)
        issue = {"primary_dimension": "G3", "issue_code": "G3_META_DISCOURSE", "severity": "medium", "repairable": True, "problem_span": "视频称", "evidence_ids": [], "rule_ids": [], "reason": "载体元话语", "action": "改写"}
        result = _normalize_reviews({"reviews": [{"candidate_id": "c1", "issues": [issue]}]}, ["c1"])[0]["issues"][0]
        self.assertEqual(result["issue_id"], "c1-issue-1")
        self.assertEqual(result["rollback_to"], "N07")

    def test_limit_checks_coverage_and_conflict_fail_closed(self):
        self.assertIn("没有限定时也必须输出空数组 limit_checks: []", REVIEW_PROMPT)
        requirement = {"claim_id": "c1", "limit": "不得写成所有人"}
        mechanical = {"limit_requirements": [requirement]}
        self.assertTrue(_limit_check_coverage_errors({"limit_checks": []}, mechanical))
        check = {**requirement, "verdict": "satisfied", "reason": "未扩大范围"}
        self.assertFalse(_limit_check_coverage_errors({"limit_checks": [check]}, mechanical))
        conflict = {**check, "verdict": "violated"}
        self.assertEqual(_limit_check_verdicts({"limit_checks": [check, conflict]})[("c1", requirement["limit"])], "unclear")


if __name__ == "__main__":
    unittest.main()
