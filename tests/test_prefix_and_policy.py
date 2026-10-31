"""基础口径的回归测试：平台前缀、长度总口径、业务政策三分处置、输入质量闸。

这些不是"质量断言"，而是结构断言——它们证明口径一旦被写进代码，就不会被下一次改动
悄悄改回去（前缀吃字数、reject 被当 rewrite 重写、材料不足仍自动放行，都是这类事）。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_system.config import RunConfig
from agent_system.data import Dataset, assess_material
from agent_system.labels import body_budget, extract_inline_prefixes, normalize_prefix_labels, render_labels, render_title, required_prefixes
from agent_system.model import MockAdapter
from agent_system.pipeline import Pipeline, _decide_candidate, _mechanical_check, _policy_action
from agent_system.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")
POLICY_FILE = ROOT / "config.business-policy.json"


def _policy() -> dict:
    return json.loads(POLICY_FILE.read_text(encoding="utf-8"))["extra"]["business_policy"]


def _checks(candidate, max_chars=20, required=None):
    return _mechanical_check(
        candidate,
        allowed_claims={"c1"},
        allowed_highlights={"h1"},
        evidence_ids={"e-title"},
        max_chars=max_chars,
        required_prefixes=required or [],
    )


def _candidate(**over):
    base = {"candidate_id": "a1-1", "headline": "十个闹钟也挡不住的早八", "highlight_id": "h1", "claim_ids": ["c1"], "evidence_ids": ["e-title"], "expression_strategy": "直接描述"}
    base.update(over)
    return base


def _review(g2_status="pass", g2_rules=None, g1="pass", g3="pass", q=(2, 2, 2)):
    return {
        "candidate_id": "a1-1",
        "gates": {
            "G1": {"status": g1, "reason": "r", "evidence_ids": ["e-title"], "rule_ids": []},
            "G2": {"status": g2_status, "reason": "r", "evidence_ids": [], "rule_ids": g2_rules or []},
            "G3": {"status": g3, "reason": "r", "evidence_ids": ["e-title"], "rule_ids": []},
        },
        "quality": {k: {"score": v, "reason": "r"} for k, v in zip(("Q1", "Q2", "Q3"), q)},
        "issues": [],
    }


class PrefixTests(unittest.TestCase):
    def test_required_prefixes_by_format_and_attribute(self):
        self.assertEqual(required_prefixes(format="剧情短片", attributes=[]), ["剧情演绎"])
        self.assertEqual(required_prefixes(format="资讯播报", attributes=["AI生成"]), ["AI生成"])
        self.assertEqual(required_prefixes(format="资讯播报", attributes=[]), [])
        # 两个都命中时同序渲染，顺序固定（枚举顺序），不随模型输出漂移
        self.assertEqual(required_prefixes(format="剧情短片", attributes=["AIGC"]), ["剧情演绎", "AI生成"])

    def test_inline_prefix_is_stripped_from_body(self):
        labels, body = extract_inline_prefixes("【剧情演绎】十个闹钟也挡不住的早八")
        self.assertEqual(labels, ["剧情演绎"])
        self.assertEqual(body, "十个闹钟也挡不住的早八")
        # 半角/圆括号写法同样剥掉，避免模型换个包裹就漏判
        labels, body = extract_inline_prefixes("[AI生成] 我做了个会说话的猫")
        self.assertEqual(labels, ["AI生成"])
        self.assertEqual(body, "我做了个会说话的猫")

    def test_normalize_keeps_required_and_drops_unknown(self):
        self.assertEqual(normalize_prefix_labels(["震惊"], ["剧情演绎"]), ["剧情演绎"])
        # 模型自报的枚举内前缀保留，但要能被上层观测到
        self.assertEqual(normalize_prefix_labels(["AI生成"], ["剧情演绎"]), ["剧情演绎", "AI生成"])

    def test_total_length_counts_prefix(self):
        # 20 字正文 + 6 字前缀 = 26 > 20：长度硬约束按渲染后总长算，前缀不豁免
        cand = _candidate(headline="十个闹钟也挡不住的早八十个闹钟也挡不住", prefix_labels=["剧情演绎"])
        result = _checks(cand, required=["剧情演绎"])
        self.assertEqual(result["status"], "fail")
        self.assertIn("G4_LENGTH_EXCEEDED", [e["code"] for e in result["errors"]])
        self.assertEqual(result["title_length"], len(render_title(cand)))
        self.assertGreater(result["title_length"], 20)

        # 正文压到预算内（14 字）即通过
        cand = _candidate(headline="十个闹钟也挡不住的早八啊啊啊", prefix_labels=["剧情演绎"])
        self.assertEqual(len(render_title(cand)), 20)
        self.assertEqual(_checks(cand, required=["剧情演绎"])["status"], "pass")

    def test_body_budget_shrinks_with_prefix(self):
        self.assertEqual(body_budget(20, []), 20)
        self.assertEqual(body_budget(20, ["剧情演绎"]), 14)
        self.assertEqual(body_budget(20, ["剧情演绎", "AI生成"]), 8)

    def test_missing_required_prefix_is_not_repairable(self):
        # 前缀是平台义务，不是表达选择：缺了不该靠"重写一版"糊过去
        result = _checks(_candidate(prefix_labels=[]), required=["剧情演绎"])
        codes = {e["code"]: e["repairable"] for e in result["errors"]}
        self.assertIn("G4_PREFIX_MISSING", codes)
        self.assertFalse(codes["G4_PREFIX_MISSING"])

    def test_prefix_budget_exhausted_is_a_business_decision(self):
        result = _checks(_candidate(headline="短标题"), max_chars=16, required=["剧情演绎", "AI生成"])
        codes = {e["code"]: e["repairable"] for e in result["errors"]}
        self.assertIn("G4_PREFIX_BUDGET_EXHAUSTED", codes)
        self.assertFalse(codes["G4_PREFIX_BUDGET_EXHAUSTED"])


class PolicyActionTests(unittest.TestCase):
    """减法做细：每类违规配一个处置动作，而不是所有问题都涌向人审。"""

    def setUp(self):
        self.policy = _policy()

    def _decide(self, review, mechanical=None):
        return _decide_candidate(review, mechanical or {"status": "pass", "errors": []}, schema_errors=[], business_policy=self.policy)

    def test_policy_action_takes_strictest(self):
        self.assertEqual(_policy_action(["P2"], self.policy), "rewrite")
        self.assertEqual(_policy_action(["P1"], self.policy), "reject")
        self.assertEqual(_policy_action(["P8"], self.policy), "review")
        # 多条命中取最严：reject 压过 rewrite
        self.assertEqual(_policy_action(["P2", "P1"], self.policy), "reject")
        self.assertIsNone(_policy_action(["NOT_A_RULE"], self.policy))

    def test_reject_level_violation_is_rejected_not_rewritten(self):
        decision = self._decide(_review(g2_status="reject", g2_rules=["P1"]))
        self.assertEqual(decision["status"], "reject")
        self.assertEqual(decision["policy_action"], "reject")

    def test_rewrite_level_violation_enters_rewrite_loop(self):
        decision = self._decide(_review(g2_status="revise", g2_rules=["P2"]))
        self.assertEqual(decision["status"], "rewrite")
        self.assertTrue(decision["repairable"])

    def test_review_level_violation_goes_to_human(self):
        decision = self._decide(_review(g2_status="revise", g2_rules=["P8"]))
        self.assertEqual(decision["status"], "needs_review")
        self.assertFalse(decision["repairable"])

    def test_pass_with_rule_reference_is_not_treated_as_violation(self):
        # G2=pass 时引用的规则是"我检查过这些"，不是"违反了这些"——不能误伤成处置
        decision = self._decide(_review(g2_status="pass", g2_rules=["P1"]))
        self.assertEqual(decision["status"], "ready_for_human_review")
        self.assertIsNone(decision["policy_action"])

    def test_policy_without_action_keeps_old_behaviour(self):
        legacy = {"status": "approved", "version": "old", "rules": [{"rule_id": "P1", "text": "不得低俗"}]}
        decision = _decide_candidate(_review(g2_status="reject", g2_rules=["P1"]), {"status": "pass", "errors": []}, schema_errors=[], business_policy=legacy)
        self.assertEqual(decision["status"], "reject")
        self.assertIsNone(decision["policy_action"])


class InputQualityTests(unittest.TestCase):
    def setUp(self):
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")

    def test_high_quality_when_asr_ladder_present(self):
        material = assess_material(self.dataset.get("BV1Ltsf6xEpV"))
        self.assertEqual(material["level"], "high")
        self.assertGreaterEqual(material["asr_segments"], 5)

    def test_empty_asr_but_readable_ocr_is_medium(self):
        material = assess_material(self.dataset.get("BV11Ssf6gEUV"))
        self.assertEqual(material["level"], "medium")
        self.assertIn("asr_empty", material["reasons"])

    def test_empty_asr_and_garbled_ocr_is_low(self):
        video = self.dataset.get("BV11Ssf6gEUV")
        video.asr_segments = []
        video.ocr_segments = [{"segment_id": "ocr-0", "second": 1, "text": "RT 令日俄罗斯 Bi ZPA ne", "source": "ocr_raw"}]
        material = assess_material(video)
        self.assertEqual(material["level"], "low")
        self.assertIn("ocr_low_chinese_ratio", material["reasons"])
        self.assertTrue(material["advice"])


class EndToEndBasicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = RunConfig.from_json(POLICY_FILE)
        self.config.output_dir = self.tmp.name
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
        self.storage = Storage(Path(self.tmp.name) / "agent.db")
        self.pipeline = Pipeline(self.dataset, self.storage, MockAdapter(), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_drama_video_gets_prefix_and_respects_total_length(self):
        task_id = self.pipeline.run("BV1PwsR6oEKW")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(state["required_prefixes"], ["剧情演绎"])
        self.assertEqual(state["body_budget"], 14)
        self.assertTrue(state["candidates"])
        for candidate in state["candidates"]:
            self.assertEqual(candidate["prefix_labels"], ["剧情演绎"])
            self.assertTrue(candidate["rendered_title"].startswith("【剧情演绎】"))
            self.assertLessEqual(candidate["title_length"], 20)
            # 前缀只存在一次：不能既在前缀字段里、又留在正文里
            self.assertNotIn("【", candidate["headline"])

    def test_plain_video_has_no_prefix(self):
        task_id = self.pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(state["required_prefixes"], [])
        self.assertEqual(state["body_budget"], 20)
        for candidate in state["candidates"]:
            self.assertEqual(candidate["prefix_labels"], [])
            self.assertEqual(candidate["rendered_title"], candidate["headline"])

    def test_source_title_usage_is_observable(self):
        task_id = self.pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        usage = state["source_evidence_usage"]
        self.assertTrue(usage["source_evidence_ids"])
        self.assertIn("covered", usage)
        self.assertTrue(any(event["event_type"] == "source_evidence_usage" for event in self.storage.events(task_id)))

    def test_low_quality_input_is_downgraded_to_human(self):
        # 材料不足时不应自动 handoff：模型只能猜，猜对了也没人复核
        weak = self.dataset.get("BV1Ltsf6xEpV")
        weak.asr_segments = []
        weak.ocr_segments = []
        with mock.patch.object(self.dataset, "get", return_value=weak):
            task_id = self.pipeline.run("BV1Ltsf6xEpV")
        state = json.loads(self.storage.get_task(task_id)["state_json"])
        self.assertEqual(state["input_quality"]["level"], "low")
        self.assertEqual(state["route"]["action"], "needs_review")
        self.assertEqual(state["route"]["reason"], "INPUT_LOW_CONFIDENCE")
        self.assertTrue(all(d["status"] != "ready_for_human_review" for d in state["candidate_decisions"]))

    def test_low_quality_can_stop_when_configured(self):
        self.config.extra = {**self.config.extra, "stop_on_low_quality_input": True}
        weak = self.dataset.get("BV1Ltsf6xEpV")
        weak.asr_segments = []
        weak.ocr_segments = []
        with mock.patch.object(self.dataset, "get", return_value=weak):
            task_id = self.pipeline.run("BV1Ltsf6xEpV")
        row = self.storage.get_task(task_id)
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(json.loads(row["state_json"])["stop_reason"], "INSUFFICIENT_MATERIAL")


if __name__ == "__main__":
    unittest.main()


class RealModelShapeTests(unittest.TestCase):
    """真实模型最常见的结构漂移：认不出就等于 0 条决策 → 静默 NO_USABLE_CLAIM 停机。

    这条不是"宽容度"测试，是成本测试：一次漂移白烧 20 秒 + 2 万 token，且任务看起来"正常停止"。
    """

    def test_boundary_set_claim_decisions_alias(self):
        from agent_system.pipeline import _normalize_boundary
        raw = {"type": "json_object", "boundary_set": {"video_id": "x", "claim_decisions": [
            {"claim_id": "c1", "decision": "limited", "claim_type": "observable", "required_limits": ["须归属"], "attribution_required": True},
        ], "unit_decisions": [{"unit_id": "su-01", "decision": "usable"}]}}
        boundary = _normalize_boundary(raw)
        self.assertEqual(len(boundary.decisions), 1)
        self.assertEqual(boundary.decisions[0].claim_id, "c1")
        self.assertEqual(boundary.decisions[0].decision, "limited")
        self.assertEqual(boundary.decisions[0].required_limits, ["须归属"])
        self.assertEqual(len(boundary.unit_decisions), 1)

    def test_decisions_list_still_works(self):
        from agent_system.pipeline import _normalize_boundary
        boundary = _normalize_boundary({"BoundarySet": {"decisions": [{"claim_id": "c1", "decision": "usable"}], "unit_decisions": []}})
        self.assertEqual(len(boundary.decisions), 1)

    def test_schema_invalid_review_does_not_mask_repairable_mechanical(self):
        """评审输出坏了，不该让"超长/引用错"这类可修问题绕过重写直接进人审。

        人审是最贵的一环；一条模型超长标题因为评审 JSON 不合法就落到人审队列，
        等于把自动化的失败变成了人工的负担。
        """
        review = _review(g2_status="pass", g2_rules=[])
        mech = {"status": "fail", "errors": [{"code": "G4_LENGTH_EXCEEDED", "repairable": True}]}
        decision = _decide_candidate(review, mech, schema_errors=["gates missing"], business_policy=_policy())
        self.assertEqual(decision["status"], "rewrite")
        self.assertEqual(decision["reason"], "G4_REPAIRABLE_WITH_INVALID_REVIEW")

    def test_schema_invalid_without_repairable_error_stays_human(self):
        review = _review(g2_status="pass", g2_rules=[])
        mech = {"status": "fail", "errors": [{"code": "G4_PREFIX_MISSING", "repairable": False}]}
        decision = _decide_candidate(review, mech, schema_errors=["gates missing"], business_policy=_policy())
        self.assertEqual(decision["status"], "needs_review")
        self.assertEqual(decision["reason"], "REVIEW_SCHEMA_INVALID")


def _video_with(evidence_specs):
    from agent_system.types import Evidence, VideoInput
    evidence = [Evidence(eid, kind, text, quality=quality) for eid, kind, text, quality in evidence_specs]
    return VideoInput(
        video_id="BV-test", title="原标题", author="a", author_uid=1, url="u",
        domain={}, format="剧情短片", published_at=None, duration=10.0, dimensions={},
        description="", caption="", asr_segments=[], ocr_segments=[], evidence=evidence,
        asset_paths={}, provenance={},
    )


def _understanding_with(claim_specs, units=()):
    from agent_system.types import Claim, SemanticUnit, Understanding
    claims = [Claim(cid, text, "observable", refs) for cid, text, refs in claim_specs]
    semantic_units = [SemanticUnit(uid, "意思", list(cids), []) for uid, cids in units]
    return Understanding("剧情短片", [], claims, [], [], [], semantic_units)


class PrimaryEvidenceGuardTests(unittest.TestCase):
    """C22：材料里有 ASR/OCR/原题，陈述却只引用模型描述或帧路径 —— 就是编造入口。

    BV1PwsR6oEKW 的真实失败：description 写「女生抱花束递男生」并引用帧，ASR 原文是
    「你吃饭了吗／就知道你还没吃」。不堵这条，修多少表达规则都没用。
    """

    def test_claim_citing_only_description_and_frame_is_flagged(self):
        from agent_system.pipeline import _primary_evidence_audit
        video = _video_with([
            ("e-title", "title", "原题", "source"),
            ("e-description", "description", "女生抱花束递给男生", "model_derived_uncalibrated"),
            ("e-frame-000", "frame", "/tmp/f0.jpg", "raw_frame"),
            ("e-asr-0000", "asr", "你吃饭了吗", "raw_asr"),
        ])
        understanding = _understanding_with([("c1", "女生抱花束递给男生", ["e-description", "e-frame-000"])])
        audit = _primary_evidence_audit(understanding, video)
        self.assertTrue(audit["applicable"])
        self.assertIn("c1", audit["claims"])
        self.assertEqual(audit["flagged"], 1)

    def test_claim_citing_asr_is_not_flagged(self):
        from agent_system.pipeline import _primary_evidence_audit
        video = _video_with([
            ("e-description", "description", "x", "model_derived_uncalibrated"),
            ("e-asr-0000", "asr", "就知道你还没吃", "raw_asr"),
        ])
        understanding = _understanding_with([("c1", "男生问女生吃没吃饭", ["e-asr-0000"])])
        audit = _primary_evidence_audit(understanding, video)
        self.assertEqual(audit["flagged"], 0)

    def test_pure_visual_material_is_not_penalized(self):
        """纯画面视频（ASR/OCR/原题都没有）不该因为「没引用文本」被判编造。"""
        from agent_system.pipeline import _primary_evidence_audit
        video = _video_with([("e-frame-000", "frame", "/tmp/f0.jpg", "raw_frame")])
        understanding = _understanding_with([("c1", "画面里有人在跑", ["e-frame-000"])])
        audit = _primary_evidence_audit(understanding, video)
        self.assertFalse(audit["applicable"])

    def test_guard_downgrades_claim_and_whole_unit(self):
        from agent_system.pipeline import _apply_primary_evidence_guard, _primary_evidence_audit
        from agent_system.types import BoundarySet, ClaimDecision, UnitDecision
        video = _video_with([
            ("e-description", "description", "x", "model_derived_uncalibrated"),
            ("e-asr-0000", "asr", "你吃饭了吗", "raw_asr"),
        ])
        understanding = _understanding_with(
            [("c1", "女生抱花束递给男生", ["e-description"]), ("c2", "男生问吃没吃饭", ["e-asr-0000"])],
            units=[("u1", ["c1"]), ("u2", ["c2"])],
        )
        boundary = BoundarySet(
            decisions=[ClaimDecision("c1", "女生抱花束递给男生", "usable", ["e-description"], [], [], [], ""),
                       ClaimDecision("c2", "男生问吃没吃饭", "usable", ["e-asr-0000"], [], [], [], "")],
            unit_decisions=[UnitDecision("u1", "usable"), UnitDecision("u2", "usable")],
        )
        audit = _primary_evidence_audit(understanding, video)
        guarded, count = _apply_primary_evidence_guard(boundary, audit, understanding)
        self.assertEqual(count, 1)
        by_id = {d.claim_id: d for d in guarded.decisions}
        self.assertEqual(by_id["c1"].decision, "excluded")
        self.assertEqual(by_id["c1"].evidence_status, "unsupported")
        self.assertIn("C22", by_id["c1"].reason)
        self.assertEqual(by_id["c2"].decision, "usable")
        units = {u.unit_id: u for u in guarded.unit_decisions}
        self.assertEqual(units["u1"].decision, "excluded")
        self.assertEqual(units["u2"].decision, "usable")

    def test_unit_downgraded_when_most_claims_lack_primary(self):
        """过半即降级：一句真话不能救活整个编造场景。

        BV1PwsR6oEKW 的 su-01（女生送花束）4 条 claim 里 3 条只引用描述，
        第 4 条是真实的「我走了」。全废才降级的旧规则让这条编造叙事活了下来。
        """
        from agent_system.pipeline import _apply_primary_evidence_guard, _primary_evidence_audit
        from agent_system.types import BoundarySet, ClaimDecision, UnitDecision
        video = _video_with([
            ("e-description", "description", "x", "model_derived_uncalibrated"),
            ("e-asr-0027", "asr", "我走了", "raw_asr"),
        ])
        understanding = _understanding_with(
            [("c1", "女生递花束", ["e-description"]), ("c2", "她说好好休息", ["e-description"]),
             ("c3", "她说我走了", ["e-asr-0027"]), ("c4", "结尾面对面站着", ["e-description"])],
            units=[("su-01", ["c1", "c2", "c3", "c4"]), ("su-02", ["c3"])],
        )
        boundary = BoundarySet(
            decisions=[ClaimDecision(cid, "x", "usable", ["e-description"], [], [], [], "") for cid in ("c1", "c2", "c3", "c4")],
            unit_decisions=[UnitDecision("su-01", "usable"), UnitDecision("su-02", "usable")],
        )
        guarded, _ = _apply_primary_evidence_guard(boundary, _primary_evidence_audit(understanding, video), understanding)
        units = {u.unit_id: u for u in guarded.unit_decisions}
        self.assertEqual(units["su-01"].decision, "excluded", "3/4 缺一手文本，整个单元应出局")
        self.assertEqual(units["su-02"].decision, "usable", "1/1 有据的单元不受牵连")


class NarrativeRiskTests(unittest.TestCase):
    """生成可信度标记：只做展示，不改判定标准。判据必须是可测的形态，不是"视频类型"。"""

    def _video(self, asr_texts):
        from agent_system.types import Evidence, VideoInput
        segs = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(asr_texts)]
        evidence = [Evidence("e-asr-%04d" % i, "asr", t, quality="raw_asr") for i, t in enumerate(asr_texts)]
        return VideoInput(video_id="v", title="t", author="a", author_uid=1, url="u", domain={}, format="剧情短片",
                          published_at=None, duration=1.0, dimensions={}, description="", caption="",
                          asr_segments=segs, ocr_segments=[], evidence=evidence, asset_paths={}, provenance={})

    def test_dialogue_drama_is_flagged(self):
        from agent_system.pipeline import _narrative_risk
        texts = ["你吃饭了吗", "就知道你还没吃", "你吃", "你赶紧回去吧", "你放心", "我走了",
                 "你给我吃", "你等衣服出去", "你还没吃", "你要不要", "我给你", "你别走"]
        risk = _narrative_risk(self._video(texts), {"applicable": True, "claim_total": 10, "flagged": 4})
        codes = {x["code"] for x in risk["signals"]}
        self.assertIn("DIALOGUE_SPEAKER_UNATTRIBUTED", codes)
        self.assertIn("NARRATIVE_IN_DESCRIPTION", codes)
        self.assertEqual(risk["level"], "high")

    def test_sports_commentary_is_not_flagged(self):
        """解说词也是密集短句，但没有「你/我」互指：不能因为短句多就标低可信。"""
        from agent_system.pipeline import _narrative_risk
        texts = ["金子般的进球", "费兰托雷斯", "补时破门", "西班牙加时1比0领先", "阿根廷压上",
                 "角球机会", "门前混战", "费兰抢点", "球进了", "太关键了", "比赛还有时间", "阿根廷要还击"]
        risk = _narrative_risk(self._video(texts), {"applicable": True, "claim_total": 10, "flagged": 0})
        self.assertEqual(risk["level"], "none")

    def test_plain_material_is_not_flagged(self):
        from agent_system.pipeline import _narrative_risk
        risk = _narrative_risk(self._video(["康平县一中学筛查肺结核时多名学生共用一个针头"]), {"applicable": True, "claim_total": 3, "flagged": 0})
        self.assertEqual(risk["level"], "none")


class StructuralCandidateTests(unittest.TestCase):
    """结构断言：事件之间的关系可以当主看点，评价性判断不行。

    事实断言要 grounded（某条 claim 直接支撑），结构断言天然是推断——
    "她迟到反被关心"不可能有原话。用事实标准卡它，最有价值的看点会被全部筛掉。
    """

    def _build(self, units, turns):
        from agent_system.types import BoundarySet, Claim, ClaimDecision, EventNode, EventTurn, SemanticUnit, UnitDecision, Understanding
        claims = [Claim("c1", "她说要定十个闹钟", "observable", ["e-title"]),
                  Claim("c2", "一方问对方吃饭没", "observable", ["e-asr-0014"])]
        events = [EventNode("ev-1", "女生", "睡过头迟到", "前情", ["e-title"]),
                  EventNode("ev-2", "一方", "问对方吃饭没", "现场", ["e-asr-0014"])]
        understanding = Understanding("剧情短片", [], claims, [], [], [], units, events,
                                      [EventTurn(t["f"], t["t"], t["type"], t.get("note", "")) for t in turns])
        boundary = BoundarySet(
            decisions=[ClaimDecision("c1", "x", "usable", ["e-title"], [], [], [], ""),
                       ClaimDecision("c2", "x", "usable", ["e-asr-0014"], [], [], [], "")],
            unit_decisions=[UnitDecision(u.unit_id, "limited") for u in units],
        )
        return understanding, boundary

    def test_contrast_turn_becomes_candidate(self):
        from agent_system.pipeline import _structural_candidates
        from agent_system.types import SemanticUnit
        u, b = self._build([SemanticUnit("su-1", "意思", ["c1", "c2"], ["e-title", "e-asr-0014"], role_in_video="主旨")],
                           [{"f": "ev-1", "t": "ev-2", "type": "contrast", "note": "她迟到反被关心"}])
        out = _structural_candidates(u, b)
        self.assertTrue(any(x["kind"] == "turn" and x["relation"] == "contrast" for x in out))

    def test_main_axis_needs_two_events(self):
        from agent_system.pipeline import _structural_candidates
        from agent_system.types import SemanticUnit
        # 覆盖两个事件的主旨 → 可作主轴
        u, b = self._build([SemanticUnit("su-1", "她迟到但被关心", ["c1", "c2"], ["e-title", "e-asr-0014"], role_in_video="主旨")], [])
        self.assertTrue(any(x["kind"] == "main_axis" for x in _structural_candidates(u, b)))
        # 只覆盖一个事件的主旨（通常是一句评价）→ 不入选
        u2, b2 = self._build([SemanticUnit("su-2", "这份通报在替学校开脱", ["c1"], ["e-title"], role_in_video="主旨")], [])
        self.assertFalse(any(x["kind"] == "main_axis" for x in _structural_candidates(u2, b2)))

    def test_evaluation_judgment_never_enters(self):
        """评价性判断没有事件端点，天然不该出现；这条测试锁住这个行为。"""
        from agent_system.pipeline import _structural_candidates
        from agent_system.types import SemanticUnit
        u, b = self._build([SemanticUnit("su-3", "作者有意误导观众", ["c1"], ["e-title"], role_in_video="主旨")],
                           [{"f": "ev-1", "t": "ev-1", "type": "contrast"}])
        out = _structural_candidates(u, b)
        self.assertFalse(any("误导" in x["value"] for x in out))

    def test_meta_event_cannot_be_turn_endpoint(self):
        """「标题承诺 X，实际是 Y」不是事件，不能当反差端点。

        BV12HxE6cEQ5 的换装视频里，模型把"标题承诺深度交流科学知识"当成事件，
        于是"标题党"被写成了剧情反差。事件端点必须来自真实发生的事。
        """
        from agent_system.pipeline import _structural_candidates
        from agent_system.types import BoundarySet, Claim, ClaimDecision, EventNode, EventTurn, Understanding
        claims = [Claim("c1", "标题承诺深度交流", "observable", ["e-title"]),
                  Claim("c2", "画面是换装摆拍", "observable", ["e-description"])]
        events = [EventNode("ev-1", "作者", "标题承诺深度交流科学知识", "前情", ["e-title"]),
                  EventNode("ev-2", "女生", "换装摆拍展示外形", "现场", ["e-description"])]
        understanding = Understanding("表演展示", [], claims, [], [], [], [], events,
                                      [EventTurn("ev-1", "ev-2", "contrast", "标题与内容不一致")])
        boundary = BoundarySet(decisions=[ClaimDecision("c1", "x", "usable", ["e-title"], [], [], [], ""),
                                          ClaimDecision("c2", "x", "usable", ["e-description"], [], [], [], "")])
        out = _structural_candidates(understanding, boundary)
        self.assertFalse([x for x in out if x["kind"] == "turn"], "标题承诺不能当反差端点")

    def test_same_event_pair_cannot_form_turn(self):
        from agent_system.pipeline import _structural_candidates
        from agent_system.types import BoundarySet, Claim, ClaimDecision, EventNode, EventTurn, Understanding
        claims = [Claim("c1", "起跳投篮", "observable", ["e-asr-0001"])]
        events = [EventNode("ev-1", "一方", "起跳投篮", "现场", ["e-asr-0001"])]
        understanding = Understanding("现场记录", [], claims, [], [], [], [], events,
                                      [EventTurn("ev-1", "ev-1", "contrast", "攻防对抗")])
        boundary = BoundarySet(decisions=[ClaimDecision("c1", "x", "usable", ["e-asr-0001"], [], [], [], "")])
        self.assertFalse([x for x in _structural_candidates(understanding, boundary) if x["kind"] == "turn"])
