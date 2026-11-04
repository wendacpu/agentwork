"""信号守卫的离线集成回归：真实 Pipeline.run，不依赖视频资产或网络。"""
import json
from unittest.mock import patch

import pytest

from agent_system.config import RunConfig
from agent_system.model import MockAdapter
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage
from agent_system.types import Evidence, VideoInput


class MemoryDataset:
    def __init__(self, video):
        self.video = video

    def get(self, video_id):
        assert video_id == self.video.video_id
        return self.video


class SignalAdapter(MockAdapter):
    """仅控制模型输出；所有守卫、路由与持久化均使用真实实现。"""

    def __init__(self, headline, claim_text=None):
        self.headline = headline
        self.claim_text = claim_text
        self.tasks = []

    def complete(self, **kwargs):
        self.tasks.append(kwargs["task"])
        reply = super().complete(**kwargs)
        if kwargs["task"] == "understand":
            reply.data["attributes"] = []
            if self.claim_text is not None:
                reply.data["claims"][0]["text"] = self.claim_text
        elif kwargs["task"] in {"generate", "rewrite"}:
            reply.data["candidates"] = reply.data["candidates"][:1]
            reply.data["candidates"][0]["headline"] = self.headline
        return reply


def run_video(tmp_path, title, headline, *, extra_evidence=(), claim_text=None, tamper=None):
    video = VideoInput(
        video_id="offline-signal", title=title, author="离线作者", author_uid=None,
        url="", domain={"一级": "生活", "二级": "日常"}, format="生活记录",
        published_at="2020-01-01", duration=10, dimensions={}, description="",
        caption="", asr_segments=[], ocr_segments=[],
        evidence=[Evidence("e-title", "title", title, quality="source"), *extra_evidence],
        asset_paths={}, provenance={},
    )
    adapter = SignalAdapter(headline, claim_text)
    config = RunConfig(
        asset_root=str(tmp_path), output_dir=str(tmp_path), memory_call_limit=0,
        max_rewrite_rounds=0, verification_enabled=False,
        extra={"require_human_on_low_quality_input": False,
               "business_policy": {"status": "approved", "version": "offline-test",
                                   "rules": [{"rule_id": "P1", "text": "离线测试政策"}]}},
    )
    storage = Storage(tmp_path / "agent.db")
    try:
        pipeline = Pipeline(MemoryDataset(video), storage, adapter, config)
        with patch.object(pipeline.memory, "search", side_effect=AssertionError("案例检索应禁用")), \
             patch.object(pipeline.memory, "search_for_video", side_effect=AssertionError("同视频案例检索应禁用")), \
             patch("socket.create_connection", side_effect=AssertionError("禁止网络")):
            task_id = pipeline.run(video.video_id)
            if tamper is not None:
                historical = json.loads(storage.get_task(task_id)["state_json"])
                tamper(historical)
                storage.set_state(task_id, "pending_human", historical)
                isolated_path = pipeline.rereview(task_id)
                isolated = Storage(isolated_path)
                try:
                    row = dict(isolated.get_task(task_id))
                finally:
                    isolated.conn.close()
            else:
                row = storage.get_task(task_id)
        state = json.loads(row["state_json"])
        assert row["status"] != "technical_failed", state.get("error")
        assert state["understanding"]["attributes"] == []
        assert not state.get("memory_calls")
        assert "verify" not in adapter.tasks
        return row["status"], state
    finally:
        storage.conn.close()


@pytest.mark.parametrize("source,headline,code", [
    ("小店开业", "顾客花872元", "G4_NUMERIC_CONFLICT"),
])
def test_rereview_recomputes_prefix_and_numbers_despite_forged_historical_pass(tmp_path, source, headline, code):
    def forge_historical_pass(state):
        candidate = state["candidates"][0]
        candidate.update(headline=headline, prefix_labels=[], rendered_title=headline)
        candidate["mechanical_checks"] = {"status": "pass", "errors": [], "rendered_title": headline, "title_length": len(headline)}
        state["required_prefixes"] = []
        state["risk_signals"] = {}

    _, state = run_video(tmp_path, source, "小店开业", tamper=forge_historical_pass)
    errors = state["candidates"][0]["mechanical_checks"]["errors"]
    assert code in {error["code"] for error in errors}
    assert state["candidates"][0]["mechanical_checks"]["status"] == "fail"
    assert state["reviews"][0]["decision"] != "ready_for_human_review"
    assert state["route"]["action"] != "handoff"


def test_rereview_reports_missing_prefix_as_warning_instead_of_failing_the_candidate(tmp_path):
    """前缀缺失降级为 warning：仍然被重算出来，但不再把候选判死。"""
    def forge_historical_pass(state):
        candidate = state["candidates"][0]
        candidate.update(headline="小店开业", prefix_labels=[], rendered_title="小店开业")
        candidate["mechanical_checks"] = {"status": "pass", "errors": [], "rendered_title": "小店开业", "title_length": 4}
        state["required_prefixes"] = []
        state["risk_signals"] = {}

    _, state = run_video(tmp_path, "本视频由AI生成", "小店开业", tamper=forge_historical_pass)
    checks = state["candidates"][0]["mechanical_checks"]
    assert "G4_PREFIX_MISSING" not in {e["code"] for e in checks["errors"]}
    assert "G4_PREFIX_MISSING" in {w["code"] for w in checks.get("warnings", [])}


def test_title_only_control_requires_review(tmp_path):
    status, state = run_video(tmp_path, "小店开业", "小店开业")
    assert status == "pending_human"
    assert state["route"]["action"] == "needs_review"
    assert state["route"]["reason"] == "SOURCE_TITLE_ONLY_UNVERIFIED"
    assert state["reviews"][0]["validation_errors"] == []


def test_raw_ai_declaration_adds_prefix_without_attributes_or_memory(tmp_path):
    _, state = run_video(tmp_path, "本视频由AI生成", "小店开业")
    assert state["required_prefixes"] == ["AI生成"]
    assert state["risk_signals"]["pending_signal_ids"] == []
    assert state["candidates"]
    for candidate in state["candidates"]:
        assert candidate["prefix_labels"] == ["AI生成"]
        assert candidate["rendered_title"].startswith("【AI生成】")


def test_derived_caption_ai_hint_blocks_otherwise_normal_handoff(tmp_path):
    _, state = run_video(tmp_path, "小店开业", "小店开业", extra_evidence=[
        Evidence("e-caption", "caption", "本视频为AI生成", quality="model_derived_uncalibrated"),
    ])
    assert state["required_prefixes"] == []
    assert state["risk_signals"]["pending_signal_ids"]
    # 候选本身合格，不能靠缺政策或评审格式错误碰巧阻断交接。
    assert state["reviews"][0]["decision"] == "ready_for_human_review"
    assert state["route"]["action"] == "needs_review"
    assert state["route"]["reason"] == "CONTENT_NATURE_SIGNAL_UNRESOLVED"
    assert state["route"]["eligible_candidate_ids"] == []
    assert state["route"]["recommended_candidate_id"] is None


@pytest.mark.parametrize("source,headline", [
    ("顾客在小店吃饭", "顾客花872元"),
    ("顾客花872元吗？", "顾客花872元"),
])
def test_generated_number_requires_assertive_source_support(tmp_path, source, headline):
    _, state = run_video(tmp_path, source, headline, claim_text="顾客在小店吃饭")
    assert state["candidates"]
    errors = state["candidates"][0]["mechanical_checks"]["errors"]
    assert "G4_NUMERIC_CONFLICT" in {error["code"] for error in errors}
    assert state["reviews"][0]["decision"] != "ready_for_human_review"
    assert state["route"]["action"] != "handoff"


@pytest.mark.parametrize("source", ["顾客在小店吃饭", "顾客花872元吗？"])
def test_numeric_claim_cannot_bypass_boundary_with_invented_or_question_number(tmp_path, source):
    status, state = run_video(tmp_path, source, "顾客花872元", claim_text="顾客花872元")
    assert status == "stopped"
    decision = state["boundary"]["decisions"][0]
    assert decision["decision"] == "excluded"
    assert decision["verification_required"] is True
    assert state["verification_tasks"][0]["claim_id"] == "c1"
    assert state["candidates"] == []


@pytest.mark.parametrize("source,headline,reason", [
    ("部分人可能适用", "人人适用", "QUALIFIER_RETENTION_UNVERIFIED"),
    ("今日小店开业", "今日小店开业", "RELATIVE_TIME_UNVERIFIED"),
])
def test_missing_qualifiers_and_historical_today_require_review(tmp_path, source, headline, reason):
    _, state = run_video(tmp_path, source, headline)
    assert state["candidates"][0]["mechanical_checks"]["status"] == "pass"
    review = state["reviews"][0]
    assert review["validation_errors"] == []
    assert reason in {item["code"] for item in review["signal_review_reasons"]}
    assert review["decision"] == "needs_review"
    assert state["candidate_decisions"][0]["reason"] == "SIGNAL_SEMANTICS_UNVERIFIED"
    assert state["route"]["action"] == "needs_review"
