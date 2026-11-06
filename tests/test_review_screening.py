from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_system.model import ModelError, ModelReply
from scripts import review_screening, review_screening_score


class SequenceAdapter:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def reply(data, raw='{"verdict":"可用"}', usage=None, latency=7):
    return ModelReply("ok", data, raw, usage or {"prompt_tokens": 3, "completion_tokens": 2}, latency)


def test_payload_is_whitelisted_and_truncated():
    item = {
        "item_id": "i1", "candidate_title": "标题", "human_decision": "rejected",
        "materials": {
            "video_id": "BV1", "caption": "素材", "route": "needs_review",
            "asr_segments": [{"text": str(i)} for i in range(25)],
            "ocr_segments": [{"text": str(i)} for i in range(22)],
        },
    }
    payload = review_screening.build_payload(item)
    assert payload == {
        "title": "标题",
        "video": {"video_id": "BV1", "caption": "素材", "asr": [str(i) for i in range(20)],
                  "ocr": [str(i) for i in range(20)]},
    }


def test_load_blind_rejects_label_leak_and_duplicate_ids(tmp_path: Path):
    leaked = tmp_path / "leaked.jsonl"
    leaked.write_text(json.dumps({"item_id": "i1", "candidate_title": "x", "human_decision": "approved"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="标签字段"):
        review_screening.load_blind(leaked)

    nested = tmp_path / "nested.jsonl"
    nested.write_text(json.dumps({"item_id": "i2", "candidate_title": "x", "materials": {"trace": {"human_reason": "泄漏"}}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="标签字段"):
        review_screening.load_blind(nested)

    duplicate = tmp_path / "duplicate.jsonl"
    line = json.dumps({"item_id": "i1", "candidate_title": "x", "materials": {}})
    duplicate.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        review_screening.load_blind(duplicate)


def test_judge_preserves_raw_retries_retryable_errors_and_sums_usage():
    adapter = SequenceAdapter([
        ModelError("temporary", retryable=True, raw="broken-1"),
        reply({"verdict": "可用", "reason": "支持"}, raw="raw-good", usage={"prompt_tokens": 5, "completion_tokens": 4}, latency=9),
    ])
    result = review_screening.judge(adapter, {"title": "x"}, retry_limit=2)
    assert result["parse_status"] == "ok"
    assert result["verdict"] == "approved"
    assert result["usage"] == {"prompt_tokens": 5, "completion_tokens": 4}
    assert result["latency_ms"] == 9
    assert result["attempts"][0]["raw_response"] == "broken-1"
    assert result["attempts"][1]["raw_response"] == "raw-good"
    assert len(adapter.calls) == 2


def test_judge_does_not_retry_non_retryable_and_keeps_pending_raw_response():
    blocked = SequenceAdapter([ModelError("bad key", retryable=False, raw="auth-body")])
    failed = review_screening.judge(blocked, {"title": "x"}, retry_limit=2)
    assert failed["parse_status"] == "failure"
    assert len(blocked.calls) == 1
    assert failed["attempts"] == [{"attempt": 0, "status": "error", "error": "bad key", "retryable": False, "raw_response": "auth-body"}]

    pending_adapter = SequenceAdapter([reply({"verdict": "待人工", "reason": "不确定"}, raw="raw-pending")])
    pending = review_screening.judge(pending_adapter, {"title": "x"}, retry_limit=2)
    assert pending["parse_status"] == "pending"
    assert pending["attempts"][0]["raw_response"] == "raw-pending"
    assert len(pending_adapter.calls) == 1


def test_run_screening_writes_records_and_refuses_stale_partial(tmp_path: Path):
    out = tmp_path / "result.jsonl"
    items = [{"item_id": "i1", "candidate_title": "标题", "materials": {"video_id": "BV1"}}]
    records = review_screening.run_screening(items, SequenceAdapter([reply({"verdict": "不可用", "reason": "错"})]), "m", out, 0)
    assert records[0]["video_id"] == "BV1"
    assert records[0]["verdict"] == "rejected"
    assert json.loads(out.read_text(encoding="utf-8"))["attempts"][0]["raw_response"]

    stale = tmp_path / "stale.jsonl"
    stale.with_suffix(".jsonl.partial").write_text("unfinished", encoding="utf-8")
    with pytest.raises(FileExistsError, match="未完成"):
        review_screening.run_screening(items, SequenceAdapter([]), "m", stale, 0)


def test_score_keeps_failures_in_denominator_and_excludes_pending_from_verdict_metrics():
    labels = {
        "a": {"video_id": "v1", "semantic_label_eligible": True, "human_decision": "approved"},
        "b": {"video_id": "v1", "semantic_label_eligible": True, "human_decision": "rejected"},
        "c": {"video_id": "v2", "semantic_label_eligible": True, "human_decision": "approved"},
    }
    records = {
        "a": {"item_id": "a", "parse_status": "ok", "verdict": "approved", "usage": {"total_tokens": 8}, "latency_ms": 10},
        "b": {"item_id": "b", "parse_status": "pending", "verdict": None, "usage": {"total_tokens": 5}, "latency_ms": 20},
        "c": {"item_id": "c", "parse_status": "failure", "verdict": None, "usage": {"total_tokens": 3}, "latency_ms": 30},
    }
    score = review_screening_score.score_model(records, labels)
    assert score["coverage"] == pytest.approx(2 / 3)
    assert score["missing_rate"] == 0
    assert score["failure_rate"] == pytest.approx(1 / 3)
    assert score["pending_rate"] == pytest.approx(1 / 2)
    assert score["strict"]["precision"] == 1
    assert score["strict"]["recall"] == 1
    assert score["usage"]["total_tokens"] == 16
    assert score["pending_label_distribution"] == {"approved": 0, "marginal": 0, "rejected": 1}
    assert score["failure_label_distribution"] == {"approved": 1, "marginal": 0, "rejected": 0}


def test_score_does_not_report_missing_records_as_model_failures():
    labels = {
        "a": {"video_id": "v1", "semantic_label_eligible": True, "human_decision": "approved"},
        "b": {"video_id": "v2", "semantic_label_eligible": True, "human_decision": "rejected"},
    }
    score = review_screening_score.score_model({"a": {"item_id": "a", "parse_status": "ok", "verdict": "approved"}}, labels)
    assert score["coverage"] == pytest.approx(1 / 2)
    assert score["missing_rate"] == pytest.approx(1 / 2)
    assert score["failure_rate"] == 0


def test_score_rejects_unknown_item_id():
    with pytest.raises(ValueError, match="不属于题库"):
        review_screening_score.score_model({"unknown": {"item_id": "unknown"}}, {})
