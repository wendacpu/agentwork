"""成本埋点与不可重试错误：不调用外部模型。"""
from types import SimpleNamespace

import pytest

from agent_system.model import ModelError, ModelReply
from agent_system.pipeline import Pipeline


class EventStore:
    def __init__(self):
        self.events = []

    def event(self, task_id, node, event_type, payload):
        self.events.append((node, event_type, payload))


def pipeline_with(adapter):
    pipeline = object.__new__(Pipeline)
    pipeline.storage = EventStore()
    pipeline.review_adapter = adapter
    pipeline.config = SimpleNamespace(review_model="test")
    return pipeline


def test_reconcile_records_usage_and_result():
    class Adapter:
        model = "judge"

        def complete(self, **kwargs):
            return ModelReply("ok", {"relation": "contradiction", "contradiction_point": "主体相反"}, usage={"prompt_tokens": 120, "completion_tokens": 8}, latency_ms=12)

    pipeline = pipeline_with(Adapter())
    result = pipeline._reconcile_contracts("task", {"thesis_contract": {"x": 1}, "review_thesis_contract": {"x": 2}})
    assert result["relation"] == "contradiction"
    replies = [payload for _, kind, payload in pipeline.storage.events if kind == "model_reply"]
    assert len(replies) == 1
    assert replies[0]["task"] == "thesis_consistency"
    assert replies[0]["model"] == "judge"
    assert replies[0]["usage"]["prompt_tokens"] == 120


def test_reconcile_failure_is_recorded_and_remains_conservative():
    class Adapter:
        def complete(self, **kwargs):
            raise ModelError("HTTP 402: Payment Required", False)

    pipeline = pipeline_with(Adapter())
    assert pipeline._reconcile_contracts("task", {"thesis_contract": {"x": 1}, "review_thesis_contract": {"x": 2}}) is None
    assert [kind for _, kind, _ in pipeline.storage.events] == ["node_started", "model_error", "thesis_reconcile_failed"]


def test_nonretryable_review_error_never_falls_back_to_per_candidate():
    pipeline = pipeline_with(None)
    pipeline._review_input = lambda *args: {}
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise ModelError("HTTP 402: Payment Required", False)

    pipeline._call = fail
    with pytest.raises(ModelError, match="402"):
        pipeline._review_with_fallback("task", None, {}, [{"candidate_id": "a"}, {"candidate_id": "b"}], [])
    assert len(calls) == 1
