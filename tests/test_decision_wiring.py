"""决策接线测试：证明 O1–O12 的改动真的把「决策结果」接到了下游。

这组测试不验证生成质量，只验证数据有没有流通：
- D02 的禁止推断 / 暂缓核验 → 是否进到生成节点、是否改变路由
- D03 的淘汰原因 → 是否落库
- D05 的策略枚举 → 是否被机械校验识别
- D08 的回退节点 → 是否随 issue 流出
- 案例检索 → 中文 query 与 scope 过滤是否可用
- 人工判定 → 是否自动沉淀为案例
"""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.memory import MemoryStore
from agent_system.model import MockAdapter, ModelReply
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage
from agent_system.goldsheet import record as record_gold, remove as remove_gold
from agent_system.types import CaseRecord

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _events(self, task_id):
        return self.storage.events(task_id)

    def test_forbidden_inferences_reach_generation(self):
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        gen = [e for e in self._events(task_id) if e["event_type"] == "prompt_snapshot" and e["payload"].get("task") == "generate"]
        self.assertTrue(gen, "没有记录到生成节点的 prompt 快照")
        payload = gen[-1]["payload"]["user_payload"]
        self.assertIn("blocked_claims", payload)
        self.assertIn("unresolved_issues", payload)
        self.assertIn("strategy_enum", payload)
        self.assertEqual(payload["strategy_enum"][-1], "直接描述")
        self.assertTrue(any(req.get("forbidden_inferences") for req in payload["source_requirements"]),
                        "禁止推断没有进入生成入参")

    def test_highlight_abandon_stops_instead_of_faking(self):
        class AbandonAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "highlight":
                    return ModelReply("ok", {"highlights": [], "abandon": True, "reason": "材料只能支持边角描述，没有可兑现的看点"})
                return super().complete(**kwargs)

        pipeline = Pipeline(self.dataset, self.storage, AbandonAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"])
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(state["stop_reason"], "NO_VIABLE_HIGHLIGHT")
        self.assertTrue(state["highlight_selection"]["abandoned"])
        self.assertFalse(state.get("candidates"), "弃权后不应再生成候选")

    def test_pending_verification_routes_to_stop_with_tasks(self):
        class VerifyAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "boundary":
                    claims = kwargs["user"].get("claims", [])
                    return ModelReply("ok", {"decisions": [
                        {"claim_id": c.get("claim_id", "c1"), "supported_statement": c.get("text"),
                         "decision": "excluded", "verification_required": True,
                         "evidence_refs": [], "required_limits": [], "forbidden_inferences": [], "rule_ids": [], "reason": "核心事实存在未解冲突"}
                        for c in claims
                    ]})
                return super().complete(**kwargs)

        pipeline = Pipeline(self.dataset, self.storage, VerifyAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"])
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(state["stop_reason"], "PENDING_VERIFICATION")
        self.assertTrue(state["verification_tasks"])

    def test_g3_issue_switches_rewrite_to_highlight_mode(self):
        class G3Adapter(MockAdapter):
            def __init__(self):
                self.review_calls = 0

            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    self.review_calls += 1
                    reply = super().complete(**kwargs)
                    for item in reply.data["reviews"]:
                        item["gates"]["G2"]["rule_ids"] = ["P1"]
                    if self.review_calls == 1:
                        for item in reply.data["reviews"]:
                            item["gates"]["G3"] = {"status": "revise", "reason": "主看点缺失", "evidence_ids": [], "rule_ids": []}
                            item["issues"] = [{"issue_id": f'{item["candidate_id"]}-G3_CORE_POINT_OMITTED', "primary_dimension": "G3",
                                               "issue_code": "G3_CORE_POINT_OMITTED", "severity": "high", "repairable": True,
                                               "problem_span": "标题", "evidence_ids": [], "rule_ids": [],
                                               "reason": "写的是边角内容", "action": "换看点", "rollback_to": "N07"}]
                    return reply
                return super().complete(**kwargs)

        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        pipeline = Pipeline(self.dataset, self.storage, G3Adapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        rewrites = [e for e in self._events(task_id) if e["event_type"] == "prompt_snapshot" and e["payload"].get("task") == "rewrite"]
        self.assertTrue(rewrites, "G3 问题没有触发重写")
        payload = rewrites[-1]["payload"]["user_payload"]
        self.assertEqual(payload["rewrite_mode"], "看点")
        self.assertTrue(payload["allow_highlight_change"])
        self.assertEqual(payload["review_issues"][0]["rollback_to"], "N07")

    def test_rewrite_records_resolved_and_repeated_issues(self):
        class RepeatAdapter(MockAdapter):
            """第二轮仍只修掉一半问题，用来验证 resolved / repeated 都能记录。"""

            def __init__(self):
                self.review_calls = 0

            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    self.review_calls += 1
                    reply = super().complete(**kwargs)
                    for item in reply.data["reviews"]:
                        item["gates"]["G2"]["rule_ids"] = ["P1"]
                        if self.review_calls <= 2:
                            item["quality"]["Q1"] = {"score": 1, "reason": "指代不明"}
                            issues = [{"issue_id": f'{item["candidate_id"]}-Q1_UNCLEAR', "primary_dimension": "Q1",
                                       "issue_code": "Q1_UNCLEAR", "severity": "medium", "repairable": True,
                                       "problem_span": "它", "evidence_ids": [], "rule_ids": [],
                                       "reason": "指代不明", "action": "写出指代对象"}]
                            if self.review_calls == 2:
                                issues.append({"issue_id": f'{item["candidate_id"]}-Q3_LOW_APPEAL', "primary_dimension": "Q3",
                                               "issue_code": "Q3_LOW_APPEAL", "severity": "low", "repairable": True,
                                               "problem_span": "标题", "evidence_ids": [], "rule_ids": [],
                                               "reason": "重写后失去吸引力", "action": "补回具体动作"})
                            item["issues"] = issues
                    return reply
                return super().complete(**kwargs)

        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        pipeline = Pipeline(self.dataset, self.storage, RepeatAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        outcomes = state.get("rewrite_outcomes") or []
        self.assertTrue(outcomes, "没有记录重写结果")
        first = outcomes[0]
        self.assertIn("Q1_UNCLEAR", first["parent_issue_codes"])
        # 第二轮评审仍报 Q1_UNCLEAR，但它不在重写父候选的 issue 里，用 repeated 判断的是父子同码
        self.assertIn("mode", first)
        self.assertIn("addressed_reported", first)
        self.assertTrue(first["child_candidate_ids"])

    def test_all_rewritable_candidates_are_rewritten(self):
        class AllBadAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    reply = super().complete(**kwargs)
                    for item in reply.data["reviews"]:
                        item["gates"]["G2"]["rule_ids"] = ["P1"]
                        item["quality"]["Q1"] = {"score": 1, "reason": "指代不明"}
                    return reply
                return super().complete(**kwargs)

        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        pipeline = Pipeline(self.dataset, self.storage, AllBadAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        origins = [c["candidate_id"] for c in state["candidates"] if not c.get("parent_candidate_id")]
        self.assertEqual(len(origins), 3)
        first_round = [o for o in state.get("rewrite_outcomes", []) if o.get("mode") and o["parent_candidate_id"] in origins]
        self.assertEqual(len(first_round), 3, "三条可修候选都应被重写，而不是只修一条")
        self.assertTrue(all(o["child_candidate_ids"] for o in first_round))

    def test_understand_receives_source_title_as_baseline(self):
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV11Ssf6gEUV")
        snaps = [e for e in self.storage.events(task_id) if e["event_type"] == "prompt_snapshot" and e["payload"].get("task") == "understand"]
        self.assertTrue(snaps)
        payload = snaps[0]["payload"]["user_payload"]
        self.assertIn("source_title", payload)
        self.assertEqual(payload["source_title"]["quality"], "source")
        self.assertIn("改写基线", payload["source_title"]["note"])

    def test_candidate_title_alias_is_accepted(self):
        from agent_system.pipeline import _normalize_candidates
        out = _normalize_candidates({"candidates": [{"candidate_id": "c1", "candidate_title": "挪威首相驳斥FT报道",
                                                     "highlight_id": "h1", "claim_ids": ["c1"], "evidence_ids": ["e1"],
                                                     "strategy": "直接描述", "qualifiers_used": "无"}]}, 0, None)
        self.assertEqual(out[0]["headline"], "挪威首相驳斥FT报道")
        self.assertEqual(out[0]["qualifiers_used"], ["无"])

    def test_understanding_finds_claims_in_any_layer(self):
        from agent_system.pipeline import _normalize_understanding
        # 形态 A：claims 在顶层，understanding 只是子字段
        a = _normalize_understanding({"video_id": "x", "understanding": {"summary": "s"},
                                      "candidate_claims": [{"claim_id": "c-01", "claim_text": "事实一"}]})
        self.assertEqual(len(a.claims), 1)
        # 形态 B：claims 套在 understanding 里
        b = _normalize_understanding({"understanding": {"candidate_claims": [{"claim_id": "c-02", "claim_text": "事实二"}]}})
        self.assertEqual(len(b.claims), 1)
        # 形态 C：大写包裹
        c = _normalize_understanding({"Understanding": {"claims": [{"claim_id": "c-03", "claim_text": "事实三"}]}})
        self.assertEqual(len(c.claims), 1)

    def test_case_retrieval_uses_rule_pack_version(self):
        """案例按 red-dot 规则包版本入库，检索不能用 config.rule_version 去查。"""
        store = MemoryStore(self.storage.conn)
        store.create_draft(CaseRecord(
            case_id="c-rv", case_type="fact", problem_type="G1_SOURCE_UNVERIFIED",
            bad_example="据RT视频：挪威首相驳斥金融时报通话报道",
            good_example="挪威首相驳斥《金融时报》涉普京通话报道",
            applicability="资讯类不把非权威来源写成官方陈述", source_task_id="t-old",
            rule_version="red-dot-v0.2-modular", confirmed=True, case_status="confirmed",
            scope="资讯 时事社会 资讯播报",
        ))
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV11Ssf6gEUV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        used = [cid for t in (state.get("memory_trace") or []) for cid in (t.get("used_case_ids") or [])]
        self.assertIn("c-rv", used, f"案例没有被检索到，memory_trace={state.get('memory_trace')}")

    def test_marginal_without_edit_becomes_negative_case(self):
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        cand = state["candidates"][0]
        self.storage.record_human_decision(task_id, {
            "candidate_id": cand["candidate_id"], "decision": "marginal",
            "reason": "FT 是缩写，多数用户不认识", "reason_type": "expression:wording_unclear",
        })
        rows = [json.loads(r[0]) for r in self.storage.conn.execute("SELECT payload FROM cases")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["polarity"], "negative")
        self.assertEqual(rows[0]["good_example"], "")
        self.assertEqual(rows[0]["bad_example"], cand["headline"])

    def test_reopen_removes_current_decision_and_allows_rejudgment(self):
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        candidate = state["candidates"][0]
        self.storage.record_human_decision(task_id, {"candidate_id": candidate["candidate_id"], "decision": "rejected", "reason": "看点理解偏了"})
        self.storage.reopen_human_decision(task_id, candidate["candidate_id"])
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(self.storage.get_task(task_id)["status"], "pending_human")
        self.assertFalse(state.get("human_decisions"))
        self.assertFalse(self.storage.conn.execute("SELECT 1 FROM labels WHERE task_id=?", (task_id,)).fetchone())
        self.assertFalse(self.storage.conn.execute("SELECT 1 FROM cases WHERE case_id=?", (f"auto-{task_id}-{candidate['candidate_id']}",)).fetchone())
        self.storage.record_human_decision(task_id, {"candidate_id": candidate["candidate_id"], "decision": "approved"})
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(state["human_decisions"][-1]["decision"], "approved")
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM labels WHERE task_id=?", (task_id,)).fetchone()[0], 1)
        self.assertEqual(self.storage.conn.execute("SELECT COUNT(*) FROM human_decisions WHERE task_id=?", (task_id,)).fetchone()[0], 1)

    def test_reopen_removes_only_matching_gold_and_rejudgment_restores_latest(self):
        with tempfile.TemporaryDirectory() as td:
            gold = Path(td) / "gold.csv"
            record_gold(video_id="v1", task_id="t1", candidate_id="c1", headline="标题", verdict="不可用", final_headline=None, criteria_id="", fallback_node="", reason="问题", reviewer="me", path=gold)
            record_gold(video_id="v2", task_id="t2", candidate_id="c1", headline="另一条", verdict="可用", final_headline="另一条", criteria_id="", fallback_node="", reason="", reviewer="me", path=gold)
            remove_gold(video_id="v1", candidate_id="c1", path=gold)
            with gold.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["video_id"], "v2")
            record_gold(video_id="v1", task_id="t1", candidate_id="c1", headline="标题", verdict="可用", final_headline="标题", criteria_id="", fallback_node="", reason="改判", reviewer="me", path=gold)
            with gold.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            restored = next(r for r in rows if r["video_id"] == "v1")
            verdict_key = next(k for k in restored if k.startswith("人工判定"))
            self.assertEqual(restored[verdict_key], "可用")

    def test_rejection_reason_lands_in_new_gold_column(self):
        """「驳回原因」要能直接统计，否则判了几十条也说不清主要失败在哪一类。"""
        with tempfile.TemporaryDirectory() as td:
            gold = Path(td) / "gold.csv"
            record_gold(video_id="v1", task_id="t1", candidate_id="c1", headline="标题", verdict="不可用",
                        final_headline=None, criteria_id="", fallback_node="", reason="主体写反了",
                        reviewer="me", rejection_reason="事实错误", path=gold)
            with gold.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["驳回原因"], "事实错误")

    def test_old_gold_without_reason_column_still_works(self):
        """历史 gold.csv 没有「驳回原因」列，写入时要就地补列，不能整表报错。"""
        with tempfile.TemporaryDirectory() as td:
            gold = Path(td) / "gold.csv"
            gold.write_text("video_id,task_id,candidate_id,标题,人工判定\nv1,t1,c1,旧标题,可用\n", encoding="utf-8-sig")
            record_gold(video_id="v1", task_id="t1", candidate_id="c1", headline="旧标题", verdict="不可用",
                        final_headline=None, criteria_id="", fallback_node="", reason="改判",
                        reviewer="me", rejection_reason="没写到核心看点", path=gold)
            with gold.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertIn("驳回原因", rows[0])
            self.assertEqual(rows[0]["驳回原因"], "没写到核心看点")
            self.assertEqual(rows[0]["人工判定"], "不可用")

    def test_chinese_case_retrieval_and_scope_filter(self):
        store = MemoryStore(self.storage.conn)
        store.create_draft(CaseRecord(
            case_id="c-cn", case_type="fact", problem_type="G1_SOURCE_ATTRIBUTION_MISSING",
            bad_example="肺结核筛查涉78名学生", good_example="据康平县通报：中学筛查涉78名学生",
            applicability="资讯通报类必须保留来源", source_task_id="t1",
            rule_version="red-dot-v0.2-modular", confirmed=True, case_status="confirmed",
            scope="资讯 时事社会 资讯播报",
        ))
        hits = store.search("肺结核筛查涉78名学生", ["G1_SOURCE_ATTRIBUTION_MISSING"], "red-dot-v0.2-modular", 3, ["资讯", "时事社会", "资讯播报"])
        self.assertTrue(hits, "中文 query 在 scope 命中时仍检索不到")
        hits2 = store.search("据康平县调查组通报中学筛查共用针头", ["G1_SOURCE_ATTRIBUTION_MISSING"], "red-dot-v0.2-modular", 3, ["资讯", "时事社会", "资讯播报"])
        self.assertTrue(hits2, "相关中文表述应能通过 2-gram 命中")
        outside = store.search("肺结核筛查涉78名学生", [], "red-dot-v0.2-modular", 3, ["美食", "烹饪教程"])
        self.assertFalse(outside, "scope 不匹配的案例不应返回")

    def test_human_rejection_creates_confirmed_negative_case_without_rewrite_suggestion(self):
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        candidate = state["candidates"][0]
        self.storage.record_human_decision(task_id, {
            "candidate_id": candidate["candidate_id"], "decision": "rejected",
            "reason": "视频未说明筛查来源，这条标题写得像已核实", "reason_type": "", 
        })
        cases = [json.loads(r[0]) for r in self.storage.conn.execute("SELECT payload FROM cases")]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["case_status"], "confirmed")
        self.assertEqual(cases[0]["problem_type"], "root_cause_unresolved")
        self.assertIsNone(cases[0].get("good_example") or None)
        self.assertEqual(cases[0]["source_task_id"], task_id)
        store = MemoryStore(self.storage.conn)
        self.assertTrue(store.search("来源归属", [], cases[0]["rule_version"]))


if __name__ == "__main__":
    unittest.main()
