"""跨契约主旨一致性：生成侧与独立复核侧讲两件事时不给推荐。

真实故障（2026-11-01，canary BV1PwsR6oEKW 剧情）：

  N07 core_promise：看一段校园恋爱剧情：一方带吃的来关心对方，叮嘱对方好好休息后告别
  R00 core_promise：女生睡过头错过看电影，男生不但没生气还体贴关心

两套契约都进了 R01 的入参，提示词也写了「不一致就标 needs_review」，但 R01 只拿生成侧
契约核对标题；程序侧又没有任何跨契约比对，于是主体被读反的那条候选顶着「系统推荐
（已交付）」交到了人工面前。人工的判定是「送吃的和叮嘱的主体都是男生」——正是被读反的那处。

这组测试锁三件事：
1. 一致时不触发（不能把每一次正常的详略差异都算成分歧）；
2. 触发时摘掉推荐、清空槽位交付，但不改候选、不替谁翻案；
3. 两侧站在同样硬的地基上时，不许自动按某一侧重写（那只是把一次猜测换成另一次猜测）。
"""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, ModelReply
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage
from agent_system.thesis import contracts_disagree, evidence_hardness, semantics_overlap

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")

GENERATED = {
    "thesis_id": "t1",
    "core_promise": "看一段校园恋爱剧情：一方带吃的来关心对方，叮嘱对方好好休息后告别",
    "required_semantics": ["剧情对白中的关心与送吃的", "叮嘱对方回去休息", "告别离开"],
    "evidence_ids": ["e-asr-0007", "e-description"],
}
REVIEWED = {
    "thesis_id": "t1",
    "core_promise": "女生睡过头错过看电影，男生不但没生气还体贴关心，展现情侣间的甜蜜日常",
    "required_semantics": ["女生因睡过头而错过看电影", "男生体贴不追究、关心女生"],
    "evidence_ids": ["e-title", "e-description"],
}
EVIDENCE = {
    "e-title": {"evidence_id": "e-title", "quality": "source"},
    "e-description": {"evidence_id": "e-description", "quality": "model_derived_uncalibrated"},
    "e-asr-0007": {"evidence_id": "e-asr-0007", "quality": "raw_asr"},
    "e-frame-1": {"evidence_id": "e-frame-1", "quality": "raw_frame"},
}


class SemanticsOverlapTests(unittest.TestCase):
    def test_same_reading_does_not_trigger(self):
        left = {"core_promise": "小包装走红是因为从功能品变成配饰", "required_semantics": ["购买理由从功能转向配饰"]}
        right = {"core_promise": "小包装走红是因为从功能品变成配饰", "required_semantics": ["购买理由从功能转向配饰"]}
        self.assertIsNone(contracts_disagree(left, right), "同一读法不该被算成分歧")

    def test_two_different_stories_trigger(self):
        verdict = contracts_disagree(GENERATED, REVIEWED, EVIDENCE)
        self.assertIsNotNone(verdict)
        self.assertLess(verdict["semantics_overlap"], 0.2)
        self.assertIn("女生睡过头", verdict["review_promise"])
        self.assertIn("带吃的", verdict["generated_promise"])

    def test_long_vs_short_restatement_still_triggers(self):
        """已知代价：一侧写大意、另一侧写全，措辞差得远时仍会被判成分歧。

        用的是 BV19AHd6PEAP 实测的两份契约（同一件事，R00 更详尽）。词法指标分不开
        「同义改写」与「真分歧」，所以这类会多报。代价只是少一个推荐章、不挡交付，
        因此宁可多报；这条测试固定该行为，避免以后有人以为它精确而据此收放阈值。
        """
        left = {"core_promise": "博主认为小包装走红的关键，是它从功能品变成配饰，为消费者创造了新的购买理由。",
                "required_semantics": ["小包装/小样", "从功能到配饰的转变", "新的购买理由", "作者观点归属"]}
        right = {"core_promise": "作者从消费心理与品牌经营两个角度，解释小包装、小样等「小」生意为何能成为主流消费热点。",
                 "required_semantics": ["小包装、小样、mini美妆等「小」形态商品成为消费热点",
                                        "消费心理层面：够用替代划算、不浪费感、降低试错成本"]}
        verdict = contracts_disagree(left, right, EVIDENCE)
        self.assertIsNotNone(verdict, "这类多报是已知代价，不是回归")
        self.assertIsNone(verdict["better_grounded"])

    def test_empty_contract_never_triggers(self):
        self.assertIsNone(contracts_disagree({}, REVIEWED, EVIDENCE))
        self.assertIsNone(contracts_disagree(GENERATED, None, EVIDENCE))

    def test_no_shared_required_semantics_gives_zero_overlap(self):
        self.assertEqual(semantics_overlap({"required_semantics": ["甲"]}, {"required_semantics": ["乙"]}), 0.0)


class EvidenceHardnessTests(unittest.TestCase):
    def test_tiers_follow_distance_from_raw_material(self):
        hard = evidence_hardness({"evidence_ids": ["e-title"]}, EVIDENCE)
        raw = evidence_hardness({"evidence_ids": ["e-frame-1"]}, EVIDENCE)
        soft = evidence_hardness({"evidence_ids": ["e-description"]}, EVIDENCE)
        self.assertEqual(hard["max_tier"], 3)
        self.assertEqual(raw["max_tier"], 2)
        self.assertEqual(soft["max_tier"], 1)
        self.assertTrue(soft["soft_only"])
        self.assertFalse(raw["soft_only"])

    def test_unknown_evidence_is_not_treated_as_hard(self):
        verdict = evidence_hardness({"evidence_ids": ["e-missing"]}, EVIDENCE)
        self.assertEqual(verdict["max_tier"], 0)

    def test_harder_side_is_named_when_only_one_side_has_source_evidence(self):
        left = {**GENERATED, "evidence_ids": ["e-description"]}
        right = {**REVIEWED, "evidence_ids": ["e-title"]}
        verdict = contracts_disagree(left, right, EVIDENCE)
        self.assertEqual(verdict["better_grounded"], "review")

    def test_no_side_is_named_when_both_rest_on_model_inference(self):
        """两侧都建在未校准的模型推断上时不许选边——按任何一侧重写都是换个猜测。"""
        left = {**GENERATED, "evidence_ids": ["e-description"]}
        right = {**REVIEWED, "evidence_ids": ["e-description"]}
        verdict = contracts_disagree(left, right, EVIDENCE)
        self.assertIsNone(verdict["better_grounded"])


class PipelineDowngradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assets = ASSETS
        self.config = RunConfig(asset_root=str(self.assets), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, self.assets, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        self.task_id = self.pipeline.prepare("BV1Ltsf6xEpV")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, generated, reviewed):
        return {"video": {"evidence": list(EVIDENCE.values())},
                "thesis_contract": generated, "review_thesis_contract": reviewed}

    def _handoff(self):
        return {"action": "handoff", "recommended_candidate_id": "a1-1",
                "eligible_candidate_ids": ["a1-1", "a1-2"], "rewrite_candidate_id": None, "rewrite_round": 0}

    def test_handoff_is_downgraded_and_recommendation_removed(self):
        state = self._state(GENERATED, REVIEWED)
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")
        self.assertIsNone(route["recommended_candidate_id"])
        self.assertEqual(route["eligible_candidate_ids"], [])
        self.assertEqual(route["reason"], "THESIS_CONTRACT_DISAGREEMENT")
        self.assertIn("contract_disagreement", state)

    def test_event_records_both_readings(self):
        state = self._state(GENERATED, REVIEWED)
        self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        events = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_disagreement"]
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertIn("女生睡过头", payload["review_promise"])
        self.assertIn("带吃的", payload["generated_promise"])

    def test_agreeing_contracts_leave_handoff_untouched(self):
        state = self._state(GENERATED, dict(GENERATED))
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route, self._handoff())
        self.assertNotIn("contract_disagreement", state)

    def test_existing_needs_review_keeps_its_own_reason(self):
        """本来就没在推荐时只留痕，不覆盖既有原因，否则丢掉「它为什么没过」。"""
        state = self._state(GENERATED, REVIEWED)
        original = {"action": "needs_review", "recommended_candidate_id": None,
                    "eligible_candidate_ids": [], "reason": "PENDING_VERIFICATION", "rewrite_round": 0}
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, original)
        self.assertEqual(route["reason"], "PENDING_VERIFICATION")
        self.assertIn("contract_disagreement", state)

    def test_rewrite_route_is_not_downgraded(self):
        state = self._state(GENERATED, REVIEWED)
        route = self.pipeline._downgrade_on_contract_disagreement(
            self.task_id, state, {"action": "rewrite", "recommended_candidate_id": None, "rewrite_candidate_id": "a1-1"})
        self.assertEqual(route["action"], "rewrite")
        self.assertIn("contract_disagreement", state)

    def test_switch_can_disable_the_downgrade(self):
        self.config.extra["downgrade_on_contract_disagreement"] = False
        state = self._state(GENERATED, REVIEWED)
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "handoff")
        self.assertNotIn("contract_disagreement", state)


class WordingOnlyDiffersTests(unittest.TestCase):
    """核心承诺一字不差、只是必保语义换了说法：不得判成分歧。

    这是收紧触发条件后固定下来的边界——只堵一个指标会误伤这一类。
    """

    def test_identical_promise_with_reworded_semantics_does_not_trigger(self):
        left = {"core_promise": "小包装走红是因为从功能品变成配饰",
                "required_semantics": ["从功能到配饰的转变", "新的购买理由"]}
        right = {"core_promise": "小包装走红是因为从功能品变成配饰",
                 "required_semantics": ["小商品从功能转向配饰与个人风格表达", "品牌做小生意需权衡成本"]}
        self.assertLess(semantics_overlap(left, right), 0.2)
        self.assertIsNone(contracts_disagree(left, right))


class DisagreementReachesGenerationTests(unittest.TestCase):
    """端到端：R00 读法不同时要真的落到 state 与事件上，不能只是函数级正确。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pipeline_run_records_disagreement_from_independent_review(self):
        class SplittingAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "thesis_review":
                    reply = super().complete(**kwargs)
                    data = json.loads(json.dumps(reply.data))
                    contract = data.get("thesis_contract") or {}
                    contract["core_promise"] = "女生睡过头错过约定，男生体贴不追究，展现情侣甜蜜日常"
                    contract["required_semantics"] = ["女生睡过头错过约定", "男生体贴不追究"]
                    data["thesis_contract"] = contract
                    return ModelReply(reply.status, data, reply.raw_text)
                return super().complete(**kwargs)

        pipeline = Pipeline(self.dataset, self.storage, SplittingAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = self.storage.load_state(task_id) if hasattr(self.storage, "load_state") else None
        if state is None:
            import sqlite3
            conn = sqlite3.connect(Path(self.tmp.name) / "agent.db")
            conn.row_factory = sqlite3.Row
            state = json.loads(conn.execute("SELECT state_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()["state_json"])
        self.assertIn("contract_disagreement", state, "独立复核读法不同时必须留在 state 里")
        self.assertNotEqual(state["route"]["action"], "handoff")
        self.assertIsNone(state["route"]["recommended_candidate_id"])
        self.assertEqual(state.get("delivery_candidates"), [])
        events = [e for e in self.storage.events(task_id) if e["event_type"] == "thesis_contract_disagreement"]
        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()


class ReviewSurfaceTests(unittest.TestCase):
    """评审台：两套读法要并排给人看，且候选不能再读成「系统推荐」。"""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.batch = Path(self.tmp.name) / "batch"
        self.batch.mkdir()
        self.disagreement = {
            "semantics_overlap": 0.05,
            "promise_similarity": 0.145,
            "generated_promise": "女生夜里给男生送吃的、叮嘱他好好休息",
            "review_promise": "女生睡过头错过约定，男生体贴不追究",
            "generated_evidence": {"tiers": {"raw_asr": 2}, "max_tier": 2, "soft_only": False},
            "review_evidence": {"tiers": {"source": 1}, "max_tier": 3, "soft_only": False},
            "better_grounded": None,
        }
        candidates = [
            {"candidate_id": "c1", "headline": "夜里送吃的还叮嘱他回去休息",
             "rendered_title": "【剧情演绎】夜里送吃的还叮嘱他回去休息", "prefix_labels": ["剧情演绎"]},
        ]
        state = {
            "video": {"video_id": "BV1x", "title": "来源标题", "asset_paths": {}},
            "candidates": candidates,
            # 降级后的真实产物：动作是 needs_review、推荐为空、原因是本机制。
            "route": {"action": "needs_review", "recommended_candidate_id": None,
                      "eligible_candidate_ids": [], "reason": "THESIS_CONTRACT_DISAGREEMENT"},
            "candidate_decisions": [{"candidate_id": "c1", "status": "ready_for_human_review", "reason": "通过"}],
            "contract_disagreement": self.disagreement,
        }
        with sqlite3.connect(self.batch / "agent.db") as conn:
            conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, "
                         "config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                         ("task-01", "BV1x", "pending_human", "{}", json.dumps(state, ensure_ascii=False), "t", "t"))
        from agent_system.batch_review import title_hash
        (self.batch / "review_mapping.json").write_text(json.dumps([{
            "blind_id": "R01", "index": 1, "task_id": "task-01", "video_id": "BV1x",
            "category": "剧情短片", "source_title": "来源标题", "task_status": "pending_human",
            "candidate_source": "final_state", "route": state["route"],
            # 冻结批次只认映射里带哈希的候选，评审台不接受事后替换。
            "candidates": [{"candidate_id": "c1", "title_sha256": title_hash(candidates[0]["rendered_title"]),
                            "status": "ready_for_human_review"}],
        }], ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from agent_system.batch_review import BatchReviewStore
        return BatchReviewStore(self.batch)

    def test_task_payload_carries_both_readings(self):
        task = self._store().get_task("task-01")
        self.assertEqual(task["contract_disagreement"]["generated_promise"], self.disagreement["generated_promise"])
        self.assertEqual(task["contract_disagreement"]["review_promise"], self.disagreement["review_promise"])
        self.assertEqual(task["state"]["contract_disagreement"], self.disagreement)

    def test_candidate_is_not_marked_recommended(self):
        """摘掉推荐后，候选状态不能还是 recommended——否则评审台照样读成「系统推荐」。"""
        task = self._store().get_task("task-01")
        self.assertNotEqual(task["state"]["candidates"][0]["status"], "recommended")

    def test_list_tasks_exposes_disagreement_for_the_banner(self):
        listed = [t for t in self._store().list_tasks() if t["task_id"] == "task-01"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["contract_disagreement"]["review_promise"], self.disagreement["review_promise"])


class ReviewPageTests(unittest.TestCase):
    def test_page_renders_both_readings_and_relabels_endorsement(self):
        from agent_system import batch_review_page
        page = batch_review_page.PAGE
        self.assertIn("两套独立解读不一致", page)
        self.assertIn("dis.generated_promise", page)
        self.assertIn("dis.review_promise", page)
        self.assertIn("function statusLabel", page)
        self.assertIn("解读有分歧，未推荐", page)


class DeclaredConsistencyTests(unittest.TestCase):
    """R01 回传的任务级判定：不增加模型调用，但要有位置、且不能敷衍就作数。"""

    def test_accepts_the_four_relations(self):
        from agent_system.pipeline import _normalize_thesis_consistency
        for relation in ("aligned", "detail", "missing_point"):
            got = _normalize_thesis_consistency({"thesis_consistency": {"relation": relation, "reason": "x"}})
            self.assertEqual(got["relation"], relation)
        got = _normalize_thesis_consistency({"thesis_consistency": {
            "relation": "contradiction", "contradiction_point": "送吃的主体相反"}})
        self.assertEqual(got["contradiction_point"], "送吃的主体相反")

    def test_contradiction_without_a_named_point_is_not_trusted(self):
        """只丢一个结论、不点名互斥在哪，视为没答——否则降级交付会被一句空话触发。"""
        from agent_system.pipeline import _normalize_thesis_consistency
        self.assertIsNone(_normalize_thesis_consistency({"thesis_consistency": {"relation": "contradiction"}}))
        self.assertIsNone(_normalize_thesis_consistency({"thesis_consistency": {"relation": "contradiction", "contradiction_point": "   "}}))

    def test_missing_or_unknown_relation_falls_back(self):
        from agent_system.pipeline import _normalize_thesis_consistency
        self.assertIsNone(_normalize_thesis_consistency({}))
        self.assertIsNone(_normalize_thesis_consistency({"thesis_consistency": {"relation": "好像不一致"}}))
        self.assertIsNone(_normalize_thesis_consistency(None))

    def test_reads_through_batch_wrappers(self):
        from agent_system.pipeline import _normalize_thesis_consistency
        for wrapper in ("ReviewResultBatch", "ReviewBatch", "review_batch"):
            got = _normalize_thesis_consistency({wrapper: {"thesis_consistency": {"relation": "detail"}}})
            self.assertEqual(got["relation"], "detail")


class DeclaredConsistencyWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        self.task_id = self.pipeline.prepare("BV1Ltsf6xEpV")

    def tearDown(self):
        self.tmp.cleanup()

    def _handoff(self):
        return {"action": "handoff", "recommended_candidate_id": "a1-1",
                "eligible_candidate_ids": ["a1-1"], "rewrite_candidate_id": None, "rewrite_round": 0}

    def _state(self, declared_relation=None, point="互斥处"):
        state = {
            "video": {"evidence": list(EVIDENCE.values())},
            # 两侧措辞一致：词法判据不会触发，专门用来验证「R01 说互斥也能拦住」
            "thesis_contract": {"core_promise": "博主演示古早妆容画法", "required_semantics": ["演示画法"], "evidence_ids": ["e-description"]},
            "review_thesis_contract": {"core_promise": "博主演示古早妆容画法", "required_semantics": ["演示画法"], "evidence_ids": ["e-description"]},
        }
        if declared_relation:
            state["thesis_consistency_history"] = [{"relation": declared_relation, "contradiction_point": point, "round": 0}]
        return state

    def test_review_declared_contradiction_blocks_handoff_even_when_lexically_aligned(self):
        state = self._state("contradiction")
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")
        self.assertEqual(route["reason"], "THESIS_CONTRACT_DISAGREEMENT")
        self.assertIsNone(route["recommended_candidate_id"])

    def test_review_declared_detail_does_not_cancel_lexical_trigger(self):
        """并集语义：R01 说是详略也不撤销词法触发。词法不可靠，但撤销会退回「没有任何检查」。"""
        state = self._state("detail")
        state["thesis_contract"] = GENERATED
        state["review_thesis_contract"] = REVIEWED
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")

    def test_aligned_and_no_lexical_trigger_leaves_handoff_untouched(self):
        state = self._state("aligned")
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route, self._handoff())

    def test_event_records_which_judge_triggered(self):
        state = self._state("contradiction", point="送吃的主体相反")
        self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        payload = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_disagreement"][0]["payload"]
        self.assertEqual(payload["triggered_by"], "review")
        self.assertEqual(payload["declared_contradiction_point"], "送吃的主体相反")


class DeclaredConsistencyReachesStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_output_is_persisted_on_state(self):
        class DeclaringAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    reply = super().complete(**kwargs)
                    data = json.loads(json.dumps(reply.data))
                    data["thesis_consistency"] = {"relation": "contradiction", "contradiction_point": "主体相反",
                                                  "reason": "两份读法不能同时成立"}
                    return ModelReply(reply.status, data, reply.raw_text)
                return super().complete(**kwargs)

        pipeline = Pipeline(self.dataset, self.storage, DeclaringAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        import sqlite3
        conn = sqlite3.connect(Path(self.tmp.name) / "agent.db")
        conn.row_factory = sqlite3.Row
        state = json.loads(conn.execute("SELECT state_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()["state_json"])
        self.assertIn("thesis_consistency", state)
        self.assertEqual(state["thesis_consistency"]["relation"], "contradiction")
        self.assertIn("thesis_consistency_history", state)
        self.assertNotEqual(state["route"]["action"], "handoff")

class SemanticJudgeOverridesLexicalTests(unittest.TestCase):
    """词法当筛子、语义当判官：判官说「同义详略」时必须撤销词法的误报。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        self.task_id = self.pipeline.prepare("BV1Ltsf6xEpV")

    def tearDown(self):
        self.tmp.cleanup()

    def _handoff(self):
        return {"action": "handoff", "recommended_candidate_id": "a1-1",
                "eligible_candidate_ids": ["a1-1"], "rewrite_candidate_id": None, "rewrite_round": 0}

    def _state(self):
        # 用真实的词法误报对：两侧措辞差得多，词法必触发
        return {"video": {"evidence": list(EVIDENCE.values())},
                "thesis_contract": GENERATED, "review_thesis_contract": REVIEWED}

    def _judge(self, relation, **extra):
        self.pipeline._reconcile_contracts = lambda task_id, state: {"relation": relation, **extra}

    def test_detail_verdict_cancels_the_lexical_alarm(self):
        state = self._state()
        self.assertTrue(contracts_disagree(GENERATED, REVIEWED), "前提：词法必须本来就触发")
        self._judge("detail", reason="只是详略，同一件事")
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "handoff")
        self.assertEqual(state["contract_reconciliation"]["outcome"], "no_downgrade")
        event = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_reconciled"][0]
        self.assertEqual(event["payload"]["relation"], "detail")

    def test_aligned_verdict_also_cancels(self):
        state = self._state()
        self._judge("aligned")
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "handoff")

    def test_contradiction_verdict_downgrades_and_records_why(self):
        state = self._state()
        self._judge("contradiction", contradiction_point="送吃的主体相反", reason="两份不能同时成立")
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")
        payload = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_disagreement"][0]["payload"]
        self.assertEqual(payload["relation"], "contradiction")
        self.assertEqual(payload["reconcile_point"], "送吃的主体相反")

    def test_missing_point_downgrades_and_names_what_is_missing(self):
        state = self._state()
        self._judge("missing_point", review_missing=["画面顶部网友负面评价黑条"])
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")
        payload = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_disagreement"][0]["payload"]
        self.assertEqual(payload["reconcile_missing"], ["画面顶部网友负面评价黑条"])

    def test_judge_failure_falls_back_to_downgrading(self):
        """判官不可用时按保守口径降级——宁可多摘一个推荐章，也不把可疑交付当合格。"""
        state = self._state()
        self.pipeline._reconcile_contracts = lambda task_id, state: None
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "needs_review")
        payload = [e for e in self.storage.events(self.task_id) if e["event_type"] == "thesis_contract_disagreement"][0]["payload"]
        self.assertEqual(payload["relation"], "unavailable")
        self.assertIn("thesis_reconcile_fallback", [e["event_type"] for e in self.storage.events(self.task_id)])

    def test_no_call_when_lexical_says_aligned_and_nobody_objected(self):
        """两侧措辞一致时不该花钱调判官。"""
        called = []
        self.pipeline._reconcile_contracts = lambda task_id, state: called.append(1)
        state = {"video": {"evidence": list(EVIDENCE.values())},
                 "thesis_contract": {"core_promise": "博主演示古早妆容画法", "required_semantics": ["演示画法"], "evidence_ids": ["e-description"]},
                 "review_thesis_contract": {"core_promise": "博主演示古早妆容画法", "required_semantics": ["演示画法"], "evidence_ids": ["e-description"]}}
        route = self.pipeline._downgrade_on_contract_disagreement(self.task_id, state, self._handoff())
        self.assertEqual(route["action"], "handoff")
        self.assertEqual(called, [])


class RegenerateOnReviewThesisTests(unittest.TestCase):
    """分歧且只有复核侧踩在原始材料上时，按复核侧重选看点重出一组，而不是一律摘章。

    依据（2026-11-02 人工复核，BV1u9HA6JEdw 红裙）：生成侧理由整组建在 raw_asr /
    raw_ocr 上，还把贯穿全片的「网友：她真的很欠扁样子」整条漏掉；复核侧引到了 e-title
    （source 级，原始标题），读法与上传者原题一致。摘章让这条视频三个槽位全空。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        self.task_id = self.pipeline.prepare("BV1Ltsf6xEpV")
        self.calls: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    # --- 构造 -------------------------------------------------------------
    HARD_GENERATED = {"thesis_id": "t1", "topic": "妆教", "delivery_type": "教程",
                      "core_promise": "面向新手演示古早娇媚妆容的上妆步骤",
                      "required_semantics": ["上妆步骤"], "evidence_ids": ["e-asr-0007"]}
    HARD_REVIEWED = {"thesis_id": "t2", "topic": "红裙采访", "delivery_type": "访谈",
                     "core_promise": "呈现红裙受访内容与顶部网友负面评价的对照",
                     "required_semantics": ["网友负面评价归属网友"], "evidence_ids": ["e-title"]}

    def _state(self, generated=None, reviewed=None, declared=None):
        return {"video": {"evidence": list(EVIDENCE.values())},
                "thesis_contract": dict(generated or self.HARD_GENERATED),
                "review_thesis_contract": dict(reviewed or self.HARD_REVIEWED),
                "thesis_consistency_history": list(declared or [{"relation": "missing_point", "round": 0}])}

    def _rehighlight(self):
        return {"thesis_contract": {"thesis_id": "t2", "topic": "红裙采访", "delivery_type": "访谈",
                                    "core_promise": "呈现红裙受访内容与顶部网友负面评价的对照",
                                    "required_semantics": ["网友负面评价归属网友"],
                                    "evidence_ids": ["e-title"], "core_unit_ids": ["u4"]},
                "highlights": [{"highlight_id": "h1", "claim_ids": ["c8"], "value": "顶部黑条网友评价",
                                "role": "main", "relation": "core", "thesis_id": "t2", "unit_id": "u4",
                                "evidence_ids": ["e-title"]}]}

    def _run(self, state=None, judge=None, rehighlight=None, route=None):
        state = state if state is not None else self._state()
        self.pipeline._reconcile_contracts = lambda task_id, st: (self.calls.append("judge"), judge or {"relation": "missing_point"})[1]

        def fake_call(task_id, node, name, *a, **k):
            self.calls.append(name)
            return rehighlight if rehighlight is not None else self._rehighlight()

        self.pipeline._call = fake_call
        # 真实模型会自己给 cand-1 这种 id，与旧组同名——这正是要防的碰撞
        self.pipeline._generate_and_review = lambda *a, **k: {
            "candidates": [{"candidate_id": "cand-1", "slot_id": "source_rewrite", "thesis_id": "t2"}],
            "reviews": [{"candidate_id": "cand-1"}],
            "decisions": [{"candidate_id": "cand-1", "status": "ready_for_human_review"}],
            "route": {}}
        candidates = {"candidates": [{"candidate_id": "a1-1", "slot_id": "source_rewrite", "thesis_id": "t1"}],
                      "reviews": [], "decisions": [], "route": dict(route or {"action": "handoff"})}
        understanding = unittest.mock.Mock()
        understanding.semantic_units = [unittest.mock.Mock(unit_id="u4", kind="文字叠加", claim_ids=["c8"])]
        understanding.claims = [unittest.mock.Mock(claim_id="c8", evidence_refs=["e-title"])]
        boundary = unittest.mock.Mock(unit_decisions={}, decisions=[])
        allowed = [unittest.mock.Mock(claim_id="c8")]
        merged, info = self.pipeline._regenerate_on_review_thesis(
            self.task_id, state, unittest.mock.Mock(asr_segments=[]), understanding, boundary,
            [], [], unittest.mock.Mock(as_context=lambda: {}), allowed, candidates)
        return state, merged, info

    # --- 该重出的场景 -----------------------------------------------------
    def test_regenerates_when_only_review_side_cites_source_evidence(self):
        state, merged, info = self._run()
        self.assertEqual(info["status"], "ok")
        self.assertEqual(len(merged["candidates"]), 2, "旧组必须保留，两组一起给人对照")
        self.assertEqual(merged["candidates"][1]["thesis_basis"], "review")
        self.assertEqual(info["review_thesis_id"], "t2")
        self.assertIn("thesis_regenerated_on_review", [e["event_type"] for e in self.storage.events(self.task_id)])

    def test_regeneration_leaves_generated_side_contract_untouched(self):
        state, _, _ = self._run()
        self.assertEqual(state["thesis_contract"]["thesis_id"], "t1", "生成侧契约是基准，不能被换掉")

    # --- 不该重出的场景 ---------------------------------------------------
    def test_skips_when_neither_side_cites_source(self):
        generated = {**self.HARD_GENERATED}
        reviewed = {**self.HARD_REVIEWED, "evidence_ids": ["e-asr-0007"]}
        state, merged, info = self._run(self._state(generated, reviewed))
        self.assertEqual(info["status"], "not_regenerated")
        self.assertEqual(len(merged["candidates"]), 1)
        self.assertNotIn("highlight", self.calls)

    def test_skips_when_generated_side_is_harder(self):
        reviewed = {**self.HARD_REVIEWED, "evidence_ids": ["e-asr-0007"]}
        state, merged, info = self._run(self._state(None, reviewed))
        self.assertEqual(info["status"], "not_regenerated")
        self.assertNotIn("highlight", self.calls)

    def test_judge_detail_skips_before_spending_a_highlight_call(self):
        state, merged, info = self._run(judge={"relation": "detail"})
        self.assertIsNone(info)
        self.assertNotIn("highlight", self.calls, "判官说同义详略时不该再花一次重选看点")

    def test_highlight_reselection_failure_keeps_original_candidates(self):
        state, merged, info = self._run(rehighlight={"abandon": True, "reason": "无可用看点"})
        self.assertEqual(info["status"], "highlight_reselection_failed")
        self.assertEqual(len(merged["candidates"]), 1)

    # --- 与降级的衔接 -----------------------------------------------------
    def test_disagreement_is_disclosed_but_not_downgraded(self):
        state, merged, _ = self._run()
        route = self.pipeline._downgrade_on_contract_disagreement(
            self.task_id, state, {"action": "handoff", "recommended_candidate_id": "a1-1",
                                  "eligible_candidate_ids": ["a1-1"], "rewrite_round": 0})
        self.assertEqual(route["action"], "handoff", "已按复核侧重出就不该再摘推荐章")
        self.assertEqual(state["contract_disagreement"]["resolution"], "regenerated_on_review")
        events = [e["event_type"] for e in self.storage.events(self.task_id)]
        self.assertIn("thesis_contract_disagreement_disclosed", events)
        self.assertNotIn("thesis_contract_disagreement", events)

    def test_judge_is_asked_once_even_though_two_stages_need_it(self):
        state, _, _ = self._run()
        self.pipeline._downgrade_on_contract_disagreement(
            self.task_id, state, {"action": "handoff", "recommended_candidate_id": "a1-1",
                                  "eligible_candidate_ids": ["a1-1"], "rewrite_round": 0})
        self.assertEqual(self.calls.count("judge"), 1, "重出与降级共用同一次判定，不能问两遍")

    # --- 交付主旨集合 -----------------------------------------------------
    def test_delivery_accepts_both_thesis_ids_after_regeneration(self):
        from agent_system.pipeline import _delivery_thesis_ids
        state = self._state()
        self.assertEqual(_delivery_thesis_ids(state), {"t1"})
        state["thesis_regeneration"] = {"status": "ok", "review_thesis_id": "t2"}
        self.assertEqual(_delivery_thesis_ids(state), {"t1", "t2"})

    def test_slot_delivery_accepts_a_set_of_thesis_ids(self):
        from agent_system.pipeline import _slot_delivery
        candidates = [{"candidate_id": "a1-1", "slot_id": "source_rewrite", "thesis_id": "t1"},
                      {"candidate_id": "a101-1", "slot_id": "core_summary", "thesis_id": "t2"}]
        decisions = [{"candidate_id": "a1-1", "status": "ready_for_human_review", "quality_sum": 5},
                     {"candidate_id": "a101-1", "status": "ready_for_human_review", "quality_sum": 6}]
        delivery, seen = _slot_delivery(candidates, decisions, {"t1", "t2"})
        self.assertEqual({c["candidate_id"] for c in delivery}, {"a1-1", "a101-1"})
        self.assertEqual(seen, {"source_rewrite", "core_summary"})


    def test_new_group_ids_do_not_collide_with_the_old_group(self):
        state, merged, info = self._run()
        ids = [c["candidate_id"] for c in merged["candidates"]]
        self.assertEqual(len(ids), len(set(ids)), "两组候选 id 不能重名")
        self.assertEqual(ids[0], "a1-1")
        self.assertEqual(info["review_candidate_ids"], [ids[1]])
        self.assertEqual(merged["decisions"][-1]["candidate_id"], ids[1], "决策表必须跟着改名")
        self.assertEqual(merged["reviews"][-1]["candidate_id"], ids[1])

    # --- 流程性收尾的抢救 ------------------------------------------------
    def test_exhausted_rewrite_budget_is_recovered_when_new_group_is_ready(self):
        state, merged, info = self._run(route={"action": "stop", "reason": "REWRITE_BUDGET_EXHAUSTED"})
        self.assertEqual(merged["route"]["action"], "handoff", "重出的合格候选不该被流程性收尾挡住")
        self.assertEqual(info["route_recovered_from"], "REWRITE_BUDGET_EXHAUSTED")

    def test_handoff_is_recomputed_so_the_new_group_can_be_delivered(self):
        original = {"action": "handoff", "recommended_candidate_id": "a1-1",
                    "eligible_candidate_ids": ["a1-1"], "rewrite_round": 0,
                    "reason": "eligible_candidates_ranked_by_Q1_Q2_Q3"}
        state, merged, info = self._run(route=original)
        self.assertEqual(merged["route"]["action"], "handoff")
        self.assertEqual(merged["route"]["eligible_candidate_ids"], ["review-1"], "重算后必须纳入新组（旧组在这一组打桩里没有决策记录）")
        self.assertEqual(info["route_recovered_from"], "eligible_candidates_ranked_by_Q1_Q2_Q3")

    def test_business_level_needs_review_is_not_overturned(self):
        original = {"action": "needs_review", "reason": "core_fact_or_business_policy_requires_human_verification"}
        state, merged, info = self._run(route=original)
        self.assertEqual(merged["route"], original, "业务性降级不因为重出而被推翻")
        self.assertNotIn("route_recovered_from", info)


    def test_review_group_wins_the_slot_when_both_are_ready(self):
        from agent_system.pipeline import _slot_delivery
        candidates = [{"candidate_id": "cand-1", "slot_id": "source_rewrite", "thesis_id": "t1", "thesis_basis": ""},
                      {"candidate_id": "review-1", "slot_id": "source_rewrite", "thesis_id": "t1", "thesis_basis": "review"}]
        decisions = [{"candidate_id": "cand-1", "status": "ready_for_human_review", "quality_sum": 9},
                     {"candidate_id": "review-1", "status": "ready_for_human_review", "quality_sum": 5}]
        delivery, _ = _slot_delivery(candidates, decisions, {"t1"}, prefer_basis="review")
        self.assertEqual([c["candidate_id"] for c in delivery], ["review-1"], "同槽竞争时复核侧组优先")

    def test_without_preference_the_higher_quality_wins(self):
        from agent_system.pipeline import _slot_delivery
        candidates = [{"candidate_id": "cand-1", "slot_id": "source_rewrite", "thesis_id": "t1", "thesis_basis": ""},
                      {"candidate_id": "review-1", "slot_id": "source_rewrite", "thesis_id": "t1", "thesis_basis": "review"}]
        decisions = [{"candidate_id": "cand-1", "status": "ready_for_human_review", "quality_sum": 9},
                     {"candidate_id": "review-1", "status": "ready_for_human_review", "quality_sum": 5}]
        delivery, _ = _slot_delivery(candidates, decisions, {"t1"})
        self.assertEqual([c["candidate_id"] for c in delivery], ["cand-1"])


class RegeneratedDisagreementSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "agent_system" / "batch_review_page.py").read_text()

    def test_banner_switches_wording_when_regenerated(self):
        self.assertIn("regenerated_on_review", self.page)
        self.assertIn("已按复核侧重出一组候选", self.page)

    def test_candidate_carries_its_basis_badge(self):
        self.assertIn("按复核侧解读重出", self.page)
        self.assertIn("thesis_basis==='review'", self.page)

    def test_both_missing_directions_are_rendered(self):
        self.assertIn("生成侧漏了", self.page)
        self.assertIn("复核侧漏了", self.page)
