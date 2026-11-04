"""评审台候选映射：失败任务的真候选不能被静默过滤。

缺陷背景：R01 崩掉的任务 route 为空、candidate_decisions 为空，候选落在
pending_review_candidates 里。按「只收可交付/待审」的过滤，这些真候选会被整条丢掉，
报表就成了「零候选」——把一次真实生成说成没产出。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_frozen_review.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_frozen_review", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


def _failed_state() -> dict:
    """R01 未完成：有候选，但 route 与 candidate_decisions 都为空。"""
    return {
        "candidates": [],
        "pending_review_candidates": [
            {"candidate_id": "a1", "headline": "旁白讲狙击手伪装焦尸骗过敌军",
             "rendered_title": "【剧情演绎】旁白讲狙击手伪装焦尸骗过敌军",
             "slot_id": "core_summary", "mechanical_checks": {"status": "fail", "errors": []}},
            {"candidate_id": "a2", "headline": "旁白：狙击手装焦尸骗过敌军",
             "rendered_title": "【剧情演绎】旁白：狙击手装焦尸骗过敌军",
             "slot_id": "core_summary", "mechanical_checks": {"status": "pass", "errors": []}},
        ],
    }


def test_failed_task_candidates_are_not_masked_by_default(mod):
    """默认口径不变：没有系统判定的候选不算待审，免得把未评审包装成可交付。"""
    assert mod.pick_candidates(_failed_state()) == []


def test_include_failed_surfaces_unreviewed_instead_of_hiding(mod):
    picked = mod.pick_candidates(_failed_state(), include_failed=True)
    assert {item["candidate_id"] for item in picked} == {"a1", "a2"}
    # 状态必须自证「系统没给出判定」，不能借用 needs_review/ready_for_human_review 冒充。
    assert {item["status"] for item in picked} == {"unreviewed"}


def test_include_failed_keeps_near_duplicates_for_human(mod):
    """两条标题相似度 0.92，默认去重会只剩一条；失败任务里不替人决定藏哪条。"""
    picked = mod.pick_candidates(_failed_state(), include_failed=True)
    assert len(picked) == 2
    for item in picked:
        assert len(item["title_sha256"]) == 64


def test_completed_task_still_filters_undeliverable(mod):
    """有 route 的任务不受影响：被判 rewrite/reject 的候选仍不进人工评审。"""
    state = {
        "candidates": [
            {"candidate_id": "b1", "rendered_title": "【剧情演绎】被拒的标题",
             "slot_id": "core_summary", "mechanical_checks": {"status": "fail", "errors": []}},
        ],
        "route": {"recommended_candidate_id": None, "eligible_candidate_ids": []},
        "candidate_decisions": [{"candidate_id": "b1", "status": "rewrite"}],
    }
    assert mod.pick_candidates(state, include_failed=True) == []


def _state_with_overlong_candidates() -> dict:
    """真实批次（BV1PwsR6oEKW）：模型写了 3 条，2 条超字数被机械判否。"""
    return {
        "delivery_candidates": [
            {"candidate_id": "cand-1", "rendered_title": "【剧情演绎】夜里送吃的还叮嘱他回去休息",
             "slot_id": "source_rewrite", "mechanical_checks": {"status": "pass", "errors": []}},
        ],
        "candidates": [
            {"candidate_id": "cand-1", "rendered_title": "【剧情演绎】夜里送吃的还叮嘱他回去休息",
             "slot_id": "source_rewrite", "mechanical_checks": {"status": "pass", "errors": []}},
            {"candidate_id": "cand-2", "rendered_title": "【剧情演绎】校园恋爱短剧：她送吃的他体贴回应",
             "slot_id": "core_summary", "mechanical_checks": {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED"}]}},
            {"candidate_id": "cand-3", "rendered_title": "【剧情演绎】知道你没吃饭，她夜里送吃的来了",
             "slot_id": "core_hook", "mechanical_checks": {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED"}]}},
        ],
        "route": {"action": "handoff", "recommended_candidate_id": "cand-1", "eligible_candidate_ids": ["cand-1"]},
        "candidate_decisions": [
            {"candidate_id": "cand-1", "status": "ready_for_human_review"},
            {"candidate_id": "cand-2", "status": "rewrite"},
            {"candidate_id": "cand-3", "status": "rewrite"},
        ],
    }


def test_overlong_candidates_are_not_sent_for_human_judgement(mod):
    """超字数不该占人眼：人只能再判一次「超字数」，得不出质量结论。"""
    picked = mod.pick_candidates(_state_with_overlong_candidates(), include_rewrite=True)
    assert [item["candidate_id"] for item in picked] == ["cand-1"]


def test_every_slot_gets_shown_not_just_the_first_three(mod):
    """上限按槽位算。曾写死 [:3]：某槽位有两组候选时，排在前面的槽位会把后面整槽挤掉，
    人看不到真交付的那条标题（实测 v28 交付了 source_rewrite，台上却没它）。"""
    state = {
        "candidates": [
            {"candidate_id": "g-sr", "rendered_title": "生成侧：红裙与画中女孩", "slot_id": "source_rewrite",
             "mechanical_checks": {"status": "pass", "errors": []}},
            {"candidate_id": "r-sr", "rendered_title": "复核侧：首次走红毯受访聊酒红裙与画作", "slot_id": "source_rewrite",
             "mechanical_checks": {"status": "pass", "errors": []}},
            {"candidate_id": "g-cs", "rendered_title": "生成侧：第一次走红毯聊画作", "slot_id": "core_summary",
             "mechanical_checks": {"status": "pass", "errors": []}},
            {"candidate_id": "r-cs", "rendered_title": "复核侧：把红裙造型画进画里", "slot_id": "core_summary",
             "mechanical_checks": {"status": "pass", "errors": []}},
            {"candidate_id": "g-ch", "rendered_title": "生成侧：紧张到出来就好", "slot_id": "core_hook",
             "mechanical_checks": {"status": "pass", "errors": []}},
        ],
        "route": {"action": "handoff", "recommended_candidate_id": "r-cs", "eligible_candidate_ids": ["g-sr", "g-cs", "r-cs"]},
        "candidate_decisions": [
            {"candidate_id": "g-sr", "status": "ready_for_human_review"},
            {"candidate_id": "r-sr", "status": "rewrite"},
            {"candidate_id": "g-cs", "status": "ready_for_human_review"},
            {"candidate_id": "r-cs", "status": "ready_for_human_review"},
            {"candidate_id": "g-ch", "status": "ready_for_human_review"},
        ],
    }
    picked = mod.pick_candidates(state, include_rewrite=True)
    assert {item["slot_id"] for item in picked} == {"source_rewrite", "core_summary", "core_hook"}
    assert "g-sr" in {item["candidate_id"] for item in picked}


def test_per_slot_cap_still_bounds_the_page(mod):
    """不是无上限：同一槽位堆再多也只送 MAX_PER_SLOT 条，超出的不占人眼。"""
    candidates, decisions = [], []
    titles = ["她把红裙穿成画里那件", "紧张到走上台才松口气", "受访聊第一次走红毯", "网友黑条与采访并置", "镜头扫过签名墙"]
    for i in range(mod.MAX_PER_SLOT + 2):
        candidates.append({"candidate_id": f"x{i}", "rendered_title": titles[i],
                           "slot_id": "core_hook", "mechanical_checks": {"status": "pass", "errors": []}})
        decisions.append({"candidate_id": f"x{i}", "status": "ready_for_human_review"})
    state = {"candidates": candidates,
             "route": {"action": "handoff", "recommended_candidate_id": "x0", "eligible_candidate_ids": [f"x{i}" for i in range(len(candidates))]},
             "candidate_decisions": decisions}
    assert len(mod.pick_candidates(state)) == mod.MAX_PER_SLOT


def test_mechanical_dropped_discloses_what_the_model_actually_wrote(mod):
    """但必须披露：台上只剩一条时，读者会以为模型只写了一条。"""
    dropped = mod.dropped_mechanical(_state_with_overlong_candidates())
    assert [item["candidate_id"] for item in dropped] == ["cand-2", "cand-3"]
    assert {item["slot_id"] for item in dropped} == {"core_summary", "core_hook"}
    assert [item["title_length"] for item in dropped] == [22, 21]
    assert all(item["codes"] == ["G4_LENGTH_EXCEEDED"] for item in dropped)


def test_mechanical_dropped_is_empty_when_nothing_was_dropped(mod):
    state = _state_with_overlong_candidates()
    state["candidates"] = [state["candidates"][0]]
    assert mod.dropped_mechanical(state) == []
