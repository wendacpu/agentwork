"""独立信号的正反例；不依赖外部数据、网络或模型。"""
from types import SimpleNamespace

from agent_system.signals import extract_risk_signals, check_signal_constraints
from agent_system.types import Evidence


def report(text, quality="source", field="title"):
    return extract_risk_signals(SimpleNamespace(evidence=[Evidence("e1", field, text, quality=quality)]))


def test_explicit_ai_declaration_survives_missing_model_attributes():
    result = report("本视频由AI生成")
    assert result["required_prefixes"] == ["AI生成"]
    signal = result["signals"][0]
    assert signal["evidence_id"] == "e1"
    assert "本视频由AI生成"[slice(*signal["span"])] == signal["value"]


def test_negative_and_discussion_do_not_label():
    for text in ("本视频不是AI生成", "教你识别AI生成视频", "什么是AIGC"):
        result = report(text)
        assert result["required_prefixes"] == []
        assert result["pending_signal_ids"] == []


def test_derived_caption_is_pending_not_fact():
    result = report("本视频为AI生成", "model_derived_uncalibrated", "caption")
    assert result["required_prefixes"] == []
    assert result["pending_signal_ids"]


def test_fiction_does_not_imply_ai():
    assert report("本作品纯属虚构")["required_prefixes"] == ["剧情演绎"]


def test_signal_families_are_locatable():
    result = report("今天部分人可能花872元买四碗面")
    kinds = {s["signal_type"] for s in result["signals"]}
    assert {"relative_time", "qualifier", "number"} <= kinds


def test_publication_time_cannot_support_recency():
    claim = SimpleNamespace(claim_id="c1", text="店铺开业")
    errors = check_signal_constraints("今日店铺开业", {}, [claim], ["c1"])
    assert errors[0]["code"] == "G1_UNSUPPORTED_RECENCY"
    claim.text = "今日店铺开业"
    assert not check_signal_constraints("今日店铺开业", {}, [claim], ["c1"])


def test_missing_qualifier_requires_semantic_review_not_keyword_rejection():
    from agent_system.signals import signal_review_reasons
    signals = report("部分人可能适用")
    assert signal_review_reasons("人人适用", signals, {"e1"})
    assert not signal_review_reasons("一些人或许适用", signals, {"e1"})
    assert not signal_review_reasons("人人适用", signals, {"unrelated"})


def test_relative_time_in_old_material_is_not_current_proof():
    from agent_system.signals import signal_review_reasons
    reasons = signal_review_reasons("今日开业", report("今日开业"), {"e1"})
    assert reasons[0]["code"] == "RELATIVE_TIME_UNVERIFIED"
