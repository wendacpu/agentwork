import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, ModelReply
from agent_system.pipeline import Pipeline
from agent_system.pipeline import REFUTED_EVIDENCE_LIMIT, _apply_verification_constraints, _decide_candidate, _mechanical_check, _normalize_boundary, _normalize_candidates, _normalize_highlights, _normalize_reviews, _normalize_understanding
from agent_system.prompts import BOUNDARY_PROMPT, HIGHLIGHT_PROMPT, REVIEW_PROMPT, UNDERSTAND_PROMPT
from agent_system.rules import load_common_rule_ids, load_rule_bundle, rubric
from agent_system.rules.loader import load_domain_rules, load_form_rules
from agent_system.schemas import validate_review
from agent_system.storage import Storage
from agent_system.memory import MemoryStore
from agent_system.types import CaseRecord, Claim, Understanding


ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verification_search_and_audit_are_persisted(self):
        class Search:
            def __init__(self): self.calls = []
            def search(self, query, sites, limit):
                self.calls.append((query, sites, limit))
                return [{"url": "https://news.cn/a", "title": "A", "snippet": "x"}]
            def fetch(self, url): raise AssertionError("not used")
        class VerifyAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "verify":
                    return ModelReply("ok", {"claim_id": "c1", "verdict": "supported", "sources": ["https://news.cn/a"], "confidence": 0.9, "query": "断言"})
                return super().complete(**kwargs)
        video = self.dataset.get("BV1Ltsf6xEpV")
        video.domain = {"一级": "资讯", "二级": "时事社会"}
        provider = Search()
        config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name, verification_enabled=True, verification_provider="wsa")
        pipeline = Pipeline(self.dataset, self.storage, VerifyAdapter(), config, search_provider=provider)
        understanding = Understanding("资讯", [], [Claim("c1", "断言", "observable", [])], [], [])
        task_id = pipeline.prepare(video.video_id)
        state = {}
        summary = pipeline._verify_understanding(task_id, video, understanding, state)
        self.assertEqual(summary["level"], "low")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(state["verification"][0]["sources"], ["https://news.cn/a"])
        self.assertEqual(state["understanding"]["claims"][0]["verification"]["verdict"], "supported")
        self.assertTrue(any(event["event_type"] == "verification_complete" for event in self.storage.events(task_id)))
        self.assertTrue(any(event["event_type"] == "checkpoint_saved" and event["payload"].get("stage") == "verification" for event in self.storage.events(task_id)))

    def test_distinct_urls_on_same_host_do_not_trigger_high_risk(self):
        class Search:
            def search(self, query, sites, limit):
                return [{"url": "https://news.cn/a"}, {"url": "https://news.cn/reprint/b"}]
            def fetch(self, url): raise AssertionError("not used")
        class VerifyAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "verify":
                    return ModelReply("ok", {"claim_id": "c1", "verdict": "refuted", "sources": ["https://news.cn/a", "https://news.cn/reprint/b"], "confidence": 0.99, "query": "claim"})
                return super().complete(**kwargs)
        video = self.dataset.get("BV1Ltsf6xEpV")
        video.domain = {"一级": "资讯", "二级": "时事社会"}
        config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name, verification_enabled=True, verification_provider="wsa")
        pipeline = Pipeline(self.dataset, self.storage, VerifyAdapter(), config, search_provider=Search())
        task_id = pipeline.prepare(video.video_id)
        understanding = Understanding("资讯", [], [Claim("c1", "断言", "observable", [])], [], [])
        summary = pipeline._verify_understanding(task_id, video, understanding, {})
        self.assertEqual(summary["level"], "low")
        result = understanding.claims[0].verification
        self.assertEqual(result.verdict, "refuted")
        self.assertTrue(result.downgraded)

    def test_high_risk_verification_holds_before_generation(self):
        class Search:
            def search(self, query, sites, limit):
                return [{"url": "https://news.cn/a"}, {"url": "https://xinhuanet.com/b"}]
            def fetch(self, url): raise AssertionError("not used")
        class VerifyAdapter(MockAdapter):
            def __init__(self): self.generate_calls = 0
            def complete(self, **kwargs):
                if kwargs["task"] == "verify":
                    return ModelReply("ok", {"claim_id": "c1", "verdict": "refuted", "sources": ["https://news.cn/a", "https://xinhuanet.com/b"], "confidence": 0.95, "query": "claim"})
                if kwargs["task"] in {"generate", "rewrite"}: self.generate_calls += 1
                return super().complete(**kwargs)
        adapter = VerifyAdapter()
        self.config.verification_enabled = True
        self.config.verification_provider = "wsa"
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config, search_provider=Search())
        task_id = pipeline.prepare("BV1Ltsf6xEpV")
        pipeline.execute(task_id)
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"])
        self.assertEqual(row["status"], "pending_human")
        self.assertEqual(state["verification_risk"]["level"], "high")
        self.assertEqual(state["verification_hold"]["claim_ids"], ["c1"])
        self.assertEqual(adapter.generate_calls, 0)
        self.assertTrue(any(event["event_type"] == "checkpoint_saved" and event["payload"].get("stage") == "verification_hold" for event in self.storage.events(task_id)))
        self.assertTrue(any(event["event_type"] == "verification_human_hold" for event in self.storage.events(task_id)))

    def test_mock_adapter_supports_verify_so_full_run_reaches_generation(self):
        """离线全链路：检索被调用且审计落库，mock 核查保守返回 unverifiable 不阻断生成。"""
        class Search:
            def search(self, query, sites, limit):
                return [{"url": "https://news.cn/a", "title": "A", "snippet": "x"}]
            def fetch(self, url): raise AssertionError("not used")
        self.config.verification_enabled = True
        self.config.verification_provider = "wsa"
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config, search_provider=Search())
        task_id = pipeline.run("BV11Ssf6gEUV")
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"])
        self.assertEqual(row["status"], "pending_human")
        self.assertTrue(state["verification"])
        self.assertEqual(state["verification"][0]["verdict"], "unverifiable")
        self.assertEqual(state["verification_risk"]["level"], "low")
        self.assertTrue(state["candidates"])

    def test_asset_audit_and_index(self):
        audit = self.dataset.audit()
        self.assertEqual(audit["count"], 100)
        self.assertEqual(audit["missing_assets"], [])
        video = self.dataset.get("BV1Ltsf6xEpV")
        self.assertTrue(video.asset_paths["video"].endswith("BV1Ltsf6xEpV.mp4"))
        self.assertTrue(any(x.quality == "raw_ocr" for x in video.evidence))

    def test_red_dot_rule_bundle_routes_domain_and_form(self):
        bundle = load_rule_bundle(domain={"一级": "资讯", "二级": "时事社会"}, form="资讯播报")
        self.assertEqual(bundle.version, "red-dot-v0.2-modular")
        self.assertEqual(bundle.rule_ids, ["U", "V04", "V11", "F11"])
        self.assertEqual({x["scope"] for x in bundle.match_trace}, {"domain", "form"})

    def test_mock_run_records_rule_bundle_and_highlights(self):
        task_id = self.pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(state["rules"]["version"], "red-dot-v0.2-modular")
        self.assertTrue(state["highlights"])
        self.assertIn("role", state["highlights"][0])

    def test_complete_mock_run_and_state(self):
        task_id = self.pipeline.run("BV1Ltsf6xEpV")
        row = self.storage.get_task(task_id)
        self.assertEqual(row["status"], "pending_human")
        state = json.loads(row["state_json"])
        self.assertTrue(state["uncalibrated"])
        self.assertTrue(state["candidates"])
        self.assertEqual(len(state["candidates"]), 3)
        self.assertTrue(state["reviews"])
        self.assertTrue(all(not review.get("validation_errors") for review in state["reviews"]))
        self.assertTrue(self.storage.events(task_id))

    def test_target_count_conflict_stops_before_generation(self):
        """三槽是唯一受支持契约：配置成别的数量时必须明确停下，而不是产出少槽候选。"""
        config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name, target_count=2)
        pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), config)
        with self.assertRaisesRegex(ValueError, "target_count=3"):
            pipeline.run("BV1Ltsf6xEpV")
        # 已有任务（例如旧配置的 resume/execute）也必须停得下来并留痕。
        task_id = self.storage.create_task("BV1Ltsf6xEpV", asdict(config))
        pipeline.execute(task_id)
        row = self.storage.get_task(task_id)
        state = json.loads(row["state_json"])
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(state["stop_reason"], "TARGET_COUNT_UNSUPPORTED")
        self.assertEqual(state["config_conflict"], {"target_count": 2, "supported": 3})
        self.assertFalse(state.get("candidates"))

    def test_unconfirmed_case_is_not_retrieved(self):
        store = MemoryStore(self.storage.conn)
        store.create_draft(CaseRecord("draft-1", "expression", "low_density", "bad", "good", "test", "task-x", "temporary-v1", False, ["temporary-v1"]))
        self.assertEqual(store.search("low_density", ["low_density"], "temporary-v1"), [])
        store.confirm("draft-1")
        self.assertEqual(len(store.search("low_density", ["low_density"], "temporary-v1")), 1)

    def test_failed_first_review_triggers_bounded_rewrite(self):
        class RepairAdapter(MockAdapter):
            def __init__(self): self.review_calls = 0
            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    self.review_calls += 1
                    reply = super().complete(**kwargs)
                    evidence_ids = [item["evidence_id"] for item in kwargs["user"].get("evidence", []) if item.get("evidence_id")]
                    for item in reply.data["reviews"]:
                        item["gates"]["G1"]["evidence_ids"] = evidence_ids[:1]
                        item["gates"]["G3"]["evidence_ids"] = evidence_ids[:1]
                        item["gates"]["G2"]["rule_ids"] = ["P1"]
                        if self.review_calls == 1:
                            # 真实 R01 按 schema 必填 delivery_supported/core_covered；
                            # OVER_PROMISE（承诺超出视频交付）即 delivery_supported=False，
                            # 属 G3 硬伤，必须走重写而不是人审。
                            item["gates"]["G3"] = {"status": "revise", "reason": "看点承诺偏宽", "evidence_ids": [], "rule_ids": [], "delivery_supported": False, "core_covered": True}
                            item["issues"] = [{"issue_id": f'{item["candidate_id"]}-G3_OVER_PROMISE', "primary_dimension": "G3", "issue_code": "G3_OVER_PROMISE", "severity": "medium", "repairable": True, "problem_span": "标题承诺", "evidence_ids": [], "rule_ids": [], "reason": "承诺超出视频交付", "action": "收窄承诺"}]
                    return reply
                return super().complete(**kwargs)
        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        adapter = RepairAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(self.storage.get_task(task_id)["status"], "pending_human")
        self.assertEqual(state["rewrite_round"], 1)
        self.assertTrue(any(e["event_type"] == "highlight_reselected" for e in self.storage.events(task_id)))
        self.assertTrue(state["candidates"])

    def test_overlong_candidate_is_repaired_even_when_another_candidate_is_ready(self):
        """真实故障：见一条 ready 就 handoff，超字数的可修候选被原样丢掉。

        2026-11-01 canary 三条视频里五条候选只因超 1–2 字被判 repairable，
        `_route_candidates` 见到 cand-1 合格就 handoff，max_rewrite_rounds 一次没用上。
        """
        class CompressAdapter(MockAdapter):
            def __init__(self):
                self.compress_calls = 0
                self.rewrite_modes = []

            def complete(self, **kwargs):
                if kwargs["task"] == "generate":
                    reply = super().complete(**kwargs)
                    # 第一条守预算（会判 ready），第二条故意超长（只会被标 repairable）
                    items = reply.data["candidates"]
                    # 用纯中文、不含数字，避免顺带触发 G4_NUMERIC_CONFLICT——那个不属于可压缩缺陷
                    items[1]["headline"] = "这条测试标题故意写得很长以便触发长度缺陷需要缩短篇幅"
                    return reply
                if kwargs["task"] == "rewrite":
                    self.rewrite_modes.append(kwargs["user"].get("rewrite_mode"))
                    self.compress_calls += 1
                    reply = super().complete(**kwargs)
                    # 压到预算内
                    for item in reply.data["candidates"]:
                        item["headline"] = "压短后的标题"
                    return reply
                return super().complete(**kwargs)

        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        adapter = CompressAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        events = self.storage.events(task_id)

        # 压缩通道确实被调用，且用的是压缩模式，不是语义重写
        self.assertTrue(adapter.compress_calls >= 1)
        self.assertIn("压缩", adapter.rewrite_modes)
        self.assertTrue(any(e["event_type"] == "mechanical_repair" for e in events))
        repair = next(e for e in events if e["event_type"] == "mechanical_repair")
        self.assertEqual(repair["payload"]["mode"], "压缩")
        self.assertIn("G4_LENGTH_EXCEEDED", repair["payload"]["defects"])
        # 独立预算：压缩不占用 max_rewrite_rounds
        self.assertEqual(state.get("rewrite_round", 0), 0)

    def test_attribution_defect_is_not_sent_to_compress_pass(self):
        """缺归属是语义缺陷：压字改不出来，必须留在语义重写通道里。"""
        class AttributionAdapter(MockAdapter):
            def __init__(self):
                self.compress_modes = []

            def complete(self, **kwargs):
                if kwargs["task"] == "review":
                    reply = super().complete(**kwargs)
                    for item in reply.data["reviews"]:
                        item["gates"]["G1"] = {"status": "revise", "reason": "缺归属", "evidence_ids": [], "rule_ids": []}
                        item["issues"] = [{"issue_id": f'{item["candidate_id"]}-G1', "primary_dimension": "G1",
                                           "issue_code": "G1_SOURCE_ATTRIBUTION_MISSING", "severity": "high",
                                           "repairable": True, "problem_span": "标题", "evidence_ids": [], "rule_ids": [],
                                           "reason": "可识别信源缺少转述关系", "action": "补归属"}]
                    return reply
                if kwargs["task"] == "rewrite":
                    self.compress_modes.append(kwargs["user"].get("rewrite_mode"))
                return super().complete(**kwargs)

        self.config.extra["business_policy"] = {"status": "approved", "version": "test-v1", "rules": [{"rule_id": "P1", "text": "测试规则"}]}
        adapter = AttributionAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        events = self.storage.events(task_id)
        self.assertNotIn("压缩", adapter.compress_modes)
        self.assertFalse(any(e["event_type"] == "mechanical_repair" for e in events))

    def test_compressible_defects_requires_all_gates_pass(self):
        from agent_system.pipeline import _compressible_defects
        source = {"mechanical_checks": {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED", "repairable": True}]}}
        decision = {"status": "rewrite"}
        ok_review = {"gates": {g: {"status": "pass"} for g in ("G1", "G2", "G3")}}
        self.assertEqual(len(_compressible_defects(source, decision, ok_review)), 1)
        # 任一语义闸未 pass 就不许走压缩：压缩会顺手改掉语义
        for gate in ("G1", "G2", "G3"):
            broken = {"gates": {g: {"status": "pass"} for g in ("G1", "G2", "G3")}}
            broken["gates"][gate] = {"status": "revise"}
            self.assertEqual(_compressible_defects(source, decision, broken), [])
        # 超长与缺限定混合时允许压缩，但仍须重新评审
        mixed = {"mechanical_checks": {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED"}, {"code": "G1_MISSING_LIMIT"}]}}
        self.assertEqual(len(_compressible_defects(mixed, decision, ok_review)), 2)
        # 状态不是 rewrite（例如已 ready）不该被再修一遍
        self.assertEqual(_compressible_defects(source, {"status": "ready_for_human_review"}, ok_review), [])

    def test_question_mark_at_end_is_mechanically_rejected(self):
        class QuestionAdapter(MockAdapter):
            def complete(self, **kwargs):
                if kwargs["task"] == "generate":
                    return ModelReply("ok", {"candidates": [{
                        "candidate_id": "q1",
                        "headline": "学生共用针头？",
                        "highlight_id": "h1",
                        "claim_ids": ["c1"],
                        "expression_strategy": "suspense",
                        "used_case_ids": [],
                        "generation_round": 0,
                    }]})
                return super().complete(**kwargs)

        pipeline = Pipeline(self.dataset, self.storage, QuestionAdapter(), self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        question_candidate = next(c for c in state["candidates"] if c["candidate_id"] == "q1")
        self.assertIn("G4_ENDING_QUESTION_MARK", [x["code"] for x in question_candidate["mechanical_checks"]["errors"]])
        self.assertEqual(next(r for r in state["reviews"] if r["candidate_id"] == "q1")["decision"], "needs_review")

    def test_meta_discourse_and_verification_conclusion_are_mechanically_rejected(self):
        """载体元话语（视频称/画面却是/标题说/据视频所述）和校验结论（与…不符）要机械拦截。"""
        cases = {
            "视频提到游客为周边花钱": "G4_META_PHRASE",
            "据视频所述陆地最长直线穿越15国": "G4_META_PHRASE",
            "标题说中韩红毯差别大": "G4_META_PHRASE",
            "画面却是排队店面": "G4_META_PHRASE",
            "事实与画面不符": "G4_VERIFICATION_CONCLUSION",
            "据咪咕体育 费兰补时破门": "G4_INCOMPLETE_ATTRIBUTION",
        }
        for headline, expected in cases.items():
            candidate = {"candidate_id": "a1-1", "headline": headline, "highlight_id": "h1", "claim_ids": ["c1"], "evidence_ids": ["e1"], "expression_strategy": "直接描述"}
            result = _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=30)
            codes = [x["code"] for x in result["errors"]]
            self.assertIn(expected, codes, f"{headline} 应命中 {expected}，实际 {codes}")
        clean = {"candidate_id": "a1-2", "headline": "游客为可爱周边和打卡花钱", "highlight_id": "h1", "claim_ids": ["c1"], "evidence_ids": ["e1"], "expression_strategy": "直接描述"}
        clean_result = _mechanical_check(clean, allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=30)
        self.assertEqual([x["code"] for x in clean_result["errors"]], [])

    def test_similar_candidates_are_deduped(self):
        from agent_system.pipeline import _dedupe_similar_candidates
        cands = [
            {"candidate_id": "a1", "headline": "据咪咕体育费兰补时破门"},
            {"candidate_id": "a2", "headline": "据咪咕体育费兰补时破门啦"},
            {"candidate_id": "a3", "headline": "西班牙1-0领先阿根廷"},
        ]
        kept = _dedupe_similar_candidates(cands)
        self.assertEqual([c["candidate_id"] for c in kept], ["a1", "a3"])

    def test_boundary_normalizes_mapping_schema(self):
        boundary = _normalize_boundary({"BoundarySet": {
            "c1": {"status": "usable", "statement": "学生共用针头", "evidence_refs": ["e1"]},
            "c2": {"status": "limited", "statement": "涉及78名学生", "required_limits": ["据通报"]},
        }})
        self.assertEqual([x.claim_id for x in boundary.decisions], ["c1", "c2"])
        self.assertEqual([x.decision for x in boundary.decisions], ["usable", "limited"])

    def test_review_batch_candidates_are_normalized(self):
        reviews = _normalize_reviews({"review_batch": {"candidates": [{"candidate_id": "c1", "issues": []}]}}, ["c1"])
        self.assertEqual(reviews[0]["candidate_id"], "c1")

    def test_malformed_review_batch_errors_are_bounded_and_fail_closed(self):
        ids = ["c1", "c2", "c3"]
        valid = {
            "candidate_id": "c3", "expected_takeaway": "内容",
            "gates": {key: {"status": "pass", "reason": "核对", "evidence_ids": ["e1"], "rule_ids": []} for key in ("G1", "G2", "G3")},
            "quality": {key: {"score": 3, "reason": "清楚"} for key in ("Q1", "Q2", "Q3")},
            "issues": [], "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
            "memory_query": {"problem_types": [], "query": ""},
        }
        reviews = _normalize_reviews({"reviews": [valid] + ["   :"] * 2253}, ids)
        for review in reviews:
            errors = validate_review(review, evidence_ids={"e1"}, rule_ids=set(), issue_codes=set(), policy_rule_ids=set())
            self.assertIn("non-object review item: 2253", errors)
            self.assertLess(len(errors), 20)
            decision = _decide_candidate(review, {"status": "pass", "errors": []}, schema_errors=errors, business_policy={"status": "approved", "rules": []})
            self.assertEqual(decision["status"], "needs_review")
        self.assertIn("review missing for candidate", reviews[0]["_normalization_errors"])
        self.assertIn("review missing for candidate", reviews[1]["_normalization_errors"])
        clean = _normalize_reviews({"reviews": [valid]}, ["c3"])[0]
        self.assertEqual(validate_review(clean, evidence_ids={"e1"}, rule_ids=set(), issue_codes=set(), policy_rule_ids=set()), [])

    def test_highlight_alias_schema_is_normalized(self):
        highlights = _normalize_highlights({"primary_highlight": {
            "highlight_id": "H1", "viewpoint": "首相否认报道", "what_user_can_see": "听到首相回应",
            "claim_ids": ["c1"], "evidence_ids": ["e1"], "required_limits": ["保留归属"],
        }, "alternative_highlights": [{
            "highlight_id": "H2", "viewpoint": "通话时长", "claim_ids": ["c2"], "evidence_ids": ["e2"],
        }]}, {"c1", "c2"}, [])
        self.assertEqual([x.highlight_id for x in highlights], ["H1", "H2"])
        self.assertEqual(highlights[0].value, "听到首相回应")
        self.assertEqual(highlights[0].required_qualifiers, ["保留归属"])

    def test_review_alias_schema_is_normalized(self):
        reviews = _normalize_reviews({"reviews": [{
            "candidate_id": "c1", "viewer_expectation": "读者会认为首相否认该报道",
            "quality_scores": {"Q1": 2, "Q2": 3, "Q3": 2},
            "quality_reasons": {"Q1": "清楚", "Q2": "具体", "Q3": "可信"},
            "memory_retrieval_query": "来源归属", "issues": [{"issue_code": "G1_MISSING_LIMIT"}],
        }]}, ["c1"])
        review = reviews[0]
        self.assertEqual(review["expected_takeaway"], "读者会认为首相否认该报道")
        self.assertEqual(review["quality"]["Q2"], {"score": 3, "reason": "具体"})
        self.assertEqual(review["memory_query"]["query"], "来源归属")
        self.assertEqual(review["issues"][0]["issue_id"], "c1-issue-1")
        self.assertIn("U-V03", load_common_rule_ids())

    def test_rule_sources_are_modular_and_complete(self):
        self.assertEqual(set(load_domain_rules()), {f"V{i:02d}" for i in range(1, 17)})
        self.assertEqual(set(load_form_rules()), {f"F{i:02d}" for i in range(1, 15)})
        bundle = load_rule_bundle(domain={"一级": "资讯", "二级": "时事社会"}, form="资讯播报")
        self.assertEqual(bundle.rule_ids, ["U", "V04", "V11", "F11"])
        self.assertEqual(len(bundle.sources), 3)

    def test_claim_model_keeps_orthogonal_axes_and_legacy_compatibility(self):
        current = _normalize_understanding({"claims": [{
            "claim_id": "c1", "text": "通报称涉及78名学生", "claim_type": "source_claim",
            "evidence_status": "limited", "original_source": {"name": "调查组", "type": "official"},
            "transmission_path": [{"actor": "视频作者", "mode": "relay"}], "temporal_status": "historical",
            "assertion_permission": "attributed", "entities": [{"name": "调查组", "role": "source"}],
        }]})
        claim = current.claims[0]
        self.assertEqual((claim.claim_type, claim.evidence_status, claim.assertion_permission), ("source_claim", "limited", "attributed"))
        self.assertEqual(claim.original_source["name"], "调查组")
        self.assertEqual(claim.entities[0]["role"], "source")
        legacy = _normalize_understanding({"claims": [{"claim_id": "c2", "text": "作者称降温", "kind": "source_asserted", "assertion_judgment": "source_asserted"}]})
        self.assertEqual(legacy.claims[0].claim_type, "source_claim")
        self.assertEqual(legacy.claims[0].evidence_status, "limited")
        self.assertEqual(legacy.claims[0].assertion_permission, "attributed")

    def test_understanding_accepts_real_model_candidate_claim_schema(self):
        understanding = _normalize_understanding({"type": "understanding", "video": {"format": "资讯播报"}, "candidate_claims": [{
            "claim_id": "c1", "claim_text": "斯特勒称已要求更正报道", "claim_type": "source_statement_paraphrase",
            "evidence_status": "uploader_metadata_text_unverified + raw_ocr_partial",
            "evidence_refs": ["e-source-description"], "original_source": "斯特勒",
            "transmission_path": ["斯特勒", "挪威TV 2", "RT今日俄罗斯"], "temporal_status": "past_reported",
            "assertion_permission": "allowed_as_attributed_paraphrase", "entities": [{"name": "斯特勒", "roles": ["source"]}],
        }]})
        claim = understanding.claims[0]
        self.assertEqual(understanding.main_format, "资讯播报")
        self.assertEqual(claim.text, "斯特勒称已要求更正报道")
        self.assertEqual(claim.claim_type, "source_claim")
        self.assertEqual(claim.evidence_status, "limited")
        self.assertEqual(claim.assertion_permission, "attributed")
        self.assertEqual(claim.temporal_status, "historical")

    def test_understanding_normalizes_chinese_claim_labels(self):
        understanding = _normalize_understanding({"candidate_claims": [
            {"claim_id": "c1", "claim_text": "标题所述事件", "claim_type": "标题陈述", "evidence_status": "source", "assertion_permission": "可作为视频标题内容引用；不得据此确认报道真伪", "temporal_status": "已发布的标题陈述（现在时）"},
            {"claim_id": "c2", "claim_text": "作者的推断", "claim_type": "观点/推断", "evidence_status": "model_derived_uncalibrated", "assertion_permission": "不得当作事实", "temporal_status": "评论/推断（非事件时间）"},
        ]})
        self.assertEqual([x.claim_type for x in understanding.claims], ["source_claim", "inference"])
        self.assertEqual(understanding.claims[0].assertion_permission, "qualified")
        self.assertEqual(understanding.claims[0].evidence_status, "limited")
        actual_shape = _normalize_understanding({"Understanding": {"candidate_claims": [
            {"claim_id": "c3", "claim_text": "原报道内容", "claim_type": "source_statement", "evidence_status": "unverified_source_metadata", "assertion_permission": "attributed_paraphrase_only", "temporal_status": "past"},
            {"claim_id": "c8", "claim_text": "制作推断", "claim_type": "inference", "evidence_status": "model_derived_uncalibrated", "assertion_permission": "inference_only", "temporal_status": "present_in_video"},
        ]}})
        self.assertEqual(actual_shape.claims[0].assertion_permission, "attributed")
        self.assertEqual(actual_shape.claims[1].assertion_permission, "forbidden")
        self.assertEqual(actual_shape.claims[1].temporal_status, "current")

    def test_node_normalizers_accept_schema_named_wrappers(self):
        understanding = _normalize_understanding({"Understanding": {"candidate_claims": [{"claim_id": "c1", "claim_text": "可引用事实", "claim_type": "observation", "evidence_refs": ["e1"]}]}})
        boundary = _normalize_boundary({"BoundarySet": {"claims": [{"claim_id": "c1", "decision": "usable", "evidence_refs": ["e1"]}]}})
        highlights = _normalize_highlights({"HighlightSet": {"primary_highlight": {"highlight_id": "h1", "viewpoint": "看点", "claim_ids": ["c1"], "evidence_ids": ["e1"]}}}, {"c1"}, [])
        candidates = _normalize_candidates({"AgentDraft": {"candidates": [{"candidate_id": "a1", "title": "标题", "highlight_id": "h1", "claim_ids": ["c1"], "evidence_ids": ["e1"]}]}}, 0, None)
        reviews = _normalize_reviews({"ReviewResultBatch": {"reviews": [{"candidate_id": "a1"}]}}, ["a1"])
        self.assertEqual(len(understanding.claims), 1)
        self.assertEqual(len(boundary.decisions), 1)
        self.assertEqual(highlights[0].highlight_id, "h1")
        self.assertEqual(candidates[0]["headline"], "标题")
        self.assertEqual(reviews[0]["candidate_id"], "a1")

    def test_boundary_preserves_source_and_attribution_as_separate_fields(self):
        boundary = _normalize_boundary({"decisions": [{
            "claim_id": "c1", "decision": "limited", "statement": "调查组称涉及78人",
            "claim_type": "source_claim", "evidence_status": "limited",
            "original_source": {"name": "调查组", "type": "official"},
            "transmission_path": [{"actor": "视频作者", "mode": "relay"}],
            "temporal_status": "historical", "assertion_permission": "attributed",
            "attribution_required": False,
        }]})
        decision = boundary.decisions[0]
        self.assertFalse(decision.attribution_required)
        self.assertEqual(decision.original_source["type"], "official")
        self.assertEqual(decision.transmission_path[0]["actor"], "视频作者")

    def test_dataset_adds_unverified_platform_description_as_evidence(self):
        video = self.dataset.get("BV11Ssf6gEUV")
        source_description = next((item for item in video.evidence if item.evidence_id == "e-source-description"), None)
        self.assertIsNotNone(source_description)
        self.assertEqual(source_description.quality, "unverified_source_metadata")

    def test_prompts_require_description_caption_and_semantic_units(self):
        for field_name in ("description", "caption"):
            self.assertIn(field_name, UNDERSTAND_PROMPT)
            self.assertIn(field_name, HIGHLIGHT_PROMPT)
        # 三个描述性输入被明确区分，而不是混为"用户/上传者文本"
        self.assertIn("searchDesc", UNDERSTAND_PROMPT)
        self.assertIn("意图分析", UNDERSTAND_PROMPT)
        self.assertIn("上传者", UNDERSTAND_PROMPT)
        self.assertIn("均可帮助理解上下文", UNDERSTAND_PROMPT)
        self.assertIn("semantic_units", UNDERSTAND_PROMPT)
        # 措辞必须是「顶层数组」：旧句子说 candidate_claims 是「语义单元内」的原子事实层，
        # 模型据此只在单元里写 claim_ids、省掉 claim 对象，导致 claims 为空、
        # 下游自行编造 claim_id、全批被判悬空引用（真实故障）。
        self.assertIn("**顶层** candidate_claims 数组", UNDERSTAND_PROMPT)
        self.assertIn("只能引用该数组中已定义的 claim_id", UNDERSTAND_PROMPT)
        self.assertNotIn("candidate_claims 是语义单元内的原子事实层", UNDERSTAND_PROMPT)
        self.assertIn("unit_decisions", BOUNDARY_PROMPT)
        self.assertIn("unit_id", HIGHLIGHT_PROMPT)
        self.assertIn("看点直接来源于输入 semantic_units", HIGHLIGHT_PROMPT)
        self.assertIn("不得把每句 ASR", HIGHLIGHT_PROMPT)
        self.assertIn("完整 ASR 话语链", HIGHLIGHT_PROMPT)
        # 语义单元的归纳证据约束（grounding）+ 印证源改一手证据
        self.assertIn("grounding", UNDERSTAND_PROMPT)
        self.assertIn("inferred", BOUNDARY_PROMPT)
        self.assertIn("speculative", HIGHLIGHT_PROMPT)
        self.assertIn("不能单独证明外部事实或未核验画面细节", UNDERSTAND_PROMPT)
        # 看点同时建模价值与钩子；主看点必须来自主旨单元（边角不压主线）
        self.assertIn("hook_type", HIGHLIGHT_PROMPT)
        # 提示词已改为「优先来自 kind=thesis」：action/support 在 thesis 无法成看点时允许退用
        self.assertIn("主看点优先代表整片核心意思", HIGHLIGHT_PROMPT)
        self.assertIn("钩子是否成立", REVIEW_PROMPT)

    def test_normalize_understanding_parses_semantic_units(self):
        raw = {
            "main_format": "资讯播报",
            "claims": [
                {"claim_id": "c1", "text": "第一句", "evidence_refs": ["e1"]},
                {"claim_id": "c2", "text": "第二句", "evidence_refs": ["e2"]},
            ],
            "semantic_units": [
                {"unit_id": "u1", "meaning": "两句合起来是一个因果", "kind": "causal",
                 "claim_ids": ["c1", "c2"], "evidence_ids": ["e1", "e2"],
                 "time_range": [1.0, 5.0], "role_in_video": "主旨",
                 "grounding": "inferred",
                 "corroboration": {"source": "description", "status": "consistent", "notes": "印证", "evidence_ids": ["e1"]}},
            ],
        }
        understanding = _normalize_understanding(raw)
        self.assertEqual(len(understanding.semantic_units), 1)
        u = understanding.semantic_units[0]
        self.assertEqual(u.unit_id, "u1")
        self.assertEqual(u.claim_ids, ["c1", "c2"])
        self.assertEqual(u.evidence_ids, ["e1", "e2"])
        self.assertEqual(u.time_range, (1.0, 5.0))
        self.assertEqual(u.role_in_video, "主旨")
        self.assertEqual(u.grounding, "inferred")
        self.assertEqual(u.corroboration["status"], "consistent")

    def test_normalize_understanding_falls_back_to_claim_units(self):
        raw = {
            "claims": [
                {"claim_id": "c1", "text": "第一句", "evidence_refs": ["e1"]},
                {"claim_id": "c2", "text": "第二句", "evidence_refs": ["e2"]},
            ],
        }
        understanding = _normalize_understanding(raw)
        self.assertEqual([u.unit_id for u in understanding.semantic_units], ["u1", "u2"])
        self.assertEqual(understanding.semantic_units[0].claim_ids, ["c1"])

    def test_normalize_understanding_grounding_fallback(self):
        raw = {
            "claims": [{"claim_id": "c1", "text": "第一句", "evidence_refs": ["e1"]}],
            "semantic_units": [
                {"unit_id": "u1", "meaning": "因果", "kind": "causal", "claim_ids": ["c1"], "evidence_ids": ["e1"], "grounding": "瞎写的"},
            ],
        }
        understanding = _normalize_understanding(raw)
        self.assertEqual(understanding.semantic_units[0].grounding, "unknown")

    def test_normalize_boundary_parses_unit_decisions(self):
        raw = {
            "decisions": [{"claim_id": "c1", "decision": "usable", "evidence_refs": ["e1"]}],
            "unit_decisions": [{"unit_id": "u1", "decision": "usable", "required_limits": [], "attribution_required": False, "reason": "可作看点来源"}],
        }
        boundary = _normalize_boundary(raw, {"c1": "第一句"})
        self.assertEqual(len(boundary.unit_decisions), 1)
        self.assertEqual(boundary.unit_decisions[0].unit_id, "u1")
        self.assertEqual(boundary.unit_decisions[0].decision, "usable")

    def test_normalize_highlights_binds_unit_id(self):
        raw = {"highlights": [{"highlight_id": "h1", "value": "看点", "claim_ids": ["c1"], "evidence_ids": ["e1"], "unit_id": "u1", "role": "main", "hook_type": "suspense"}]}
        highlights = _normalize_highlights(raw, {"c1"}, [])
        self.assertEqual(highlights[0].unit_id, "u1")
        self.assertEqual(highlights[0].hook_type, "suspense")

    def test_normalize_highlights_hook_type_fallback(self):
        raw = {"highlights": [{"highlight_id": "h1", "value": "看点", "claim_ids": ["c1"], "evidence_ids": ["e1"], "unit_id": "u1", "hook_type": "瞎写的"}]}
        highlights = _normalize_highlights(raw, {"c1"}, [])
        self.assertEqual(highlights[0].hook_type, "")

    def test_mock_run_propagates_semantic_units_through_state(self):
        task_id = self.pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        units = state["understanding"]["semantic_units"]
        self.assertTrue(units)
        self.assertEqual(units[0]["unit_id"], "u1")
        self.assertEqual(units[0]["grounding"], "grounded")
        self.assertEqual(state["boundary"]["unit_decisions"][0]["unit_id"], "u1")
        self.assertTrue(state["highlights"])
        self.assertIn("unit_id", state["highlights"][0])
        self.assertEqual(state["highlights"][0]["unit_id"], "u1")
        self.assertEqual(state["highlights"][0]["hook_type"], "information")

    def test_verification_refuted_claim_is_excluded_and_unit_downgraded(self):
        from types import SimpleNamespace
        refuted = SimpleNamespace(claim_id="c1", verification=SimpleNamespace(verdict="refuted", sources=["https://news.cn/a", "https://xinhuanet.com/b"], confidence=0.95, downgraded=False))
        ok = SimpleNamespace(claim_id="c2", verification=None)
        understanding = SimpleNamespace(claims=[refuted, ok], semantic_units=[
            SimpleNamespace(unit_id="u1", claim_ids=["c1"], grounding="grounded"),
            SimpleNamespace(unit_id="u2", claim_ids=["c2"], grounding="grounded"),
        ])
        boundary = SimpleNamespace(decisions=[
            SimpleNamespace(claim_id="c1", decision="usable", reason="原判", required_limits=[]),
            SimpleNamespace(claim_id="c2", decision="usable", reason="原判", required_limits=[]),
        ])
        constraints = _apply_verification_constraints(understanding, boundary)
        self.assertEqual(constraints["excluded"], ["c1"])
        self.assertEqual(constraints["limited"], [])
        self.assertEqual(boundary.decisions[0].decision, "excluded")
        self.assertIn("refuted", boundary.decisions[0].reason)
        self.assertEqual(boundary.decisions[1].decision, "usable")
        self.assertEqual(understanding.semantic_units[0].grounding, "speculative")
        self.assertEqual(understanding.semantic_units[1].grounding, "grounded")

    def test_weak_refutation_keeps_claim_but_requires_qualified_expression(self):
        """单源反驳证据不足：不排除、也不假装没发生，降级为 limited 并写入限定语。"""
        from types import SimpleNamespace
        weak = SimpleNamespace(claim_id="c1", verification=SimpleNamespace(verdict="refuted", sources=["https://news.cn/a"], confidence=0.95, downgraded=True))
        understanding = SimpleNamespace(claims=[weak], semantic_units=[
            SimpleNamespace(unit_id="u1", claim_ids=["c1"], grounding="grounded"),
        ])
        boundary = SimpleNamespace(decisions=[
            SimpleNamespace(claim_id="c1", decision="usable", reason="原判", required_limits=["原有限定"]),
        ])
        constraints = _apply_verification_constraints(understanding, boundary)
        self.assertEqual(constraints["excluded"], [])
        self.assertEqual(constraints["limited"], ["c1"])
        self.assertEqual(boundary.decisions[0].decision, "limited")
        self.assertIn("原有限定", boundary.decisions[0].required_limits)
        self.assertIn(REFUTED_EVIDENCE_LIMIT, boundary.decisions[0].required_limits)
        self.assertIn("证据不足", boundary.decisions[0].reason)
        self.assertEqual(understanding.semantic_units[0].grounding, "inferred")

    def test_low_confidence_refutation_is_treated_as_weak(self):
        from types import SimpleNamespace
        weak = SimpleNamespace(claim_id="c1", verification=SimpleNamespace(verdict="refuted", sources=["https://news.cn/a", "https://xinhuanet.com/b"], confidence=0.5, downgraded=False))
        understanding = SimpleNamespace(claims=[weak], semantic_units=[])
        boundary = SimpleNamespace(decisions=[SimpleNamespace(claim_id="c1", decision="usable", reason="", required_limits=[])])
        constraints = _apply_verification_constraints(understanding, boundary)
        self.assertEqual(constraints["limited"], ["c1"])
        self.assertEqual(boundary.decisions[0].decision, "limited")

    def test_partial_refuted_unit_downgrades_to_inferred(self):
        from types import SimpleNamespace
        refuted = SimpleNamespace(claim_id="c1", verification=SimpleNamespace(verdict="refuted", sources=[], confidence=0.0, downgraded=True))
        supported = SimpleNamespace(claim_id="c2", verification=SimpleNamespace(verdict="supported"))
        understanding = SimpleNamespace(claims=[refuted, supported], semantic_units=[
            SimpleNamespace(unit_id="u1", claim_ids=["c1", "c2"], grounding="grounded"),
        ])
        boundary = SimpleNamespace(decisions=[
            SimpleNamespace(claim_id="c1", decision="usable", reason="", required_limits=[]),
            SimpleNamespace(claim_id="c2", decision="usable", reason="", required_limits=[]),
        ])
        _apply_verification_constraints(understanding, boundary)
        self.assertEqual(understanding.semantic_units[0].grounding, "inferred")

    def test_no_refuted_claims_keeps_state_unchanged(self):
        from types import SimpleNamespace
        understanding = SimpleNamespace(claims=[SimpleNamespace(claim_id="c1", verification=None)], semantic_units=[])
        boundary = SimpleNamespace(decisions=[SimpleNamespace(claim_id="c1", decision="usable", reason="")])
        refuted_ids = _apply_verification_constraints(understanding, boundary)
        self.assertEqual(refuted_ids, {"excluded": [], "limited": []})
        self.assertEqual(boundary.decisions[0].decision, "usable")

    def test_issue_dimension_must_be_unique_and_match_code(self):
        review = {
            "candidate_id": "c1", "expected_takeaway": {"subject": "学生", "content_nature": "事件", "viewing_promise": "了解情况"},
            "gates": {g: {"status": "pass", "reason": "符合", "evidence_ids": ["e1"] if g in {"G1", "G3"} else [], "rule_ids": []} for g in ("G1", "G2", "G3")},
            "quality": {q: {"score": 2, "reason": "达到基本要求"} for q in ("Q1", "Q2", "Q3")},
            "issues": [{"issue_id": "i1", "primary_dimension": "Q1", "issue_code": "Q1_UNCLEAR", "severity": "low", "repairable": True, "problem_span": "这里", "evidence_ids": [], "rule_ids": [], "reason": "指代不明", "action": "补充对象"}],
            "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []}, "memory_query": {"problem_types": [], "query": ""},
        }
        errors = validate_review(review, evidence_ids={"e1"}, rule_ids=set(rubric()["rule_ids"]), issue_codes=set(rubric()["issue_codes"]), policy_rule_ids=set())
        self.assertFalse(any("primary_dimension" in error for error in errors))
        review["issues"][0]["primary_dimension"] = "Q2"
        errors = validate_review(review, evidence_ids={"e1"}, rule_ids=set(rubric()["rule_ids"]), issue_codes=set(rubric()["issue_codes"]), policy_rule_ids=set())
        self.assertIn("issue primary_dimension must match issue_code prefix", errors)

    def test_reported_contradiction_requires_dispute_mark_in_title(self):
        from types import SimpleNamespace
        decision = SimpleNamespace(claim_id="c1", decision="limited", required_limits=[REFUTED_EVIDENCE_LIMIT])
        common = dict(allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=20, claim_decisions=[decision])
        candidate = {"candidate_id": "a1", "headline": "斯特勒称通话约90分钟", "claim_ids": ["c1"], "highlight_id": "h1", "evidence_ids": ["e1"]}
        result = _mechanical_check(candidate, **common)
        codes = [e["code"] for e in result["errors"]]
        self.assertIn("G1_MISSING_DISPUTE_MARK", codes)
        self.assertEqual(result["status"], "fail")
        candidate["headline"] = "通话时长存在争议"
        result = _mechanical_check(candidate, **common)
        self.assertNotIn("G1_MISSING_DISPUTE_MARK", [e["code"] for e in result["errors"]])

    def test_plain_attribution_limit_does_not_require_dispute_mark(self):
        from types import SimpleNamespace
        decision = SimpleNamespace(claim_id="c1", decision="limited", required_limits=["须保留归属：斯特勒称"])
        candidate = {"candidate_id": "a1", "headline": "斯特勒称通话约90分钟", "claim_ids": ["c1"], "highlight_id": "h1", "evidence_ids": ["e1"]}
        result = _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=20, claim_decisions=[decision])
        self.assertNotIn("G1_MISSING_DISPUTE_MARK", [e["code"] for e in result["errors"]])

    def test_decision_thresholds_are_per_dimension_and_sum_only_sorts(self):
        review = {"candidate_id": "c1", "gates": {g: {"status": "pass", "reason": "pass"} for g in ("G1", "G2", "G3")}, "quality": {"Q1": {"score": 1}, "Q2": {"score": 3}, "Q3": {"score": 3}}, "issues": []}
        result = _decide_candidate(review, {"status": "pass", "errors": []}, schema_errors=[], business_policy={"status": "approved", "rules": [{"rule_id": "P1"}]})
        self.assertEqual(result["status"], "rewrite")
        self.assertEqual(result["quality_sum"], 7)

    def test_missing_whole_claim_retries_boundary_and_records_second_omission(self):
        class OmitClaimAdapter(MockAdapter):
            def __init__(self):
                self.boundary_calls = 0
                self.omitted = None

            def complete(self, **kwargs):
                reply = super().complete(**kwargs)
                if kwargs['task'] == 'boundary':
                    self.boundary_calls += 1
                    claims = kwargs['user']['claims']
                    self.omitted = claims[-1]['claim_id']
                    reply.data = {'decisions': [{'claim_id': c['claim_id'], 'decision': 'usable'} for c in claims[:-1]]}
                return reply

        adapter = OmitClaimAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run('BV1Ltsf6xEpV')
        state = json.loads(self.storage.get_task(task_id)['state_json'])
        self.assertEqual(adapter.boundary_calls, 2)
        self.assertIn(adapter.omitted, state['boundary_missing_decisions'])
        self.assertTrue(any(e['event_type'] == 'boundary_empty_retry' for e in self.storage.events(task_id)))
        decisions = (state.get('boundary') or {}).get('decisions') or []
        self.assertNotIn(adapter.omitted, [d['claim_id'] for d in decisions if d['decision'] != 'excluded' and not d.get('verification_required')])

    def test_empty_claim_set_retries_understand_then_stops_without_calling_boundary(self):
        """真实故障：单元声明 c1..cN 却没有任何 claim 对象。

        旧行为是照常记录 claim_count=0 继续跑，边界节点拿到空 claim 集合后自己编出
        一套 claim_id，引用审计再把它们全判悬空，整条视频以「引用不全」的误导理由归零。
        现在必须：先带着明确要求重问一次理解节点；仍拿不到陈述就停在 N03，
        且**不得**调用边界节点替它编造事实。
        """
        class NoClaimsAdapter(MockAdapter):
            def __init__(self):
                self.understand_calls = 0
                self.boundary_calls = 0
                self.retry_payloads = []

            def complete(self, **kwargs):
                if kwargs["task"] == "understand":
                    self.understand_calls += 1
                    if "_previous_output_error" in kwargs["user"]:
                        self.retry_payloads.append(kwargs["user"]["_previous_output_error"])
                    return ModelReply("ok", {
                        "content_mode": "口播",
                        "semantic_units": [{
                            "unit_id": "u1", "meaning": "只有 ID 没有对象",
                            "claim_ids": ["c1", "c2"], "evidence_ids": [],
                            "semantic_use": "content_summary", "grounding": "unknown",
                        }],
                    })
                if kwargs["task"] == "boundary":
                    self.boundary_calls += 1
                return super().complete(**kwargs)

        adapter = NoClaimsAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])

        # 重问了一次，而且带上了「顶层 candidate_claims」的要求
        self.assertEqual(adapter.understand_calls, 2)
        self.assertEqual(len(adapter.retry_payloads), 1)
        self.assertIn("candidate_claims", adapter.retry_payloads[0]["instruction"])
        # 停在理解阶段，没有让边界节点编造 claim
        self.assertEqual(adapter.boundary_calls, 0)
        self.assertEqual(state["stop_reason"], "UNDERSTANDING_CLAIMS_MISSING")
        self.assertEqual(state["understanding_failure"]["reason"], "CLAIMS_EMPTY")
        self.assertEqual(state["understanding_failure"]["semantic_units"], 1)
        events = self.storage.events(task_id)
        self.assertTrue(any(e["event_type"] == "understanding_claims_missing" for e in events))
        self.assertTrue(any(e["event_type"] == "understanding_empty" for e in events))

    def test_understand_retry_recovers_when_claims_nested_in_units(self):
        """重问后模型把 claim 对象放进单元里：解析端要能救回，流程继续走。"""
        class NestedClaimsAdapter(MockAdapter):
            def __init__(self):
                self.understand_calls = 0

            def complete(self, **kwargs):
                if kwargs["task"] == "understand":
                    self.understand_calls += 1
                    if self.understand_calls == 1:
                        return ModelReply("ok", {
                            "content_mode": "口播",
                            "semantic_units": [{"unit_id": "u1", "meaning": "只有 ID",
                                                "claim_ids": ["c1"], "evidence_ids": []}],
                        })
                    evidence_ids = [item["evidence_id"] for item in kwargs["user"].get("video", {}).get("evidence", []) if item.get("evidence_id")]
                    return ModelReply("ok", {
                        "content_mode": "口播",
                        "semantic_units": [{
                            "unit_id": "u1", "meaning": "嵌套回来的陈述",
                            "claim_ids": ["c1"], "evidence_ids": evidence_ids[:1],
                            "semantic_use": "content_summary", "grounding": "unknown",
                            "candidate_claims": [{"claim_id": "c1", "claim_text": "嵌套陈述",
                                                  "claim_type": "observable", "evidence_refs": evidence_ids[:1]}],
                        }],
                    })
                return super().complete(**kwargs)

        adapter = NestedClaimsAdapter()
        pipeline = Pipeline(self.dataset, self.storage, adapter, self.config)
        task_id = pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(adapter.understand_calls, 2)
        self.assertEqual(len(state["understanding"]["claims"]), 1)
        self.assertNotEqual(state.get("stop_reason"), "UNDERSTANDING_CLAIMS_MISSING")
        # 救援路径不留停机记录
        self.assertTrue(any(e["event_type"] == "understanding_claims_missing" for e in self.storage.events(task_id)))
        self.assertFalse(any(e["event_type"] == "understanding_empty" for e in self.storage.events(task_id)))


if __name__ == "__main__":
    unittest.main()
