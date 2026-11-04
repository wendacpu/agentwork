"""R01 结构准入和恢复的离线回归测试；不访问任何模型或网络。"""
import copy
import unittest
from types import SimpleNamespace

from agent_system.model import ModelError, ModelReply
from agent_system.pipeline import Pipeline, _normalize_reviews


def review(cid):
    return {
        "candidate_id": cid,
        "expected_takeaway": "内容",
        "gates": {k: {"status": "pass", "reason": "理由", "evidence_ids": ["e1"], "rule_ids": []} for k in ("G1", "G2", "G3")},
        "quality": {k: {"score": 2, "reason": "理由"} for k in ("Q1", "Q2", "Q3")},
        "issues": [], "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
        "memory_query": {"problem_types": [], "query": ""},
    }


class Storage:
    def __init__(self, cached=None):
        self.cached = cached
        self.events = []

    def cached_model_reply(self, task_id, request_hash):
        return self.cached

    def event(self, task_id, node, event, data):
        self.events.append((event, data))


class Adapter:
    model = "offline"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, ModelReply):
            return response
        return ModelReply("ok", response)


def make_pipeline(responses, cached=None, retries=1, per_candidate=False):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = SimpleNamespace(schema_retry_limit=retries, review_model="offline", generation_model="offline",
                                      review_per_candidate=per_candidate)
    pipeline.storage = Storage(cached)
    pipeline.adapter = pipeline.review_adapter = Adapter(responses)
    pipeline._review_input = lambda video, state, candidates, checks: {"candidate_titles": candidates}
    return pipeline


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"candidate_id": x} for x in ("a", "b", "c")]
        self.complete = {"reviews": [review(x) for x in ("a", "b", "c")]}

    def run_review(self, pipeline):
        return pipeline._review_with_fallback("task", None, {}, self.candidates, [])

    def test_partial_batch_retried_before_success_and_cache(self):
        p = make_pipeline([{"reviews": [review("a")]}, self.complete])
        result = self.run_review(p)
        self.assertEqual(result, self.complete)
        self.assertEqual(len(p.adapter.calls), 2)
        self.assertIn("_previous_output_error", p.adapter.calls[1]["user"])
        self.assertEqual(sum(name == "model_reply" for name, _ in p.storage.events), 1)

    def test_invalid_old_cache_is_not_accepted(self):
        p = make_pipeline([self.complete], cached={"reviews": [review("a")]})
        self.assertEqual(self.run_review(p), self.complete)
        self.assertEqual(len(p.adapter.calls), 1)
        self.assertIn("model_cache_invalid", [name for name, _ in p.storage.events])

    def test_partial_and_malformed_retry_then_per_candidate(self):
        malformed = copy.deepcopy(self.complete)
        malformed["reviews"][1]["issues"] = "not a list"
        p = make_pipeline([{"reviews": [review("a")]}, malformed,
                   {"ReviewBatch": {"reviews": [review("a")]}},
                   {"reviews": [review("b")]}, {"reviews": [review("c")]}])
        result = self.run_review(p)
        self.assertEqual([r["candidate_id"] for r in result["reviews"]], ["a", "b", "c"])
        self.assertEqual(result["degraded_failures"], [])
        self.assertEqual(len(p.adapter.calls), 5)

    def test_all_retryable_failures_leave_every_candidate_missing(self):
        # 每条候选：整批 2 次尝试 + 逐条 2 次 + 逐条补一次 2 次 = 6；再加整批 2 次。
        p = make_pipeline([ModelError("bad", True)] * 14)
        result = self.run_review(p)
        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["degraded_failures"], ["a", "b", "c"])
        missing = _normalize_reviews(result, ["a", "b", "c"])
        self.assertEqual([r["candidate_id"] for r in missing], ["a", "b", "c"])
        self.assertTrue(all("gates" not in r for r in missing))
        self.assertEqual(len(p.adapter.calls), 14)

    def test_single_retry_recovers_a_candidate_that_dropped_quality(self):
        """整批坏、逐条仍漏 quality 时再试一次，成功就保留该候选。"""
        bad = {"reviews": [{"candidate_id": "a"}]}
        p = make_pipeline([ModelError("review", True), ModelError("review", True), bad, bad,
                           {"reviews": [review("a")]}])
        result = p._review_with_fallback("task", None, {}, self.candidates[:1], [])
        self.assertEqual([r["candidate_id"] for r in result["reviews"]], ["a"])
        self.assertEqual(result["degraded_failures"], [])
        self.assertIn("review_single_retry", [name for name, _ in p.storage.events])

    def test_single_candidate_failure_retains_missing_review(self):
        p = make_pipeline([ModelError("bad", True)] * 6)
        result = p._review_with_fallback("task", None, {}, self.candidates[:1], [])
        self.assertEqual(result["degraded_failures"], ["a"])
        self.assertEqual(result["reviews"], [])

    def test_configured_per_candidate_skips_the_batch_request(self):
        """配置成逐条后不再先赌整批——整批失败过一次的代价是三个候选一起丢。"""
        p = make_pipeline([{"reviews": [review("a")]}, {"reviews": [review("b")]}, {"reviews": [review("c")]}],
                          per_candidate=True)
        result = self.run_review(p)
        self.assertEqual([r["candidate_id"] for r in result["reviews"]], ["a", "b", "c"])
        self.assertEqual(result["per_candidate_reason"], "configured")
        self.assertEqual(result["degraded_failures"], [])
        # 三次调用，每次只带一个候选；整批请求一次都没发。
        self.assertEqual(len(p.adapter.calls), 3)
        for call in p.adapter.calls:
            self.assertEqual(len(call["user"]["candidate_titles"]), 1)

    def test_configured_per_candidate_keeps_one_bad_candidate_from_killing_the_rest(self):
        """坏一条不再连坐：另两条照常通过。

        b 要走满两条失败路径：`_call` 内部重试耗尽（2 次）→ 逐条补一次再耗尽（2 次）。
        """
        p = make_pipeline([{"reviews": [review("a")]},
                           ModelError("bad", True), ModelError("bad", True),
                           ModelError("bad", True), ModelError("bad", True),
                           {"reviews": [review("c")]}], per_candidate=True)
        result = self.run_review(p)
        self.assertEqual([r["candidate_id"] for r in result["reviews"]], ["a", "c"])
        self.assertEqual(result["degraded_failures"], ["b"])

    def test_per_candidate_preserves_task_level_thesis_consistency(self):
        """thesis_consistency 是任务级结论，逐条请求时不能整段丢掉。"""
        consistency = {"relation": "aligned", "reason": "同一件事", "contradiction_point": "",
                       "generated_missing": [], "review_missing": []}
        first = {"reviews": [review("a")], "thesis_consistency": consistency, "rubric_version": "r1"}
        p = make_pipeline([first, {"reviews": [review("b")]}, {"reviews": [review("c")]}], per_candidate=True)
        result = self.run_review(p)
        self.assertEqual(result["thesis_consistency"], consistency)
        self.assertEqual(result["rubric_version"], "r1")

    def test_fatal_batch_does_not_fallback(self):
        p = make_pipeline([ModelError("unauthorized", False)])
        with self.assertRaises(ModelError):
            self.run_review(p)
        self.assertEqual(len(p.adapter.calls), 1)

    def test_fatal_single_stops_fallback(self):
        p = make_pipeline([ModelError("bad", True), ModelError("bad", True), ModelError("payment", False)])
        with self.assertRaises(ModelError):
            self.run_review(p)
        self.assertEqual(len(p.adapter.calls), 3)

    def test_candidate_level_retry_asks_only_the_broken_candidate(self):
        """整批合法、单条漏 quality 时只重问那一条，并把错误带回去。"""
        p = make_pipeline([{"reviews": [review("a")]}])
        result = p._retry_single_review("task", None, {}, {"candidate_id": "a"}, None, ["quality missing or invalid"])
        self.assertEqual(result["candidate_id"], "a")
        self.assertIn("quality missing or invalid", p.adapter.calls[0]["user"]["_previous_output_error"])
        self.assertEqual([c["candidate_id"] for c in p.adapter.calls[0]["user"]["candidate_titles"]], ["a"])
        self.assertIn("review_candidate_retry", [name for name, _ in p.storage.events])

    def test_structure_failure_carries_raw_output_into_retry(self):
        """结构不合法的原文必须带回给下一次尝试，否则模型只能盲改同类错误。"""
        broken_text = '{"reviews": [{"candidate_id": "a"}]}'
        p = make_pipeline([ModelReply("ok", {"reviews": [review("a")]}, raw_text=broken_text), self.complete])
        self.assertEqual(self.run_review(p), self.complete)
        retried = p.adapter.calls[1]["user"]["_previous_output_error"]
        self.assertIn("candidate_id", retried["previous_output_tail"])
        self.assertIn("invalid review structure", retried["error"])
        invalid = [d for name, d in p.storage.events if name == "model_reply_invalid"]
        self.assertEqual(invalid[0]["raw_output"], broken_text)

    def test_duplicate_and_unknown_candidates_rejected(self):
        duplicate = {"reviews": [review("a"), review("a"), review("c")]}
        unknown = {"reviews": [review("a"), review("b"), review("z")]}
        p = make_pipeline([duplicate, unknown, {"reviews": [review("a")]}, {"reviews": [review("b")]}, {"reviews": [review("c")]}])
        self.assertEqual(len(self.run_review(p)["reviews"]), 3)
        self.assertEqual(len(p.adapter.calls), 5)


if __name__ == "__main__":
    unittest.main()
