"""重写结构重试的离线回归，不访问模型或网络。"""
import unittest

from agent_system.model import ModelError
from test_r01_structure_recovery import make_pipeline


class RewriteStructureRecoveryTests(unittest.TestCase):
    def call(self, pipeline):
        return pipeline._call("task", "N09", "rewrite", {}, "AgentDraft")

    def test_missing_headline_retries_and_preserves_invalid_output(self):
        bad = {"candidates": [{"change_reason": "原因"}]}
        good = {"candidates": [{"headline": "有效标题"}]}
        pipeline = make_pipeline([bad, good])
        self.assertEqual(self.call(pipeline), good)
        self.assertEqual(len(pipeline.adapter.calls), 2)
        error = pipeline.adapter.calls[1]["user"]["_previous_output_error"]
        self.assertIn("headline", error["error"])
        self.assertIn("change_reason", error["previous_output_tail"])
        self.assertEqual(sum(name == "model_reply" for name, _ in pipeline.storage.events), 1)

    def test_empty_candidates_are_valid(self):
        pipeline = make_pipeline([{"candidates": []}])
        self.assertEqual(self.call(pipeline), {"candidates": []})
        self.assertEqual(len(pipeline.adapter.calls), 1)

    def test_invalid_cache_is_retried(self):
        good = {"candidates": [{"headline": "有效标题"}]}
        pipeline = make_pipeline([good], cached={"candidates": [{}]})
        self.assertEqual(self.call(pipeline), good)
        self.assertIn("model_cache_invalid", [name for name, _ in pipeline.storage.events])

    def test_invalid_exhaustion_and_alias(self):
        pipeline = make_pipeline([{"candidates": [{}]}, {"candidates": [{"headline": " "}]}])
        with self.assertRaises(ModelError):
            self.call(pipeline)
        pipeline = make_pipeline([{"candidates": [{"candidate_title": "可用标题"}]}])
        self.assertEqual(self.call(pipeline)["candidates"][0]["candidate_title"], "可用标题")


if __name__ == "__main__":
    unittest.main()
