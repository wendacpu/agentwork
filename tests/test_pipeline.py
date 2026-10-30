import json
import tempfile
import unittest
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter, ModelReply
from agent_system.pipeline import Pipeline
from agent_system.pipeline import _decide_candidate, _normalize_boundary, _normalize_candidates, _normalize_highlights, _normalize_reviews, _normalize_understanding
from agent_system.rules import load_common_rule_ids, load_rule_bundle, rubric
from agent_system.rules.loader import load_domain_rules, load_form_rules
from agent_system.schemas import validate_review
from agent_system.storage import Storage
from agent_system.memory import MemoryStore
from agent_system.types import CaseRecord


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
        self.assertEqual(bundle.rule_ids, ["U", "V04", "F11"])
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
                            item["gates"]["G3"] = {"status": "revise", "reason": "看点承诺偏宽", "evidence_ids": [], "rule_ids": []}
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
        self.assertGreaterEqual(len(state["candidates"]), 4)

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
        self.assertEqual(set(load_domain_rules()), {f"V{i:02d}" for i in range(1, 11)})
        self.assertEqual(set(load_form_rules()), {f"F{i:02d}" for i in range(1, 15)})
        bundle = load_rule_bundle(domain={"一级": "资讯", "二级": "时事社会"}, form="资讯播报")
        self.assertEqual(bundle.rule_ids, ["U", "V04", "F11"])
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

    def test_decision_thresholds_are_per_dimension_and_sum_only_sorts(self):
        review = {"candidate_id": "c1", "gates": {g: {"status": "pass", "reason": "pass"} for g in ("G1", "G2", "G3")}, "quality": {"Q1": {"score": 1}, "Q2": {"score": 3}, "Q3": {"score": 3}}, "issues": []}
        result = _decide_candidate(review, {"status": "pass", "errors": []}, schema_errors=[], business_policy={"status": "approved", "rules": [{"rule_id": "P1"}]})
        self.assertEqual(result["status"], "rewrite")
        self.assertEqual(result["quality_sum"], 7)


if __name__ == "__main__":
    unittest.main()
