"""逐候选主旨结论必须完整进入既有的语义裁决链。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, ModelReply
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")


class VaryingReviewAdapter(MockAdapter):
    def __init__(self, relations):
        super().__init__()
        self.relations = relations
        self.review_calls = 0

    def complete(self, **kwargs):
        reply = super().complete(**kwargs)
        if kwargs["task"] != "review":
            return reply
        data = json.loads(json.dumps(reply.data))
        relation = self.relations[self.review_calls % len(self.relations)]
        self.review_calls += 1
        data["thesis_consistency"] = relation
        return ModelReply(reply.status, data, reply.raw_text)


class PerCandidateConsistencyTests(unittest.TestCase):
    def run_pipeline(self, conclusions, judge):
        with tempfile.TemporaryDirectory() as temp:
            config = RunConfig(asset_root=str(ASSETS), output_dir=temp, review_per_candidate=True)
            dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
            storage = Storage(Path(temp) / "agent.db")
            adapter = VaryingReviewAdapter(conclusions)
            pipeline = Pipeline(dataset, storage, adapter, config)
            with patch("agent_system.pipeline.contracts_disagree", return_value=None), patch.object(
                pipeline, "_reconcile_contracts", return_value=judge
            ) as reconcile:
                task = pipeline.run("BV1Ltsf6xEpV")
            state = json.loads(storage.get_task(task)["state_json"])
            return state, reconcile.call_count, adapter.review_calls

    def test_later_contradiction_reaches_judge_in_either_order(self):
        aligned = {"relation": "aligned", "reason": "一致"}
        conflict = {"relation": "contradiction", "contradiction_point": "主体相反"}
        for conclusions in ([aligned, conflict], [conflict, aligned]):
            with self.subTest(conclusions=conclusions):
                state, calls, reviews = self.run_pipeline(conclusions, None)
                history = state["thesis_consistency_history"]
                self.assertGreaterEqual(reviews, 2)
                self.assertTrue({"aligned", "contradiction"}.issubset({x["relation"] for x in history}))
                self.assertTrue(all(x.get("candidate_id") for x in history))
                self.assertGreaterEqual(calls, 1)
                self.assertEqual(state["route"]["action"], "needs_review")

    def test_judge_can_resolve_conflict_without_fabricated_fact(self):
        state, calls, _ = self.run_pipeline(
            [{"relation": "aligned"}, {"relation": "contradiction", "contradiction_point": "主体相反"}],
            {"relation": "detail", "reason": "详略不同"},
        )
        self.assertGreaterEqual(calls, 1)
        self.assertEqual(state["contract_reconciliation"]["outcome"], "no_downgrade")

    def test_later_missing_point_triggers_existing_reconciliation(self):
        state, calls, _ = self.run_pipeline(
            [{"relation": "aligned"}, {"relation": "missing_point", "review_missing": ["一处要点"]}],
            {"relation": "detail", "reason": "详略不同"},
        )
        self.assertIn("missing_point", [x["relation"] for x in state["thesis_consistency_history"]])
        self.assertGreaterEqual(calls, 1)

    def test_invalid_conflict_is_not_promoted_to_fact(self):
        state, calls, _ = self.run_pipeline(
            [{"relation": "aligned"}, {"relation": "contradiction", "contradiction_point": ""}], None
        )
        self.assertNotIn("contradiction", [x["relation"] for x in state["thesis_consistency_history"]])
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
