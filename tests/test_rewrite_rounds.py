"""重写两轮的回归测试：跨轮判重与 slot_id 继承。

两个都是 2026-11-02 R09（BV17QpS6cEje 白菜）暴露的真实现场：
- 第 2 轮重写的标题与第 1 轮一字不差，却没有被任何判重拦下，原因是比较池
  一直停在首轮快照上；
- 模型重写时没复述 slot_id，整批候选被判结构非法，而槽位本该由父候选继承。
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")


class _RewriteAdapter(MockAdapter):
    """重写一律不写 slot_id；第二轮与第一轮输出完全相同。"""

    def __init__(self):
        self.rewrite_calls = 0

    def complete(self, **kwargs):
        if kwargs["task"] == "rewrite":
            self.rewrite_calls += 1
            reply = super().complete(**kwargs)
            round_no = int((kwargs["user"] or {}).get("rewrite_round", 0) or 1)
            for item in reply.data["candidates"]:
                item.pop("slot_id", None)
                # 第 2 轮及以上：与第 1 轮逐字相同，模拟"根本没改"
                item["headline"] = f"重写标题第{1 if round_no > 1 else round_no}版{kwargs['user'].get('parent_candidate_id', '')[-1]}"
            return reply
        if kwargs["task"] == "review":
            reply = super().complete(**kwargs)
            for item in reply.data["reviews"]:
                item["gates"]["G2"]["rule_ids"] = ["P1"]
                # 造一个可修复问题，让首轮路由进 rewrite
                item["quality"]["Q1"] = {"score": 1, "reason": "指代不明"}
            return reply
        return super().complete(**kwargs)


class RewriteRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, task_id):
        return json.loads(self.storage.get_task(task_id)["state_json"])

    def test_rewrite_inherits_parent_slot_when_model_omits_it(self):
        config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        config.extra["business_policy"] = self.config.extra["business_policy"]
        config.extra["max_rewrite_rounds"] = 1
        pipeline = Pipeline(self.dataset, self.storage, _RewriteAdapter(), config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = self._state(task_id)
        rewritten = [c for c in state.get("candidates", []) if c.get("parent_candidate_id")]
        self.assertTrue(rewritten, "没有产生重写候选")
        for candidate in rewritten:
            self.assertIn(candidate.get("slot_id"), ("source_rewrite", "core_summary", "core_hook"),
                          "重写候选的槽位没有从父候选继承")
            self.assertNotIn("rewrite changed or omitted parent slot_id", candidate.get("generation_schema_errors", []))
            self.assertNotIn("rewrite changed slot_id", candidate.get("generation_schema_errors", []))

    def test_second_round_identical_titles_are_dropped(self):
        config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        config.extra["business_policy"] = self.config.extra["business_policy"]
        config.extra["max_rewrite_rounds"] = 2
        adapter = _RewriteAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = self._state(task_id)
        dropped = state.get("rewrite_duplicates") or []
        self.assertTrue(dropped, "第 2 轮与第 1 轮逐字相同的标题没有被判重拦下")
        self.assertGreaterEqual(max(d["round"] for d in dropped), 2)
        # 判重之后不该继续烧更多轮：路由应停在"没有新标题"或预算耗尽，而不是再发起第 3 轮
        reason = (state.get("route") or {}).get("reason")
        self.assertIn(reason, {"REWRITE_NO_NEW_TITLE", "REWRITE_BUDGET_EXHAUSTED",
                               "REWRITE_PRODUCED_NO_CANDIDATE", "PENDING_VERIFICATION", None})


class _PartialReviewAdapter(MockAdapter):
    """重写产出三条，评审只回两条：第三条落成待核实，不该连累前两条。"""

    def __init__(self):
        self.review_calls = 0

    def complete(self, **kwargs):
        if kwargs["task"] == "rewrite":
            reply = super().complete(**kwargs)
            base = reply.data["candidates"][0]
            # 换掉句尾策略词，保持正文长度与内容有效，同时避开首轮那三个策略词
            tails = ["做法要点", "操作步骤", "日常吃法"]
            out = []
            for tail in tails:
                c = dict(base)
                c["headline"] = base["headline"][:-4] + tail
                out.append(c)
            reply.data["candidates"] = out
            return reply
        if kwargs["task"] == "review":
            self.review_calls += 1
            reply = super().complete(**kwargs)
            for item in reply.data["reviews"]:
                item["gates"]["G2"]["rule_ids"] = ["P1"]
            # 首轮全部可修，确保进入重写循环；重写轮不再施加质量问题
            if self.review_calls == 1:
                for item in reply.data["reviews"]:
                    item["quality"]["Q1"] = {"score": 1, "reason": "指代不明"}
            # 批量缺评触发降级；逐条时保留前两条，特定第三条仍缺评，验证局部待核不连坐。
            if self.review_calls >= 2 and len(reply.data["reviews"]) > 1:
                reply.data["reviews"] = reply.data["reviews"][:-1]
            elif self.review_calls >= 2 and len(reply.data["reviews"]) == 1 and str(reply.data["reviews"][0].get("candidate_id", "")).endswith("-3"):
                reply.data["reviews"] = []
            return reply
        return super().complete(**kwargs)


class SubrouteMergeTests(unittest.TestCase):
    """一条重写子路由需人工核实，不该把同批已合格的候选一起拖进人工。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_needs_review_does_not_cancel_ready_candidates(self):
        pipeline = Pipeline(self.dataset, self.storage, _PartialReviewAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        route = state.get("route") or {}
        pending = route.get("pending_review_candidate_ids") or []
        merged = [e for e in self.storage.events(task_id) if e["event_type"] == "subroute_escalation_merged"]
        self.assertTrue(merged, "没有走子路由合并分支（说明仍被整批升级）")
        self.assertEqual(route.get("action"), "handoff", f"合格候选被连坐：{route}")
        self.assertTrue(route.get("eligible_candidate_ids"), "合并后没有可交付候选")
        self.assertTrue(pending, "需核实的候选没有进入交付面")


if __name__ == "__main__":
    unittest.main()
