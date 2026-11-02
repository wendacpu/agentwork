"""限定闸门不能要求标题写出「本条不得推断细节」这种审核话。

真实故障（2026-11-02 Flash 对照批次，202 条候选逐条重算）：
同一批候选在改前的 G1_MISSING_LIMIT 判否 75 条，改后 3 条，其中 73 条是误杀。
两类根因：
  1. limited_cited 已按「至少一条限定需要正文表达」筛过 claim，随后又把该 claim 的
     全部 required_limits 摊回 needs_other——被豁免的禁令/保留类限定重新进来，
     而禁令永远写不进标题，候选必死（25a1fe「博主：体测跑前9个热身动作」9 条全灭）。
  2. 「须作为现场救援描述，不得推断出警过程细节」被前半的「须」读成正向要求。
剩下 3 条经人工核对都是真有归属义务没写（新闻来源、博主自述、假设限定），保持拦截。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.pipeline import (
    _limit_attribution_scope_absent,
    _limit_is_expressed,
    _limit_is_prohibition,
    _mechanical_check,
)


def _decision(claim_id: str = "c1", *limits: str):
    return SimpleNamespace(claim_id=claim_id, decision="limited", required_limits=list(limits),
                           attribution_required=False, required_source_phrase="",
                           verification_required=False)


def _check(headline: str, decisions):
    candidate = {"candidate_id": "a1", "headline": headline, "prefix_labels": [],
                 "claim_ids": [d.claim_id for d in decisions], "evidence_ids": ["e1"],
                 "highlight_id": "h1", "unit_id": "u1", "generation_schema_errors": []}
    return _mechanical_check(candidate, allowed_claims={d.claim_id for d in decisions},
                             allowed_highlights={"h1"}, evidence_ids={"e1"},
                             max_chars=30, allowed_units={"u1"}, claim_decisions=decisions)


# --- 根因 1：同一 claim 上的禁令/保留类限定不得被摊回正文要求 ---

def test_prohibition_limit_on_same_claim_does_not_kill_the_candidate():
    """归属已写、同 claim 上还有一条 ASR 禁令：这条标题可用（25a1fe 实况）。"""
    decisions = [_decision("c1", "须归属为博主个人说法", "动作名称ASR转写不清，不得逐字采信")]
    codes = {e["code"] for e in _check("博主：体测跑前9个热身动作", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" not in codes


def test_attribution_still_required_when_title_has_none():
    """放宽的是禁令，不是归属：同一 claim 却不写归属，仍然要拦。"""
    decisions = [_decision("c1", "须归属为博主个人说法", "动作名称ASR转写不清，不得逐字采信")]
    codes = {e["code"] for e in _check("体测跑前9个热身动作", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" in codes


def test_retention_limit_is_not_flattened_back():
    decisions = [_decision("c1", "须归属为博主观点", "须保留“山野院子”等具体场景")]
    codes = {e["code"] for e in _check("博主做渣渣牦牛肉，全程无口播无字幕", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" not in codes


# --- 根因 2：「须作为…描述，不得推断…」是禁令，不是正向要求 ---

@pytest.mark.parametrize("limit", [
    "须作为现场救援描述，不得推断出警过程细节",
    "须作为救援动作描述，不得推断破窗具体方式与工具",
    "须按画面呈现描述，不得补出未展示的步骤",
])
def test_meta_frame_plus_prohibition_is_a_prohibition(limit):
    assert _limit_is_prohibition(limit) is True


def test_real_positive_attribution_is_not_a_prohibition():
    assert _limit_is_prohibition("须归属为旁白，不得写成史实定论") is False


def test_rescue_news_title_passes_the_frame_limit():
    decisions = [_decision("c1", "须作为现场救援描述，不得推断出警过程细节",
                           "须作为救援动作描述，不得推断破窗具体方式与工具")]
    codes = {e["code"] for e in _check("越野车撞树司机昏迷，辅警破窗救援", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" not in codes


# --- 归属限定绑定了具体子事实：标题没提该子事实就没有归属义务 ---

def test_scoped_attribution_is_skipped_when_subject_absent():
    limit = "可写司机昏迷斜靠座椅，但病因须归属"
    assert _limit_attribution_scope_absent(limit, "越野车撞树司机昏迷，辅警破窗救援") is True
    assert _limit_attribution_scope_absent(limit, "越野车撞树，司机病因待查") is False


def test_scoped_attribution_does_not_relax_plain_attribution():
    assert _limit_attribution_scope_absent("须归属为博主自述", "女生居家一周四练day1：哑铃练背") is False


def test_plain_attribution_still_blocks_when_missing():
    """剩下来的真拦截：博主自创训练计划没写归属。"""
    decisions = [_decision("c1", "须归属为博主自述，不得写成系统确认的普遍适用结论")]
    codes = {e["code"] for e in _check("女生居家一周四练day1：哑铃练背", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" in codes


# --- 范围/条件/不确定性类限定仍然必须表达（防止顺手放太宽） ---

@pytest.mark.parametrize("limit,headline", [
    ("须写明时间范围", "黄多多自述红裙画作"),
    ("须注明未经证实", "某地一商场起火"),
    ("保留“涉嫌”等程度限定", "女性化单品怎么穿不低俗"),
])
def test_range_and_uncertainty_limits_still_require_expression(limit, headline):
    assert _limit_is_expressed(limit, headline) is False


@pytest.mark.parametrize("limit,headline", [
    ("须写明时间范围", "2026年上半年消费趋势"),
    ("须注明未经证实", "网传某地商场起火，尚未证实"),
    ("保留“涉嫌”等程度限定", "单品单穿涉嫌低俗"),
])
def test_range_and_uncertainty_limits_accept_real_expression(limit, headline):
    assert _limit_is_expressed(limit, headline) is True


def test_scope_limit_with_no_checkable_anchor_is_not_killed():
    """机械判不了的具体限定放行，交给评审——默认判死就是零交付。"""
    assert _limit_is_expressed("须保持稳定的呈现节奏", "某视频标题") is True
