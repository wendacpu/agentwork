"""压缩小载荷：超长修复不再重付全量材料，引用一律继承父候选。

守三条契约（2026-11-05 成本改造第 3 条）：
① repair_mode=压缩 时发给模型的 user 不再携带 video 全量 / claims / boundary /
  规则 / 案例 / 风险信号，只带「父标题 + 预算 + 哪些字不能删 + P9 违禁词」；
② 模型返回只含 headline，claim_ids/evidence_ids/slot/thesis/prefix 由
  _fill_compress_draft 从父候选继承——压缩不改绑定，也不许模型编引用；
③ compress_small_request 事件把新旧载荷字符数都记下来，省了多少是查得到的。
"""
from types import SimpleNamespace

import pytest

from agent_system.config import RunConfig
from agent_system.pipeline import Pipeline, _fill_compress_draft
from agent_system.types import BoundarySet, Claim, ClaimDecision, Understanding, VideoInput


class CapturedCall(Exception):
    pass


class Store:
    def __init__(self):
        self.events = []

    def event(self, task_id, node, event_type, payload):
        self.events.append((node, event_type, payload))

    def save_candidates(self, task_id, candidates):
        pass


class NoMemory:
    def __init__(self):
        self.searched = 0

    def search(self, *args, **kwargs):
        self.searched += 1
        return []

    def search_for_video(self, *args, **kwargs):
        self.searched += 1
        return []


def build_pipeline():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(memory_call_limit=3)
    pipeline.storage = Store()
    pipeline.memory = NoMemory()
    return pipeline


def claim(cid, text):
    return Claim(cid, text, "observable", ["e1"])


def decision(cid, **kw):
    return ClaimDecision(claim_id=cid, supported_statement="原始陈述", decision="limited",
                         evidence_refs=["e1"], required_limits=kw.get("limits", []),
                         forbidden_inferences=[], rule_ids=[], reason="测试",
                         attribution_required=kw.get("attr", False),
                         required_source_phrase=kw.get("phrase"),
                         verification_required=kw.get("verify", False))


def video_input():
    video = VideoInput("v", "原标题", "作者", None, "", {"一级": "剧情"}, "", None, None,
                       {}, "", "", [], [], [], {}, {})
    video.format = "剧情短片"
    video.attributes = []
    return video


def source_candidate():
    return {"candidate_id": "cand-2", "headline": "这是一个明显超出二十字预算的很长标题需要压缩",
            "rendered_title": "【剧情演绎】这是一个明显超出二十字预算的很长标题需要压缩",
            "prefix_labels": ["剧情演绎"], "claim_ids": ["c1"], "evidence_ids": ["e1"],
            "highlight_id": "h1", "unit_id": "su-001", "slot_id": "core_summary", "thesis_id": "t1",
            "expression_strategy": "直接描述", "generation_round": 0,
            "mechanical_checks": {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED", "reason": "渲染后总长 25 超过 20"}]}}


def run_compress(**over):
    pipeline = build_pipeline()
    captured = {}

    def capture(task_id, node, task, user, schema, *args, **kwargs):
        captured.update(task=task, user=user, schema=schema, kwargs=kwargs)
        raise CapturedCall

    pipeline._call = capture
    video = video_input()
    understanding = Understanding("剧情", [], [claim("c1", "博主称这是很长标题的原始陈述")], [], [])
    boundary = BoundarySet([decision("c1", limits=["须归属为作者说法"])])
    state = {"prompt_hashes": {}, "reviews": [{"candidate_id": "cand-2", "issues": [
        {"issue_code": "G4_LENGTH_EXCEEDED", "action": "缩短正文"}]}], "rules": {}}
    state.update(over)
    source = over.get("source", source_candidate())
    with pytest.raises(CapturedCall):
        pipeline._generate_and_review("task", state, video, understanding, boundary, [], 1,
                                      source=source, repair_mode="压缩")
    return pipeline, captured, state


def test_compress_payload_carries_only_compression_inputs():
    _, captured, _state = run_compress()
    user = captured["user"]
    assert captured["task"] == "rewrite"
    assert user["rewrite_mode"] == "压缩"
    # 全量材料必须缺席：这些是超长的标题「不需要」的东西。
    for key in ("claims", "semantic_units", "unit_decisions", "boundary", "source_requirements",
                "blocked_claims", "highlights", "risk_signals", "matched_rules", "matched_rule_ids",
                "thesis_contract", "input_quality", "strategy_enum", "strategy_triggers"):
        assert key not in user, key
    # 必须在场的是：压哪句、压到多少、哪些字不能删。
    assert user["parent_candidate"]["candidate_id"] == "cand-2"
    assert user["length_repair"]["over_by"] > 0
    assert "max_chars_body" in user["length_repair"]
    assert user["required_claims"][0]["required_limits"] == ["须归属为作者说法"]
    assert "博主称这是很长标题的原始陈述" in user["required_claims"][0]["text"]


def test_compress_payload_is_a_fraction_of_full_payload():
    pipeline, captured, _state = run_compress()
    event = next(p for _, kind, p in pipeline.storage.events if kind == "compress_small_request")
    assert event["parent"] == "cand-2"
    assert event["compress_payload_chars"] < event["full_payload_chars"] * 0.5


def test_compress_skips_case_memory():
    pipeline, captured, state = run_compress()
    assert pipeline.memory.searched == 0
    assert "memory_cases" not in captured["user"]
    assert "memory_calls" not in state  # 压缩不消耗案例额度


def test_fill_compress_draft_inherits_every_binding_field():
    source = source_candidate()
    draft = {"candidates": [
        {"headline": "作者谈超长标题的压缩", "change_reason": "删掉自造标签"},
        {"text": "备选键名也要认"},
        {"headline": ""},
        "不是对象",
    ]}
    filled = _fill_compress_draft(draft, source)
    items = filled["candidates"]
    assert len(items) == 2
    first = items[0]
    for key in ("claim_ids", "evidence_ids", "highlight_id", "unit_id", "slot_id", "thesis_id", "prefix_labels"):
        assert first[key] == source[key], key
    assert first["parent_candidate_id"] == "cand-2"
    assert first["headline"] == "作者谈超长标题的压缩"
    assert items[1]["headline"] == "备选键名也要认"


def test_attack_words_follow_the_compress_request():
    source = source_candidate()
    source["rendered_title"] = "【剧情演绎】丑八怪教化妆还超出字数预算很长很长"
    pipeline = build_pipeline()
    captured = {}

    def capture(task_id, node, task, user, schema, *args, **kwargs):
        captured.update(user=user)
        raise CapturedCall

    pipeline._call = capture
    video = video_input()
    understanding = Understanding("剧情", [], [claim("c1", "陈述")], [], [])
    boundary = BoundarySet([])
    state = {"prompt_hashes": {}, "reviews": [], "rules": {}}
    with pytest.raises(CapturedCall):
        pipeline._generate_and_review("task", state, video, understanding, boundary, [], 1,
                                      source=source, repair_mode="压缩")
    forbidden = captured["user"].get("forbidden_terms") or {}
    assert forbidden.get("rule_id") == "P9"
    assert forbidden.get("terms")


def test_semantic_rewrite_still_gets_the_full_payload():
    # 反向对照：非压缩的定向重写必须仍拿到全量材料，这次改动不能顺手削掉它。
    pipeline = build_pipeline()
    captured = {}

    def capture(task_id, node, task, user, schema, *args, **kwargs):
        captured.update(user=user)
        raise CapturedCall

    pipeline._call = capture
    video = video_input()
    understanding = Understanding("剧情", [], [claim("c1", "陈述")], [], [])
    boundary = BoundarySet([decision("c1")])
    state = {"prompt_hashes": {}, "reviews": [], "rules": {"rule_ids": ["U", "V04"]}}
    with pytest.raises(CapturedCall):
        pipeline._generate_and_review("task", state, video, understanding, boundary, [], 1,
                                      source=source_candidate())
    user = captured["user"]
    assert user["rewrite_mode"] == "表达"
    assert "boundary" in user and "claims" in user and "video" in user
    assert user["matched_rule_ids"] == ["U", "V04"]
