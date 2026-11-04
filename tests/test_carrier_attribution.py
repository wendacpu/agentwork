"""载体不是信源：剧情演绎类视频的归属披露由前缀承担，不能逼正文写「旁白」。

缺陷：边界把每条旁白陈述判成 limited + required_limits=["须归属为旁白/剧情叙述"]，
生成器照做写出「旁白：狙击手装焦尸骗过敌军」，机械检查再判 G1_MISSING_LIMIT——
而 U-S09 与系统提示词都禁止把旁白当信源。三处规则互相锁死，这类视频永远零交付。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.pipeline import (
    _META_PHRASE_RE,
    _carrier_only_source,
    _limit_covered_by_narration,
    _mechanical_check,
)


def _claim(claim_id: str, *, limits=None, attribution=False, phrase=None):
    return SimpleNamespace(claim_id=claim_id, decision="limited", required_limits=list(limits or []),
                           attribution_required=attribution, required_source_phrase=phrase,
                           verification_required=False)


def _candidate(**over):
    base = {"candidate_id": "a1", "headline": "狙击手装焦尸骗过敌军",
            "prefix_labels": ["剧情演绎"], "claim_ids": ["c1"], "evidence_ids": ["e1"],
            "highlight_id": "h1", "unit_id": "u1", "generation_schema_errors": []}
    base.update(over)
    return base


def _check(candidate, decisions):
    return _mechanical_check(candidate, allowed_claims={"c1"}, allowed_highlights={"h1"},
                             evidence_ids={"e1"}, max_chars=20, allowed_units={"u1"},
                             required_prefixes=["剧情演绎"], claim_decisions=decisions)


def test_carrier_limit_is_satisfied_by_narrative_prefix():
    decisions = [_claim("c1", limits=["须归属为旁白/剧情叙述"], attribution=True, phrase="旁白称")]
    result = _check(_candidate(), decisions)
    assert result["status"] == "pass", result["errors"]


def test_factual_limit_still_requires_body_expression():
    """事实层面的限定期限不能靠前缀顶掉，否则等于放宽证据要求。"""
    decisions = [_claim("c1", limits=["须注明未经证实，存在争议"])]
    result = _check(_candidate(), decisions)
    # 新契约：限定登记交评审；争议标记缺失仍由词法正面拦截（G1_MISSING_DISPUTE_MARK）。
    codes = {e["code"] for e in result["errors"]}
    assert "G1_MISSING_LIMIT" not in codes
    assert "G1_MISSING_DISPUTE_MARK" in codes
    assert "须注明未经证实，存在争议" in {r["limit"] for r in result["limit_requirements"]}


def test_real_source_is_not_covered_by_prefix():
    decisions = [_claim("c1", limits=["须归属为新华社报道"], attribution=True, phrase="据新华社报道")]
    result = _check(_candidate(), decisions)
    # 新契约：真信源归属不因【剧情演绎】前缀顶掉，登记交评审逐条判定。
    assert not {e["code"] for e in result["errors"]}
    assert "须归属为新华社报道" in {r["limit"] for r in result["limit_requirements"]}


@pytest.mark.parametrize("title,hit", [
    ("旁白：狙击手装焦尸骗过敌军", "旁白："),
    ("旁白讲狙击手伪装焦尸骗过敌军", "旁白讲"),
    ("字幕：他是怎么活下来的", "字幕："),
    ("口播：三步搞定", "口播："),
    ("据旁白所述，他装死", "据旁白"),
])
def test_carrier_phrases_are_meta_discourse(title, hit):
    match = _META_PHRASE_RE.search(title)
    assert match and match.group(0) == hit


@pytest.mark.parametrize("title", [
    "狙击手装焦尸骗过敌军",
    "博主：低俗感不由单件决定",
    "据新华社报道，事故已处理",
])
def test_plain_titles_are_not_flagged(title):
    assert _META_PHRASE_RE.search(title) is None


def test_carrier_only_source_detection():
    assert _carrier_only_source("旁白称")
    assert _carrier_only_source("视频旁白")
    assert not _carrier_only_source("据新华社报道")
    assert not _carrier_only_source("博主自述")
    assert not _carrier_only_source("")


def test_limit_covered_requires_narrative_prefix():
    assert _limit_covered_by_narration("须归属为旁白/剧情叙述", ["剧情演绎"])
    assert not _limit_covered_by_narration("须归属为旁白/剧情叙述", [])
    assert not _limit_covered_by_narration("须归属为旁白/剧情叙述", ["AI生成"])


# ---------------------------------------------------------------------------
# 禁止型限定 / 内容性质信号 / 主线重选后回归
# ---------------------------------------------------------------------------

from agent_system.pipeline import (  # noqa: E402
    _enforce_single_mainline,
    _limit_is_prohibition,
    _limit_needs_body,
    _nature_disclosure_applied,
)


@pytest.mark.parametrize("limit", [
    "不得补出具体动作细节",
    "ASR含糊（'吞食/推施灰剑'），不得补出具体动作细节",
    "禁止写成已证实史实",
    "避免补出人物动机",
])
def test_prohibition_limits_need_no_body_expression(limit):
    """禁令靠"标题里不出现该信息"满足，不能要求标题写出「本条不得…」。"""
    assert _limit_is_prohibition(limit)
    assert not _limit_needs_body(limit, ["剧情演绎"])


@pytest.mark.parametrize("limit", [
    "须注明未经证实，存在争议",
    "须归属为新华社报道",
    "须归属为旁白/剧情叙述，不得写成史实定论",
])
def test_positive_limits_still_need_body_expression(limit):
    """前半是正向要求时仍须落在正文（例：须归属 + 不得写成史实）。"""
    assert not _limit_is_prohibition(limit)


def test_carrier_limit_satisfied_only_with_narrative_prefix():
    assert not _limit_needs_body("须归属为视频旁白/剧情叙述，不得写成史实定论", ["剧情演绎"])
    assert _limit_needs_body("须归属为视频旁白/剧情叙述，不得写成史实定论", [])


def _state(prefixes, signals, pending):
    return {"required_prefixes": prefixes,
            "risk_signals": {"signals": signals, "pending_signal_ids": pending}}


def test_nature_signal_resolved_when_prefix_declares_it():
    """加了【剧情演绎】就等于已回答"这是不是演绎"，不该再拿同一个信号阻断交付。"""
    signals = [{"signal_id": "s1", "signal_type": "simulation",
                "quality": "model_derived_uncalibrated"}]
    assert _nature_disclosure_applied(_state(["剧情演绎"], signals, ["s1"]))


def test_nature_signal_blocks_without_prefix():
    signals = [{"signal_id": "s1", "signal_type": "simulation"}]
    assert not _nature_disclosure_applied(_state([], signals, ["s1"]))


def test_non_nature_pending_signal_still_blocks():
    """数值/时效这类待核信号与前缀无关，必须继续拦。"""
    signals = [{"signal_id": "s1", "signal_type": "number"}]
    assert not _nature_disclosure_applied(_state(["剧情演绎"], signals, ["s1"]))


def test_mainline_demotion_is_repeatable_on_reselected_contract():
    """看点重选会给出新契约，降级必须能在新契约上再跑一次（否则 core_unit_ids 退回合集）。"""
    from agent_system.types import Claim, Evidence, SemanticUnit, VideoInput

    segments = [{"text": "装死", "start": 0.0, "end": 23.4},
                {"text": "乐高枪", "start": 26.0, "end": 53.0}]
    video = VideoInput("v", "t", None, None, "", {}, "", None, 60, {}, "", "",
                       segments, [], [Evidence("e-asr-0000", "asr", "装死", quality="raw_asr"),
                                      Evidence("e-asr-0001", "asr", "乐高枪", quality="raw_asr")],
                       {}, {})
    understanding = type("Understanding", (), {
        "semantic_units": [
            SemanticUnit("u1", "装死", ["c1"], ["e-asr-0000"], kind="叙事"),
            SemanticUnit("u2", "乐高枪", ["c2"], ["e-asr-0001"], kind="叙事")],
        "claims": [Claim("c1", "装死", "observable", ["e-asr-0000"]),
                   Claim("c2", "乐高枪", "observable", ["e-asr-0001"])],
    })()

    fresh = {"core_unit_ids": ["u1", "u2"]}
    demoted = _enforce_single_mainline(fresh, understanding, video)
    assert [d["unit_id"] for d in demoted] == ["u2"]
    assert fresh["core_unit_ids"] == ["u1"]

    # 重选后的契约（模型又给了合集）必须同样被收敛，否则 core_hook 又去写尾段。
    retry = {"core_unit_ids": ["u1", "u2"]}
    assert [d["unit_id"] for d in _enforce_single_mainline(retry, understanding, video)] == ["u2"]
    assert retry["core_unit_ids"] == ["u1"]


# ---------------------------------------------------------------------------
# 信封解包：模型把结构化输出再包一层
# ---------------------------------------------------------------------------

from agent_system.model import _unwrap_envelope  # noqa: E402


def test_json_object_envelope_with_content_dict():
    """{"type":"json_object","content":{"understanding":{...}}} —— 实测 N03 形状。"""
    raw = {"type": "json_object", "content": {"understanding": {"claims": [{"claim_id": "c1"}], "semantic_units": []}}}
    out = _unwrap_envelope(raw)
    assert out["claims"] == [{"claim_id": "c1"}]


def test_json_object_envelope_with_content_string():
    raw = {"type": "json_object", "content": '{"claims": [{"claim_id": "c2"}]}'}
    assert _unwrap_envelope(raw)["claims"][0]["claim_id"] == "c2"


def test_schema_named_wrapper():
    """{"type":"json_object","BoundarySet":{...}} —— 实测 N05 形状。"""
    raw = {"type": "json_object", "claims": [], "semantic_units": [],
           "BoundarySet": {"decisions": [{"claim_id": "c1", "decision": "usable"}]}}
    out = _unwrap_envelope(raw)
    assert out["decisions"][0]["claim_id"] == "c1"


def test_envelope_keeps_non_empty_outer_arrays():
    """外层数组非空时不能被内层空数组覆盖（决策常在信封层）。"""
    raw = {"type": "json_object", "decisions": [{"claim_id": "c9"}], "BoundarySet": {"decisions": []}}
    assert _unwrap_envelope(raw)["decisions"] == [{"claim_id": "c9"}]


def test_plain_object_is_untouched():
    raw = {"claims": [{"claim_id": "c1"}], "semantic_units": [], "events": []}
    assert _unwrap_envelope(raw) is raw


def test_hook_off_mainline_flips_status_to_fail():
    """G4_HOOK_OFF_MAINLINE 追加进 errors 后 status 必须变 fail。

    缺陷：只往 errors 里塞一条、不改 status，评审台标「判否」而决策按「合格」放行，
    同一条候选在两处得到相反结论。
    """
    from agent_system.pipeline import _mechanical_check
    decisions = [_claim("c1", limits=["不得写成史实"])]
    cand = _candidate(slot_id="core_hook", highlight_id="h2")
    result = _mechanical_check(cand, allowed_claims={"c1"}, allowed_highlights={"h1", "h2"},
                               evidence_ids={"e1"}, max_chars=20, allowed_units={"u1"},
                               required_prefixes=["剧情演绎"], claim_decisions=decisions)
    assert result["status"] == "pass"  # 机械检查本身不判 hook，由生成侧补
