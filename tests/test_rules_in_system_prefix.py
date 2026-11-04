"""规则进 system 公共前缀：段落顺序、逐字重建与事件体积。

规则从 user 载荷搬到 system，靠的是 provider 的前缀缓存「从第一个 token 起一致」。
所以这里守三条不变量：①同一规则包在同一任务的所有节点上生成同一个前缀；
②同一份 context 必须逐字重建同一段规则（否则前缀抖动，整段缓存失效）；
③事件表不存规则全文（8.3K 字符 × 每任务 8–12 次调用会把库撑大一倍）。
"""
from types import SimpleNamespace

from agent_system.model import ModelReply
from agent_system.pipeline import Pipeline
from agent_system.prompts import SHARED_RULES, prompt_for
from agent_system.rules import load_rule_bundle
from agent_system.rules.models import prompt_text_from_context


def bundle_text():
    bundle = load_rule_bundle(domain={"category": "知识科普"}, form="剧情短片", attributes=[])
    return bundle, prompt_text_from_context(bundle.as_context())


def test_rules_block_rebuilds_identically_from_context():
    bundle, text = bundle_text()
    assert text == bundle.as_prompt_text()
    # 从 as_context() 反序列化后（checkpoint 存的就是这个 dict）必须逐字相同。
    roundtrip = prompt_text_from_context(bundle.as_context())
    assert roundtrip == text


def test_rules_block_carries_common_and_matched_blocks():
    bundle, text = bundle_text()
    assert "U-W02" in text  # common 里的表达策略规则
    assert "V16" in text and "F09" in text
    assert text.startswith("## 本次适用规则")
    assert prompt_text_from_context(None) == ""
    assert prompt_text_from_context({}) == ""


def test_every_node_shares_the_same_system_prefix():
    _, text = bundle_text()
    systems = [prompt_for(task, schema, text) for task, schema in (
        ("boundary", "BoundarySet"), ("highlight", "HighlightSet"),
        ("generate", "AgentDraft"), ("rewrite", "AgentDraft"), ("review", "ReviewResultBatch"),
    )]
    prefix = f"{SHARED_RULES}\n\n{text}"
    assert all(system.startswith(prefix) for system in systems)
    # 节点指令必须落在规则之后，否则公共前缀被切成五份。
    assert all(len(system) > len(prefix) for system in systems)


def test_prompt_without_rules_is_byte_identical_to_legacy_layout():
    for task in ("understand", "review", "thesis_review"):
        assert prompt_for(task, "ReviewResultBatch") == prompt_for(task, "ReviewResultBatch", "")


class Store:
    def __init__(self):
        self.events = []

    def event(self, task_id, node, event_type, payload):
        self.events.append((node, event_type, payload))

    def cached_model_reply(self, task_id, request_hash):
        return None


def pipeline_with_capture():
    captured = {}

    class Adapter:
        model = "fake"

        def complete(self, *, task, system, user, schema, attempt=0):
            captured.update(system=system, user=user, schema=schema)
            return ModelReply("ok", {"candidates": []}, usage={}, latency_ms=1)

    pipeline = object.__new__(Pipeline)
    pipeline.storage = Store()
    pipeline.config = SimpleNamespace(schema_retry_limit=0, review_model="fake", generation_model="fake")
    pipeline.adapter = Adapter()
    pipeline.review_adapter = Adapter()
    return pipeline, captured


def test_call_sends_rules_in_system_and_keeps_them_out_of_user():
    pipeline, captured = pipeline_with_capture()
    bundle, text = bundle_text()
    user = {"video": {"title": "标题"}, "matched_rule_ids": bundle.rule_ids}
    pipeline._call("task", "N05", "boundary", user, "BoundarySet", rules=bundle.as_context())
    assert text in captured["system"]
    assert captured["system"].startswith(SHARED_RULES)
    assert "matched_rules" not in captured["user"]

    snapshot = next(p for _, kind, p in pipeline.storage.events if kind == "prompt_snapshot")
    # 快照不落规则全文，但要能复原：hash + 字符数必须与真实 system 里的规则块一致。
    import hashlib
    assert text not in snapshot["system_prompt"]
    assert snapshot["system_prompt"] == prompt_for("boundary", "BoundarySet")
    assert snapshot["rules_block_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert snapshot["rules_block_chars"] == len(text)
    assert snapshot["system_chars"] == len(captured["system"])
    assert snapshot["prompt_hash"] == hashlib.sha256(captured["system"].encode("utf-8")).hexdigest()


def test_call_without_rules_keeps_legacy_snapshot_shape():
    pipeline, captured = pipeline_with_capture()
    pipeline._call("task", "N03", "understand", {"video": {}}, "Understanding")
    snapshot = next(p for _, kind, p in pipeline.storage.events if kind == "prompt_snapshot")
    assert snapshot["system_prompt"] == captured["system"]
    assert snapshot["rules_block_chars"] == 0
    assert snapshot["rules_block_hash"] is None
