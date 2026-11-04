"""重写主旨锁：改标题不等于改题目。

契约（本文件为准）：
- 模型没回显 thesis_id：按父候选/契约锁定并标 thesis_id_inherited，不算错——
  ID 回显是格式合规，不是"内容没换题"的证据；内容是否跑题由 G3 的 core_covered 判定。
- 模型显式给了不同的 thesis_id：判结构错误，退回重写/待审，这是真正的换题。
- 没有任何锁（旧任务/未接主旨契约）：不凭空造 ID，也不判错。
"""
from __future__ import annotations

from agent_system.pipeline import _enforce_rewrite_thesis


def test_rewrite_inherits_locked_thesis_when_model_omits_it():
    candidates = [{"candidate_id": "r1", "headline": "改后标题"}]
    _enforce_rewrite_thesis(candidates, {"thesis_id": "t1"}, {"thesis_id": "t1"})
    assert candidates[0]["thesis_id"] == "t1"
    assert candidates[0]["thesis_id_inherited"] is True
    assert not candidates[0].get("generation_schema_errors")


def test_rewrite_cannot_switch_to_another_thesis():
    candidates = [{"candidate_id": "r1", "headline": "换成更好写的边角", "thesis_id": "t9"}]
    _enforce_rewrite_thesis(candidates, {"thesis_id": "t1"}, {"thesis_id": "t1"})
    errors = candidates[0]["generation_schema_errors"]
    assert any("thesis_id" in e for e in errors), errors


def test_rewrite_falls_back_to_contract_when_parent_has_no_thesis():
    candidates = [{"candidate_id": "r1", "headline": "改后标题"}]
    _enforce_rewrite_thesis(candidates, {}, {"thesis_id": "t2"})
    assert candidates[0]["thesis_id"] == "t2"
    assert not candidates[0].get("generation_schema_errors")


def test_rewrite_without_any_lock_does_not_fabricate_thesis():
    candidates = [{"candidate_id": "r1", "headline": "改后标题"}]
    _enforce_rewrite_thesis(candidates, {}, {})
    assert not candidates[0].get("thesis_id")
    assert candidates[0].get("generation_schema_errors")
