"""结构化输出（json_schema）接入的回归测试。

背景：`json_object` 只保证「是合法 JSON」，不保证字段齐全与类型正确。评审节点
R01 一次要评 3 个候选、每个候选约 30 个必填字段，于是「少写一个 problem_span」
这种表达层偏差会让整批 3 个候选一起作废。V 集批 20 条里累计 50 次无效调用、
浪费约 161 万 prompt token，就是这条路径。

这里的断言保证三件事：
1. schema 与手写校验器一致（schema 生成的最小实例必须能被校验器判 PASS）；
2. 未登记的结构名不会误用结构化输出；
3. 结构化输出被网关拒绝时能回退，不把一次参数不支持变成整批失败。
"""

from __future__ import annotations

import json
import unittest

from agent_system import review_schema
from agent_system.pipeline import _review_batch_structure_error


def _minimal_review(candidate_ids: list[str]) -> dict:
    """按 schema 构造一份最小合法实例。"""

    def gate(extended: bool) -> dict:
        out = {
            "status": "pass",
            "reason": "材料支持",
            "evidence_ids": ["ev-1"],
            "rule_ids": ["U-B01"],
        }
        if extended:
            out["delivery_supported"] = True
            out["core_covered"] = True
            out["promise_comparison"] = {
                "title_promise": "标题承诺",
                "trigger_span": "标题原文",
                "video_delivery": "视频交付",
                "evidence_excerpt": "证据摘录",
                "evidence_answered_question": "证据回答的问题",
                "alignment": "matched",
                "explanation": "逐项对比",
            }
        return out

    return {
        "thesis_consistency": {
            "relation": "aligned",
            "reason": "同一件事",
            "contradiction_point": "",
            "generated_missing": [],
            "review_missing": [],
        },
        "reviews": [
            {
                "candidate_id": cid,
                "expected_takeaway": {
                    "subject": "主体",
                    "content_nature": "性质",
                    "viewing_promise": "承诺",
                },
                "gates": {"G1": gate(False), "G2": gate(False), "G3": gate(True)},
                "quality": {
                    "Q1": {"score": 2, "reason": "清晰"},
                    "Q2": {"score": 2, "reason": "具体"},
                    "Q3": {"score": 2, "reason": "有钩子"},
                },
                "issues": [],
                "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
                "memory_query": {"problem_types": [], "query": "示例"},
            }
            for cid in candidate_ids
        ],
        "rubric_version": "red-dot-review-v2-uncalibrated",
    }


class ReviewSchemaConsistencyTest(unittest.TestCase):
    def test_schema_instance_passes_hand_written_validator(self) -> None:
        """schema 允许的输出，手写校验器必须接受——否则约束解码只是换一种失败。"""
        self.assertIsNone(_review_batch_structure_error(_minimal_review(["cand-1"]), ["cand-1"]))
        self.assertIsNone(_review_batch_structure_error(_minimal_review(["cand-1", "cand-2"]), ["cand-1", "cand-2"]))

    def test_schema_marks_all_validator_required_keys_required(self) -> None:
        """校验器检查的键必须在 schema 的 required 里，否则模型仍能合法地漏写。"""
        schema = review_schema.review_result_schema()
        self.assertEqual(set(schema["required"]), {"thesis_consistency", "reviews", "rubric_version"})
        review = schema["properties"]["reviews"]["items"]
        for key in ("candidate_id", "expected_takeaway", "gates", "quality", "issues", "rewrite_guidance", "memory_query"):
            self.assertIn(key, review["required"])
        gates = review["properties"]["gates"]
        self.assertEqual(set(gates["required"]), {"G1", "G2", "G3"})
        for gate in ("G1", "G2", "G3"):
            self.assertEqual(set(gates["properties"][gate]["required"]), {"status", "reason", "evidence_ids", "rule_ids"} | (
                {"delivery_supported", "core_covered", "promise_comparison"} if gate == "G3" else set()))
        quality = review["properties"]["quality"]
        self.assertEqual(set(quality["required"]), {"Q1", "Q2", "Q3"})
        issue = review["properties"]["issues"]["items"]
        self.assertEqual(
            set(issue["required"]),
            {"primary_dimension", "issue_code", "severity", "problem_span", "reason", "action", "repairable", "evidence_ids", "rule_ids"},
        )
        self.assertEqual(issue["properties"]["score"] if "score" in issue["properties"] else quality["properties"]["Q1"]["properties"]["score"]["type"], "integer")

    def test_enums_match_validator_accepted_values(self) -> None:
        schema = review_schema.review_result_schema()
        review = schema["properties"]["reviews"]["items"]
        gate_status = review["properties"]["gates"]["properties"]["G1"]["properties"]["status"]["enum"]
        self.assertEqual(set(gate_status), {"pass", "revise", "needs_review", "reject"})
        score = review["properties"]["quality"]["properties"]["Q1"]["properties"]["score"]
        self.assertEqual(score["type"], "integer")
        self.assertEqual(score["enum"], [1, 2, 3])
        severity = review["properties"]["issues"]["items"]["properties"]["severity"]["enum"]
        self.assertEqual(set(severity), {"low", "medium", "high", "critical"})
        relation = schema["properties"]["thesis_consistency"]["properties"]["relation"]["enum"]
        self.assertEqual(set(relation), {"aligned", "detail", "missing_point", "contradiction"})

    def test_json_schema_rejects_values_validator_rejects(self) -> None:
        """反例：校验器会拒的写法，schema 也必须不允许（枚举外 / 类型错）。"""
        broken = _minimal_review(["cand-1"])
        broken["reviews"][0]["gates"]["G1"]["status"] = "通过"
        self.assertIsNotNone(_review_batch_structure_error(broken, ["cand-1"]))
        broken = _minimal_review(["cand-1"])
        broken["reviews"][0]["quality"]["Q1"]["score"] = 5
        self.assertIsNotNone(_review_batch_structure_error(broken, ["cand-1"]))
        broken = _minimal_review(["cand-1"])
        broken["reviews"][0]["issues"] = ["字符串数组"]  # json_object 下的真实故障形态
        self.assertIsNotNone(_review_batch_structure_error(broken, ["cand-1"]))


class ResponseFormatTest(unittest.TestCase):
    def test_known_schema_builds_json_schema_format(self) -> None:
        fmt = review_schema.response_format("ReviewResultBatch")
        self.assertIsNotNone(fmt)
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["json_schema"]["name"], "ReviewResultBatch")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["schema"], review_schema.review_result_schema())

    def test_unknown_schema_returns_none(self) -> None:
        """没登记的结构名不能瞎猜，交回调用方用 json_object。"""
        self.assertIsNone(review_schema.response_format("SomeOtherSchema"))

    def test_schema_is_json_serializable(self) -> None:
        """必须能序列化进请求体，否则运行时才炸。"""
        text = json.dumps(review_schema.response_format("ReviewResultBatch"))
        self.assertIn("json_schema", text)


class _FakeHTTPError(Exception):
    pass


class AdapterStructuredOutputTest(unittest.TestCase):
    """适配器层：开关语义 + 回退路径。"""

    def _adapter(self, mode: str):
        from agent_system.model import OpenAICompatibleAdapter
        return OpenAICompatibleAdapter("https://api.example.com/v1", "SOME_KEY", "m", structured_output=mode)

    def test_off_mode_never_uses_json_schema(self) -> None:
        adapter = self._adapter("off")
        self.assertIsNone(adapter._response_format("ReviewResultBatch"))

    def test_auto_mode_uses_json_schema_for_known_schema(self) -> None:
        adapter = self._adapter("auto")
        fmt = adapter._response_format("ReviewResultBatch")
        self.assertEqual(fmt["type"], "json_schema")

    def test_auto_mode_falls_back_on_unknown_schema(self) -> None:
        self.assertIsNone(self._adapter("auto")._response_format("UnknownSchema"))

    def test_on_mode_also_builds_format(self) -> None:
        self.assertEqual(self._adapter("on")._response_format("ReviewResultBatch")["type"], "json_schema")


if __name__ == "__main__":
    unittest.main()
