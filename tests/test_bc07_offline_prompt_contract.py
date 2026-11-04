"""BC07 提示词及生成入参契约：仅检查确定性文本与传参，不调用模型。"""
from pathlib import Path

import pytest

from agent_system.config import RunConfig
from agent_system.pipeline import Pipeline
from agent_system.prompts import GENERATION_PROMPT, REVIEW_PROMPT
from agent_system.rules.rubric import STRATEGY_ENUM, STRATEGY_TRIGGERS
from agent_system.types import BoundarySet, Understanding, VideoInput


class CapturedCall(Exception):
    pass


@pytest.mark.parametrize("rewrite", [False, True], ids=["生成", "重写"])
def test_model_input_contains_exact_strategy_triggers(rewrite):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(memory_call_limit=0)
    captured = {}

    def capture(task_id, node, task, user, schema, **kwargs):
        captured.update(task=task, user=user, call_kwargs=kwargs)
        raise CapturedCall

    pipeline._call = capture
    video = VideoInput("v", "标题", "作者", None, "", {}, "", None, None,
                       {}, "", "", [], [], [], {}, {})
    understanding = Understanding("", [], [], [], [])
    source = {"candidate_id": "c1", "headline": "标题", "slot_id": "core_summary",
              "thesis_id": "t1", "highlight_id": "h1"} if rewrite else None
    with pytest.raises(CapturedCall):
        pipeline._generate_and_review("task", {"prompt_hashes": {}}, video,
                                      understanding, BoundarySet([]), [], 0, source=source)
    assert captured["task"] == ("rewrite" if rewrite else "generate")
    assert captured["user"]["strategy_enum"] == STRATEGY_ENUM
    assert captured["user"]["strategy_triggers"] == STRATEGY_TRIGGERS
    assert set(captured["user"]["strategy_triggers"]) == set(STRATEGY_ENUM)


def test_strategy_rule_requires_support_and_direct_description_fallback():
    rules = (Path(__file__).resolve().parents[1] / "rulepacks" / "common.md").read_text(encoding="utf-8")
    rule = next(line for line in rules.splitlines() if line.startswith("U-W02 "))
    assert "材料支持" in rule
    assert all(term in rule for term in ("比较", "前后", "方法"))
    assert "直接描述" in rule
    assert any(term in rule for term in ("无法确认", "不确定"))
    fallback = STRATEGY_TRIGGERS["直接描述"]
    assert any(term in fallback for term in ("兜底", "不适合", "不满足"))
    assert all(STRATEGY_TRIGGERS[name] for name in STRATEGY_ENUM)


def test_generation_prompt_demands_readability_without_rigid_sentence_template():
    assert "语义自足" in GENERATION_PROMPT
    assert "自然" in GENERATION_PROMPT
    assert "完整主谓宾" in GENERATION_PROMPT
    assert "不必机械补词" in GENERATION_PROMPT
    assert "残句" in GENERATION_PROMPT
    assert "直接描述" in GENERATION_PROMPT


def test_semantic_review_prompt_checks_delivery_and_allows_real_answers():
    assert "delivery_supported" in REVIEW_PROMPT
    assert "core_covered" in REVIEW_PROMPT
    assert "G3_NOT_DELIVERED" in REVIEW_PROMPT
    assert "G3_CORE_POINT_OMITTED" in REVIEW_PROMPT
    assert "疑问词" in REVIEW_PROMPT and "机械" in REVIEW_PROMPT
    assert "Q1" in REVIEW_PROMPT and "自然省略" in REVIEW_PROMPT
