"""核查选择：off / static / dynamic 三层路由。

这个文件锁的是三条设计约束，不是普通功能覆盖：

1. 强制条件优先于模型建议——dynamic 模式下模型可以在非强制项上说「不必查」，
   但无权关闭命中强制清单的项（这是安全网）。
2. 建议解析失败必须回退静态路由，绝不能读成「模型说不用查」。
3. dynamic 下模型点名要查、但该品类没有权威白名单时，不退回「搜全网」。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_system.config import RunConfig
from agent_system.pipeline import Pipeline, _verification_hold
from agent_system.types import Claim, Understanding
from agent_system.verification.router import (
    extract_verification_suggestion,
    mandatory_verification,
    normalize_verification_mode,
    pick_core_claims,
    resolve_verification,
)

NEWS = {"一级": "资讯", "二级": "时事社会"}
FOOD = {"一级": "美食", "二级": "饮食文化"}


def _video(domain=None):
    return SimpleNamespace(video_id="BV1test", domain=domain or FOOD)


def _claims(*specs):
    return [Claim(claim_id, text, claim_type, []) for claim_id, text, claim_type in specs]


def _suggestion(needed, reason="", skip_reason=""):
    return {"needed": needed, "reason": reason, "skip_reason": skip_reason, "claim_ids": []}


class ModeTests(unittest.TestCase):
    def test_unknown_mode_falls_back_to_static_not_dynamic(self):
        self.assertEqual(normalize_verification_mode(RunConfig(verification_mode="banana")), "static")
        self.assertEqual(normalize_verification_mode(RunConfig(verification_mode=None)), "static")
        self.assertEqual(normalize_verification_mode(RunConfig(verification_mode="DYNAMIC")), "dynamic")

    def test_off_mode_never_verifies_even_when_enabled(self):
        config = RunConfig(verification_enabled=True, verification_mode="off")
        decision = resolve_verification(_video(NEWS), [], None, config)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["decider"], "off")

    def test_disabled_flag_wins_over_dynamic(self):
        config = RunConfig(verification_enabled=False, verification_mode="dynamic")
        decision = resolve_verification(_video(NEWS), [], _suggestion(True, reason="涉及政策"), config)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["decider"], "disabled")

    def test_static_mode_matches_legacy_domain_routing(self):
        config = RunConfig(verification_enabled=True)  # 默认 static
        self.assertTrue(resolve_verification(_video(NEWS), [], None, config)["needed"])
        self.assertFalse(resolve_verification(_video(FOOD), [], None, config)["needed"])

    def test_dynamic_ignores_suggestion_until_mode_is_dynamic(self):
        """static 下模型建议不生效——否则旧配置的运行行为会被悄悄改掉。"""
        config = RunConfig(verification_enabled=True, verification_mode="static")
        decision = resolve_verification(_video(NEWS), [], _suggestion(False, skip_reason="纯个人记录"), config)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["decider"], "static")


class ModelSkipTests(unittest.TestCase):
    def test_model_can_skip_non_mandatory_domain(self):
        """收益口：模型说不必查，且没命中强制清单时允许跳过。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic")
        decision = resolve_verification(_video(NEWS), [], _suggestion(False, skip_reason="只是日常记录"), config)
        self.assertFalse(decision["needed"])
        self.assertEqual(decision["decider"], "model")
        self.assertTrue(decision["reason"], "跳过必须留下理由，否则事后无法审计")

    def test_model_can_request_verification_outside_static_route(self):
        """反向也要成立：非资讯品类，模型说要查就查。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic")
        decision = resolve_verification(_video(FOOD), [], _suggestion(True, reason="提到具体门店卫生处罚"), config)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["decider"], "model")


class MandatoryTests(unittest.TestCase):
    def test_mandatory_domain_overrides_model_skip(self):
        """安全锁：模型说不用查，但品类在强制清单里——仍然要查。"""
        config = RunConfig(
            verification_enabled=True,
            verification_mode="dynamic",
            verification_mandatory_domains=["时事社会"],
        )
        decision = resolve_verification(_video(NEWS), [], _suggestion(False, skip_reason="只是转载"), config)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["decider"], "mandatory")
        self.assertEqual(decision["reason"], "mandatory_domain:时事社会")

    def test_mandatory_keyword_overrides_model_skip(self):
        config = RunConfig(
            verification_enabled=True,
            verification_mode="dynamic",
            verification_mandatory_keywords=["治愈"],
        )
        claims = _claims(("c1", "用了三天治愈皮炎", "observable"))
        decision = resolve_verification(_video(FOOD), claims, _suggestion(False, skip_reason="个人经验分享"), config)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["decider"], "mandatory")
        self.assertEqual(decision["reason"], "mandatory_keyword:治愈")

    def test_mandatory_domain_applies_in_static_mode_too(self):
        config = RunConfig(
            verification_enabled=True,
            verification_mode="static",
            verification_mandatory_domains=["饮食文化"],
        )
        decision = resolve_verification(_video(FOOD), [], None, config)
        self.assertTrue(decision["needed"])
        self.assertEqual(decision["decider"], "mandatory")

    def test_mandatory_helper_is_inert_without_configured_lists(self):
        """默认清单为空：不引入任何新的强制行为。"""
        self.assertEqual(mandatory_verification(_claims(("c1", "随意文本", "observable")), _video(NEWS), RunConfig()), (False, ""))


class SuggestionParsingTests(unittest.TestCase):
    def test_invalid_suggestions_all_return_none(self):
        """字符串 "true"、缺字段、空对象——全部当「无建议」，不能当「模型说不用查」。"""
        broken = [
            {},
            {"verification_suggestion": None},
            {"verification_suggestion": "yes"},
            {"verification_suggestion": {"verification_needed": "true"}},
            {"verification_suggestion": {"verification_needed": 1}},
            {"verification_suggestion": {"reason": "只给了理由"}},
        ]
        for raw in broken:
            with self.subTest(raw=raw):
                self.assertIsNone(extract_verification_suggestion(raw))

    def test_suggestion_is_read_through_model_envelope(self):
        raw = {"Understanding": {"verification_suggestion": {
            "verification_needed": True,
            "verification_reason": "涉及政策发布",
            "verification_claim_ids": ["c3", " c7 ", ""],
        }}}
        suggestion = extract_verification_suggestion(raw)
        self.assertTrue(suggestion["needed"])
        self.assertEqual(suggestion["reason"], "涉及政策发布")
        self.assertEqual(suggestion["claim_ids"], ["c3", "c7"])

    def test_parse_failure_falls_back_to_static_routing(self):
        """最关键的一条：解析失败 → 回退静态，而不是静默变成「不用查」。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic")
        for raw in ({}, {"verification_suggestion": {"verification_needed": "false"}}):
            with self.subTest(raw=raw):
                suggestion = extract_verification_suggestion(raw)
                self.assertIsNone(suggestion)
                decision = resolve_verification(_video(NEWS), [], suggestion, config)
                self.assertTrue(decision["needed"], "资讯域回退后仍应核查")
                self.assertEqual(decision["decider"], "static_fallback")

    def test_static_fallback_is_distinguishable_from_model_decision(self):
        """回退比例要能统计，所以 decider 不能和 model 混成同一个值。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic")
        fallback = resolve_verification(_video(NEWS), [], None, config)
        model = resolve_verification(_video(NEWS), [], _suggestion(True, reason="有具体事实"), config)
        self.assertEqual(fallback["decider"], "static_fallback")
        self.assertEqual(model["decider"], "model")


class ClaimSelectionTests(unittest.TestCase):
    def test_model_picked_claims_cannot_bypass_type_filter(self):
        """模型不能把观点/预测塞进检索队列——优先归优先，仍须先过可核查过滤。"""
        claims = _claims(
            ("c1", "观察事实", "observable"),
            ("c2", "个人观点", "opinion"),
            ("c3", "来源陈述", "source_claim"),
            ("c4", "另一条观察", "observable"),
        )
        config = RunConfig(max_verification_claims=2)
        picked = pick_core_claims(claims, config, ["c2", "c4"])
        self.assertEqual([c.claim_id for c in picked], ["c4", "c1"])

    def test_claim_selection_without_hint_keeps_legacy_order(self):
        claims = _claims(("c1", "观察事实", "observable"), ("c2", "观点", "opinion"), ("c3", "来源陈述", "source_claim"))
        config = RunConfig(max_verification_claims=2)
        self.assertEqual([c.claim_id for c in pick_core_claims(claims, config)], ["c1", "c3"])


class _RecordingStorage:
    def __init__(self):
        self.events = []
        self.checkpoints = []

    def event(self, task_id, node, event_type, payload):
        self.events.append((node, event_type, payload))

    def checkpoint(self, task_id, status, state, node, name, payload=None):
        self.checkpoints.append((node, name, payload))



class _RecordingProvider:
    def __init__(self):
        self.calls = []

    def search(self, query, sites, limit):
        self.calls.append((query, sites, limit))
        return []


def _bare_pipeline(config, provider=None):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = config
    pipeline.search_provider = provider or _RecordingProvider()
    pipeline.storage = _RecordingStorage()
    pipeline.review_adapter = None
    pipeline.adapter = None
    return pipeline


def _understanding(claims):
    return Understanding(main_format="口播", attributes=[], claims=claims, event_times=[], extracted_sources=[])


class WhitelistGapTests(unittest.TestCase):
    def test_dynamic_does_not_fall_back_to_open_web_search(self):
        """dynamic + 白名单为空：停止核查并留痕，不拿不可控来源去裁决事实。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic", verification_require_whitelist=True)
        pipeline = _bare_pipeline(config)
        claims = _claims(("c1", "某地发生事故", "observable"))
        state = {"verification_suggestion": _suggestion(True, reason="涉及事故")}
        with patch("agent_system.pipeline.whitelist_for", return_value=[]):
            summary = pipeline._verify_understanding("t1", _video(FOOD), _understanding(claims), state)
        self.assertEqual(summary["reason"], "no_whitelist_for_domain")
        self.assertEqual(summary["unverified_claim_ids"], ["c1"])
        self.assertTrue(summary["pending_human"], "必须核查却无从核查时不能静默继续")
        self.assertEqual(pipeline.search_provider.calls, [], "不得退回全网搜索")
        skipped = [e for e in pipeline.storage.events if e[1] == "verification_skipped_no_whitelist"]
        self.assertEqual(len(skipped), 1)

    def test_static_mode_keeps_legacy_open_behaviour(self):
        """static 不启用白名单闸门，保持历史行为不变。"""
        config = RunConfig(verification_enabled=True, verification_mode="static", verification_require_whitelist=False)
        pipeline = _bare_pipeline(config)
        claims = _claims(("c1", "某地发生事故", "observable"))
        state = {}
        with patch("agent_system.pipeline.whitelist_for", return_value=[]):
            summary = pipeline._verify_understanding("t1", _video(NEWS), _understanding(claims), state)
        self.assertNotEqual((summary or {}).get("reason"), "no_whitelist_for_domain")

    def test_declining_verification_emits_no_provider_call(self):
        """needed=False 时不应构造 provider、不应发检索请求。"""
        config = RunConfig(verification_enabled=True, verification_mode="dynamic")
        pipeline = _bare_pipeline(config)
        state = {"verification_suggestion": _suggestion(False, skip_reason="无外部事实主张")}
        result = pipeline._verify_understanding("t1", _video(NEWS), _understanding([]), state)
        self.assertIsNone(result)
        self.assertEqual(pipeline.search_provider.calls, [])
        self.assertEqual(state["verification_decision"]["decider"], "model")
        logged = [e for e in pipeline.storage.events if e[1] == "verification_decision"]
        self.assertEqual(len(logged), 1)


class VerificationHoldTests(unittest.TestCase):
    """转人工的两种理由：事实被证伪，以及「必须核查但无从核查」。

    第二条是强制清单能不能落地的关键——没有它，命中强制项的品类在无白名单时
    只会留一条痕就继续跑，等于强制核查没发生。
    """

    def test_refuted_high_risk_holds(self):
        hold = _verification_hold({"level": "high", "refuted_claim_ids": ["c1"]})
        self.assertEqual(hold["reason"], "HIGH_EXTERNAL_FACT_RISK")
        self.assertEqual(hold["claim_ids"], ["c1"])

    def test_pending_human_holds_without_refutation(self):
        hold = _verification_hold({"level": "low", "pending_human": True, "unverified_claim_ids": ["c3"]})
        self.assertEqual(hold["reason"], "NO_WHITELIST_FOR_REQUIRED_CHECK")
        self.assertEqual(hold["claim_ids"], ["c3"])

    def test_ordinary_low_risk_does_not_hold(self):
        self.assertIsNone(_verification_hold({"level": "low", "refuted_claim_ids": []}))
        self.assertIsNone(_verification_hold(None))
        self.assertIsNone(_verification_hold({}))


if __name__ == "__main__":
    unittest.main()
