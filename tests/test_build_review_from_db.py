"""`build_review_from_db` 的轮次判定回归测试。

两个真实事故：候选因为 `generation_round` 写坏，被静默丢出评审台 / 被当成重写轮。
这里把它们钉住——轮次一律以 candidate_id 为准。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("build_review_from_db", ROOT / "scripts" / "build_review_from_db.py")
brd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brd)


def _candidate(cid: str, headline: str, generation_round=None, errors=None):
    candidate = {"candidate_id": cid, "headline": headline}
    if generation_round is not None:
        candidate["generation_round"] = generation_round
    if errors:
        candidate["mechanical_checks"] = {"errors": [{"code": c} for c in errors]}
    return candidate


def test_round_parses_both_rewrite_naming_conventions():
    """历史数据里两种命名并存，只认一种会把另一批重写候选算成首轮。"""
    assert brd._round_of(_candidate("cand-2-r1-1", "标题")) == 1
    assert brd._round_of(_candidate("cand-2-r-1-2", "标题")) == 1
    assert brd._round_of(_candidate("cand-3-r12-4", "标题")) == 12


def test_round_treats_candidates_without_rewrite_suffix_as_first_round():
    assert brd._round_of(_candidate("cand-1", "标题")) == 0
    assert brd._round_of(_candidate("a3-1", "标题")) == 0


def test_round_ignores_the_model_written_generation_round():
    """generation_round 由模型输出，写 -1 或 100 都不该改变轮次归属。"""
    assert brd._round_of(_candidate("cand-1", "标题", generation_round=-1)) == 0
    assert brd._round_of(_candidate("cand-1", "标题", generation_round=100)) == 0
    assert brd._round_of(_candidate("cand-2-r-1-1", "标题", generation_round=7)) == 1


def test_compliant_rewrite_candidate_is_not_silently_dropped():
    """R18 事故：轮次写 -1 的合规候选曾被判「未知」后整条丢弃。"""
    state = {"candidates": [
        _candidate("cand-1", "博主拍屏：iPad mini 8售价5499元起", generation_round=0,
                   errors=["G4_LENGTH_EXCEEDED"]),
        _candidate("cand-3-r-1-2", "博主称iPad mini 8售5499起", generation_round=-1),
    ]}
    picked, base, dropped = brd.earliest_compliant(state)
    assert [c[0] for c in picked] == ["cand-3-r-1-2"]
    assert base == 1
    assert [d["candidate_id"] for d in dropped] == ["cand-1"]


def test_first_round_candidate_with_pending_limit_stays_reviewable():
    """R15 事故：轮次写 1 的首轮候选曾被当作重写轮，首轮桶变空→误判零合规。

    2026-11-04 又发现另一面：财经三条候选只被 G1（信源/限定待核）挡住，
    按「有 blocking 就剔」同样被判成零合规候选，人根本看不到那三条标题。
    现在 G1 级别的待核留在台上（状态 needs_review），只有 G4 硬约束才剔。
    """
    state = {"candidates": [
        _candidate("cand-1", "司机昏迷被困，辅警破窗救人手被划伤", generation_round=1,
                   errors=["G1_MISSING_LIMIT"]),
    ]}
    picked, base, dropped = brd.earliest_compliant(state)
    assert [c[0] for c in picked] == ["cand-1"]
    assert base == 0
    assert dropped == []


def test_length_failure_alone_is_still_excluded_from_review():
    """超长是交付硬约束，人工不再判一次「超了」；只留在只读失败清单。"""
    state = {"candidates": [
        _candidate("cand-1", "司机昏迷被困，辅警破窗救人", errors=["G4_LENGTH_EXCEEDED"]),
    ]}
    picked, base, dropped = brd.earliest_compliant(state)
    assert picked == [] and base is None
    assert [d["candidate_id"] for d in dropped] == ["cand-1"]


def test_earliest_compliant_returns_the_earliest_clean_round():
    """首轮有合规候选时就用首轮，不该跳到重写轮。"""
    state = {"candidates": [
        _candidate("cand-1", "首轮标题", generation_round=0),
        _candidate("cand-1-r-1-1", "重写标题", generation_round=1),
    ]}
    picked, base, dropped = brd.earliest_compliant(state)
    assert [c[0] for c in picked] == ["cand-1"]
    assert base == 0
