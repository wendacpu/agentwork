"""2026-11-03 Evolink 复测暴露的两条误拦，锁住修复。

两条都不是模型判断问题，是程序把不该拦的拦了，人工判定全部可用：

1. 结构片段的单元名是 turn-2 / axis-1，语义单元叫 su-N。允许集合只收 semantic_units 时，
   绑结构片段的候选一律被判 G4_INVALID_REFERENCE「unit_id 不在当前任务范围」。
2. 边界给的「标题可写画面可见的看球动作」是改法指引，标题照做即满足；过去按「必须表达
   限定」处理，于是要求标题写出来源，而标题写的正是被建议写的那个动作。

两次都是「初评与重评各写一套判定」造成的分叉（第 2 条是限定豁免链漏了一个分支），
所以这里同时锁住单点行为与两条路径共用同一函数。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from agent_system import pipeline
from agent_system.pipeline import _allowed_unit_ids, _has_explicit_source_relation, _limit_needs_body


def _understanding(*unit_ids):
    return SimpleNamespace(semantic_units=[SimpleNamespace(unit_id=u) for u in unit_ids])


def _boundary(decisions):
    return SimpleNamespace(unit_decisions=[SimpleNamespace(unit_id=u, decision=d) for u, d in decisions])


# --- 缺陷 1：结构片段的 unit_id 被判成任务范围外 ---

def test_structural_unit_is_in_scope():
    """turn-2 是抽帧结构片段，不是语义单元，但它指向的内容就在这条视频里。"""
    allowed = _allowed_unit_ids(_understanding("su-1", "su-2"), _boundary([]), [{"highlight_id": "h1", "unit_id": "turn-2"}])
    assert "turn-2" in allowed
    assert {"su-1", "su-2"} <= allowed


def test_excluded_unit_stays_excluded():
    """放宽范围不能把边界明确排除的单元放回来。"""
    allowed = _allowed_unit_ids(_understanding("u1", "u4"), _boundary([("u4", "excluded")]), [])
    assert "u4" not in allowed
    assert "u1" in allowed


def test_unit_mentioned_only_by_boundary_is_in_scope():
    """边界点到过、但理解域没列出的单元同样在范围内（曾因此整条视频零交付）。"""
    allowed = _allowed_unit_ids(_understanding("u1"), _boundary([("u7", "limited")]), [])
    assert "u7" in allowed


def test_highlight_object_and_dict_both_supported():
    """初评传对象、重评传存档 dict，同一函数必须都能读。"""
    as_object = SimpleNamespace(highlight_id="h1", unit_id="turn-9")
    as_dict = {"highlight_id": "h1", "unit_id": "turn-9"}
    assert _allowed_unit_ids(_understanding("su-1"), _boundary([]), [as_object]) == _allowed_unit_ids(_understanding("su-1"), _boundary([]), [as_dict])


def test_review_paths_share_one_unit_set_builder():
    """初评与重评必须共用同一个构造函数——分叉正是这次误拦的来源。"""
    source = inspect.getsource(pipeline.Pipeline)
    assert source.count("_allowed_unit_ids(") == 2, "初评与重评应各调用一次共用函数"
    assert source.count("u.unit_id for u in understanding.semantic_units") == 0, "不得再各写一套集合构造"


# --- 缺陷 2：写作指引型限定被当成必须表达限定 ---

@pytest.mark.parametrize("limit,headline", [
    ('"上班时偷看"为字幕条转述，标题可写画面可见的看球动作', "两员工偷看球赛被球场大屏抓包"),
    ('"假装认真工作"含动机推断，宜写为转身面向电脑等可观察动作', "两人转身盯电脑"),
])
def test_writing_guidance_needs_no_body_limit(limit, headline):
    """标题已经照指引改了，就不该再要求它写出来源。"""
    assert _limit_needs_body(limit, [], headline) is False


def test_guidance_still_applies_when_headline_repeats_rejected_fact():
    """标题写了被否定的那个子事实，说明没照做，限定仍须处理。"""
    limit = '"假装认真工作"含动机推断，宜写为转身面向电脑等可观察动作'
    assert _limit_needs_body(limit, [], "两人假装认真工作被抓包") is True


@pytest.mark.parametrize("limit", [
    '"被转播镜头拍到并投放球场大屏"为字幕条转述，非画面直接可见',
    "赛事信息来自字幕条转引，非本视频一手核实",
])
def test_real_source_limit_is_not_weakened(limit):
    """来源披露不是写作指引：标题引用了转述内容时仍须表达来源，不因本次修复被放过。"""
    assert _limit_needs_body(limit, [], "两员工偷看球赛被球场大屏抓包") is True


def test_guidance_without_headline_stays_strict():
    """没传标题时按必须表达处理，避免调用方漏参导致静默放宽。"""
    assert _limit_needs_body('"上班时偷看"为字幕条转述，标题可写画面可见的看球动作', []) is True


# --- 缺陷 3：冒号式归属被当成「未表达转述关系」 ---

@pytest.mark.parametrize("headline", [
    "萨戈夫：德文是传奇，我只赢过一次",
    "萨戈夫赛后：不认为自己已强于德文",
    "作者：财经媒体误导性强，投资需核实",
    "萨戈夫称不会说自己强于德文",
])
def test_named_source_is_attributed(headline):
    """「主体：内容」是标题最常用的归属写法，读者不会误以为是平台在断言。"""
    assert _has_explicit_source_relation(headline) is True


@pytest.mark.parametrize("headline", [
    "通报：某地发生事故",
    "有消息：某地发生事故",
    "视频：两员工偷看球赛",
])
def test_generic_or_carrier_source_still_missing(headline):
    """泛称和载体不是信源：读者看完不知道是谁说的。"""
    assert _has_explicit_source_relation(headline) is False


def test_headline_without_any_source_still_missing():
    assert _has_explicit_source_relation("两员工偷看球赛被球场大屏抓包") is False


def test_event_phrase_before_colon_is_not_a_source():
    """「黄多多首次走红毯：」是事件不是信源，不能因此认定已完成归属。"""
    assert _has_explicit_source_relation("黄多多首次走红毯：紧张到出来才好") is False
    assert _has_explicit_source_relation("黄多多：紧张到出来才好") is True
