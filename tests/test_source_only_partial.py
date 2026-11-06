"""仅原题闸修复回归（2026-11-06 定版，V 集 v2-20261106 回放驱动）：
① source_only 集合只认一手证据（raw_asr/raw_ocr/raw_frame）字面互证 ≥6 字升格；
   model_derived 转述不算互证——被核验对象不能自证。
② 闸门取消连坐：eligible 中引用孤证的候选按候选降级进 pending_review，
   其余候选维持 handoff 与推荐；整批孤证仍保持 needs_review 原语义。
"""
import tempfile
import unittest
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import MockAdapter
from agent_system.pipeline import (SOURCE_ONLY_PRIMARY_RUN_MIN, Pipeline,
                                    _source_only_claim_ids, _source_only_eligible_ids)
from agent_system.storage import Storage
from agent_system.types import Claim, Evidence, VideoInput

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")


def _video(evidence):
    return VideoInput(video_id="BVtest", title="测试标题", author="", author_uid=None, url="",
                      domain={}, format="现场记录", published_at=None, duration=None, dimensions={},
                      description="", caption="", asr_segments=[], ocr_segments=[],
                      evidence=evidence, asset_paths={}, provenance={})


def _claim(cid, text, refs):
    return Claim(cid, text, "observation", refs)


class SourceOnlySetTests(unittest.TestCase):
    def test_title_only_claim_stays_source_only(self):
        video = _video([Evidence("e-title", "source", "久站小腿酸胀", "title", quality="source"),
                        Evidence("e-a1", "asr", "拉伸跟腱三组动作", "asr:0-5", quality="raw_asr")])
        claims = [_claim("c1", "原题称久站小腿酸胀紧绷", ["e-title"])]
        self.assertEqual(_source_only_claim_ids(claims, video), ["c1"])

    def test_primary_evidence_cross_verification_upgrades_claim(self):
        # 与 raw_ocr 字面重叠 ≥6 → 升格出孤证集
        run_text = "放松小腿肌肉和跟腱"
        video = _video([Evidence("e-title", "source", "久站小腿酸胀", "title", quality="source"),
                        Evidence("e-o1", "ocr", run_text + "演示", "ocr:3s", quality="raw_ocr")])
        claims = [_claim("c1", "视频主张" + run_text, ["e-title"])]
        self.assertEqual(_source_only_claim_ids(claims, video), [])

    def test_model_derived_cross_verification_does_not_upgrade(self):
        # 重叠只出现在 model_derived_uncalibrated → 不升格（不得让模型转述自证）
        run_text = "小腿酸胀紧绷的"
        video = _video([Evidence("e-title", "source", "久站小腿酸胀", "title", quality="source"),
                        Evidence("e-cap", "caption", "展示处理" + run_text + "方式", "caption", quality="model_derived_uncalibrated")])
        claims = [_claim("c1", "原题主张" + run_text, ["e-title"])]
        self.assertEqual(_source_only_claim_ids(claims, video), ["c1"])

    def test_short_overlap_below_threshold_not_upgraded(self):
        video = _video([Evidence("e-title", "source", "ABCD", "title", quality="source"),
                        Evidence("e-a1", "asr", "XY小腿ZW", "asr:0-1", quality="raw_asr")])
        claims = [_claim("c1", "标题写XY小腿", ["e-title"])]  # 4 字 < 6
        self.assertEqual(_source_only_claim_ids(claims, video), ["c1"])
        self.assertEqual(SOURCE_ONLY_PRIMARY_RUN_MIN, 6)


class GuardPartialDemotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def _route_state(self):
        route = {"action": "handoff", "recommended_candidate_id": "cand-1",
                 "eligible_candidate_ids": ["cand-1", "cand-2"], "pending_review_candidate_ids": ["cand-3"],
                 "reason": "eligible_candidates_ranked_by_Q1_Q2_Q3"}
        state = {"source_only_claim_ids": ["c1"], "risk_signals": {"pending_signal_ids": []},
                 "evidence_contract_version": "evidence-v1", "main_highlight_relation": "core",
                 "reference_audit": {"issues": []}, "input_quality": {"level": "high"}}
        candidates = [{"candidate_id": "cand-1", "claim_ids": ["c1"]},
                      {"candidate_id": "cand-2", "claim_ids": ["c2"]},
                      {"candidate_id": "cand-3", "claim_ids": ["c3"]}]
        return route, state, candidates

    def test_partial_demotion_keeps_handoff_for_clean_candidates(self):
        route, state, candidates = self._route_state()
        out = self.pipeline._guard_handoff(route, state, candidates, task_id="task-test")
        self.assertEqual(out["action"], "handoff")
        self.assertEqual(out["eligible_candidate_ids"], ["cand-2"])
        self.assertEqual(out["recommended_candidate_id"], "cand-2", "推荐被孤证候选占据时顺延给下一干净候选")
        self.assertIn("cand-1", out["pending_review_candidate_ids"], "孤证候选降级但仍交人工")
        self.assertEqual(out["reason"], "SOURCE_TITLE_ONLY_PARTIAL")
        self.assertEqual(out["source_only_demoted_candidate_ids"], ["cand-1"])
        events = [r["payload_json"] for r in self.storage.conn.execute(
            "SELECT payload_json FROM events WHERE event_type='source_only_candidate_demoted'")]
        self.assertTrue(events, "降级必须落事件")

    def test_all_eligible_source_only_still_full_batch_review(self):
        route, state, candidates = self._route_state()
        candidates[1]["claim_ids"] = ["c1"]
        out = self.pipeline._guard_handoff(route, state, candidates, task_id="task-test")
        self.assertEqual(out["action"], "needs_review")
        self.assertEqual(out["reason"], "SOURCE_TITLE_ONLY_UNVERIFIED")
        self.assertIsNone(out["recommended_candidate_id"])
        self.assertEqual(out["eligible_candidate_ids"], [])

    def test_eligible_order_preserved_after_demotion(self):
        route, state, candidates = self._route_state()
        route["eligible_candidate_ids"] = ["cand-2", "cand-1"]
        route["recommended_candidate_id"] = "cand-2"
        out = self.pipeline._guard_handoff(route, state, candidates)
        self.assertEqual(out["recommended_candidate_id"], "cand-2", "干净推荐不受连坐影响")
        self.assertEqual(out["eligible_candidate_ids"], ["cand-2"])

    def test_source_only_eligible_ids_respects_route_order(self):
        route, state, candidates = self._route_state()
        self.assertEqual(_source_only_eligible_ids(route, state, candidates), ["cand-1"])


if __name__ == "__main__":
    unittest.main()
