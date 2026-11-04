"""turns 解析与剪枝：一条结构注解不该换掉整条视频的候选。

真实故障（2026-11-01 真实冒烟）：模型输出 from_event_id/to_event_id，解析器只认 from_event，
端点全空 → 引用审计判 empty_ref → N05 整条阻断 → 视频零产出。
"""
from __future__ import annotations

from agent_system.pipeline import _normalize_understanding, _prune_invalid_turns
from agent_system.types import EventNode, EventTurn, Understanding


def test_model_side_keys_are_accepted():
    raw = {
        "events": [
            {"event_id": "ev1", "actor": "甲", "action": "先做A", "evidence_ids": ["e-asr-0000"]},
            {"event_id": "ev2", "actor": "乙", "action": "后做B", "evidence_ids": ["e-asr-0001"]},
        ],
        "turns": [
            {"turn_id": "t1", "type": "causal", "from_event_id": "ev1", "to_event_id": "ev2", "notes": "A 导致 B"},
        ],
    }
    understanding = _normalize_understanding(raw)
    assert len(understanding.turns) == 1
    turn = understanding.turns[0]
    assert (turn.from_event, turn.to_event) == ("ev1", "ev2")
    assert turn.note == "A 导致 B"


def _understanding(turns, event_ids=("ev1", "ev2")):
    return Understanding(
        main_format="叙事",
        attributes=[],
        claims=[],
        event_times=[],
        extracted_sources=[],
        events=[EventNode(event_id=e, actor="甲", action="做事") for e in event_ids],
        turns=turns,
    )


def test_empty_endpoint_turn_is_dropped_not_blocking():
    understanding = _understanding([
        EventTurn("ev1", "ev2", "sequence", note="好的一条"),
        EventTurn("", "", "sequence"),
    ])
    dropped = _prune_invalid_turns(understanding, [])
    assert [t.from_event for t in understanding.turns] == ["ev1"]
    assert dropped == [{"index": 1, "from": "", "to": "", "type": "sequence"}]


def test_dangling_endpoint_turn_is_dropped():
    understanding = _understanding([EventTurn("ev1", "ev9", "sequence")])
    dropped = _prune_invalid_turns(understanding, [])
    assert understanding.turns == []
    assert dropped and dropped[0]["to"] == "ev9"


def test_flagged_turn_is_dropped_even_if_endpoints_look_valid():
    understanding = _understanding([EventTurn("ev1", "ev2", "sequence")])
    dropped = _prune_invalid_turns(understanding, [{"path": "understanding.turns[0]", "code": "empty_ref"}])
    assert understanding.turns == []
    assert dropped[0]["index"] == 0
