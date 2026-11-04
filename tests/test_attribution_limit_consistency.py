"""归属两个判据必须一致：同一个标题不能既被判「已归属」又被判「缺限定」。

真实故障（2026-11-02，canary_v25 黄多多红毯采访 BV1u9HA6JEdw）：

  boundary 把 c3–c9 全判成 limited + required_limits=["须归属为受访者自述"]
  G1_SOURCE_ATTRIBUTION_MISSING 用 _ATTRIBUTION_MARKERS 判断，认「自述」「受访」
  G1_MISSING_LIMIT 另用一份 limit_phrases，只认「据/称/网友/不…」，不认「自述」「受访」
  → 六个候选全被判 G1_MISSING_LIMIT，全进重写轮，重写后照样被判，最后零交付。

两个检查看的是同一件事（标题有没有把话归给受访者），词表不一致就会互相打脸。
这里锁住：归属表达一律走 _ATTRIBUTION_MARKERS，两条判据对同一标题给同一个答案。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.pipeline import _ATTRIBUTION_MARKERS, _has_explicit_source_relation, _mechanical_check


def _limited(claim_id: str = "c1", limit: str = "须归属为受访者自述"):
    return SimpleNamespace(claim_id=claim_id, decision="limited", required_limits=[limit],
                           attribution_required=True, required_source_phrase="受访者称",
                           verification_required=False)


def _check(headline: str, decisions=None):
    candidate = {"candidate_id": "a1", "headline": headline, "prefix_labels": [], "claim_ids": ["c1"],
                 "evidence_ids": ["e1"], "highlight_id": "h1", "unit_id": "u1", "generation_schema_errors": []}
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"},
                             max_chars=24, allowed_units={"u1"}, claim_decisions=decisions or [_limited()])


def _codes(headline: str):
    return {e["code"] for e in _check(headline)["errors"]}


@pytest.mark.parametrize("headline", [
    "黄多多自述：常画红裙女孩",
    "黄多多受访：画里穿红裙的是自己",
    "黄多多讲述红裙来历",
    "黄多多表示裙子上的画是自己画的",
    "黄多多透露画作了由来",
])
def test_attribution_phrases_satisfy_the_limit(headline):
    assert "G1_MISSING_LIMIT" not in _codes(headline), f"{headline} 已经表达了归属，不该判缺限定"


def test_source_phrase_alternatives_do_not_require_both_spellings():
    phrase = "据上游新闻报道/报道称"
    assert _has_explicit_source_relation("二仙桥大爷再被拦，报道称警方未罚款", [phrase])
    assert _has_explicit_source_relation("据上游新闻报道警方未罚款", [phrase])
    assert not _has_explicit_source_relation("警方称已收到举报", [phrase])
    assert not _has_explicit_source_relation("二仙桥大爷再被拦，警方未罚款", [phrase])
    assert not _has_explicit_source_relation("报道称警方未罚款", ["谭乔称"])
    assert not _has_explicit_source_relation("报道称警方未罚款", [phrase, "谭乔称"])
    assert not _has_explicit_source_relation("警方抓人，网友称案件已侦破", ["警方称/有关部门称"])
    assert not _has_explicit_source_relation("网友称案件已侦破", ["警方称/有关部门称，须保留事实"])
    assert _has_explicit_source_relation("警方称案件已侦破", ["警方称/有关部门称"])


def test_short_adverbial_between_subject_and_verb_still_counts():
    """真实误拦：标题写「萨戈夫赛后称…」「博主赛后受访：…」，归属对象确实写了。"""
    assert _has_explicit_source_relation("萨戈夫赛后称不认为自己强于德文", ["萨戈夫称"])
    assert _has_explicit_source_relation("萨戈夫赛后受访：不认为自己更强", ["萨戈夫表示"])
    assert _has_explicit_source_relation("谭乔证实此次未罚款", ["谭乔证实"])
    assert _has_explicit_source_relation("博主：财经快讯误导性强", ["博主"])


def test_title_without_any_attribution_is_registered_for_review():
    """漏的是「引用了 limited 陈述却一句归属都没有」，不是「没写某个特定词」。

    2026-11-04 起这条不再由词法否决（词法判不出语气，实测在 V 集上 18/24 是误伤），
    而是把限定登记成 limit_requirements，由 R01 在 limit_checks 里逐条判定。
    所以这里断言的是「被登记下来交给评审」，不再是「被词法判错」。
    """
    result = _check("黄多多首次走红毯：紧张到出来才好")
    assert "G1_MISSING_LIMIT" not in {e["code"] for e in result["errors"]}
    assert result["limit_requirements"], "没有归属表达的标题必须把限定登记下来交给评审"
    assert {r["limit"] for r in result["limit_requirements"]} == {"须归属为受访者自述"}


@pytest.mark.parametrize("marker", [m for m in _ATTRIBUTION_MARKERS if len(m) >= 2])
def test_every_multi_char_marker_is_accepted_by_both_checks(marker):
    """两条判据共用一个词表；任何一个标记词被其中一条漏掉，就是这次这类 bug 的复发。"""
    headline = f"黄多多{marker}红裙画作"
    assert _has_explicit_source_relation(headline) is True
    assert "G1_MISSING_LIMIT" not in _codes(headline)


@pytest.mark.parametrize("limit", ["须写明时间范围", "须注明未经证实", "须标明为传闻而非结论"])
def test_range_limit_is_not_satisfied_by_attribution_alone(limit):
    """范围/条件类限定不能靠「某人说」顶掉——那是两件事，别在放宽归属时顺手放宽这个。

    判定不再由词法给出（词法只测得「限定没写在标题里」，测不出「意思到没到」），
    但这条限定仍然必须被登记下来、由评审逐条回答，不能因为标题有归属就自动免除。
    """
    result = _check("黄多多自述红裙画作", [_limited(limit=limit)])
    assert "G1_MISSING_LIMIT" not in {e["code"] for e in result["errors"]}
    assert [r["limit"] for r in result["limit_requirements"]] == [limit], f"{limit} 需要正文里的范围表达，不是归属语"


def test_attribution_limit_and_range_limit_are_judged_separately():
    """同一条候选同时挂着两类限定时：只写归属应只满足归属那一半。

    新契约：两条限定都登记进 limit_requirements 交给评审逐条判定，归属那条可能
    判 satisfied，范围那条留给模型/人审——不再由词法直接判死。
    """
    from agent_system.pipeline import _limit_is_attribution
    assert _limit_is_attribution("须归属为受访者自述") is True
    assert _limit_is_attribution("须注明未经证实") is False
    decisions = [_limited(limit="须归属为受访者自述"), _limited(limit="须注明未经证实")]
    result = _check("黄多多自述红裙画作", decisions)
    assert "G1_MISSING_LIMIT" not in {e["code"] for e in result["errors"]}
    assert {r["limit"] for r in result["limit_requirements"]} == {"须归属为受访者自述", "须注明未经证实"}


# —— 2026-11-02 对照实验（compare_v2，40 条 Flash 跑批）暴露的三类误杀 ——
#
# 系统臂 36 条候选里 14 条被机械判否，其中 9 条人工判定是「可用」。错误码集中在
# G1_MISSING_LIMIT，而看具体限定原文，被误杀的原因是两个裸词匹配：
#   「须保留活动**名称**与地点」  → 名词「名称」里的「称」被当成信源归属
#   「OCR 数据**对应**关系不明确」 → 「对应」里的「应」被当成正向要求词
# 外加一类语义混淆：「须保留 X / 限定为 X / 须以 X 为准」要求的是「别写错」，
# 不需要标题额外写出内容，却被要求复述测试条件与未引用的数值口径。


@pytest.mark.parametrize("limit", ["须保留活动名称与地点", "须保留为口播/标题所述的食物名称"])
def test_retention_limit_is_not_read_as_attribution(limit):
    """「名称」是名词，不是归属动词；这类限定不该要求标题出现信源。"""
    from agent_system.pipeline import _limit_is_attribution, _limit_needs_body

    assert _limit_is_attribution(limit) is False
    assert _limit_needs_body(limit, []) is False


def test_real_attribution_limit_survives_the_retention_exemption():
    """「须保留来源归属」虽含「须保留」，但它说的就是归属，仍必须表达。"""
    from agent_system.pipeline import _limit_needs_body

    assert _limit_needs_body("须保留来源归属", []) is True


@pytest.mark.parametrize("limit", [
    "限定为原神+微信视频测试条件",
    "具体增速数值须以官方发布为准，ASR转写存在噪声，不得直接引用14.7%",
])
def test_non_expressive_limits_need_no_body_expression(limit):
    """条件/口径类限定：标题照材料原样写就已满足，不要求复述条件或数值。"""
    from agent_system.pipeline import _limit_needs_body

    assert _limit_needs_body(limit, []) is False


def test_incidental_positive_token_does_not_turn_prohibition_into_a_requirement():
    """「对应」里的「应」不是正向要求，这条是纯禁令。"""
    from agent_system.pipeline import _limit_is_prohibition

    assert _limit_is_prohibition("OCR数据对应关系不明确，不得将48.5%、39.3%、28.0%直接对应到具体行业") is True


def test_retention_only_limit_no_longer_kills_a_compliant_title():
    """端到端：标题老实保留了活动名与地点，就不该再被判缺限定。"""
    decisions = [_limited(limit="须保留活动名称与地点")]
    codes = {e["code"] for e in _check("古力娜扎东莞钻石世家星光之夜现场记录", decisions)["errors"]}
    assert "G1_MISSING_LIMIT" not in codes
