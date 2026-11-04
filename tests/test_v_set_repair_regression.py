"""V 集人工评审暴露的四项缺陷修复的回归锁（2026-11-04 batch 2）。

数据来源：docs/baseline-20261104-机械层修复前.json（49 项人工判定）。
每一组测试对应一个人工端可复现的误伤或漏放，缺一环就是退化。

1. 数字守卫：模糊量词（数小时）同词互认；「数亿年」不被「3亿年」隐式顶替；
   存在量词（一名幼童）不构成数量主张；跨语言证据跳过词法核验。
2. 制作者语义：「博主把X剪成合集」是制作者动作（人工 rejected），
   「博主：X合集」是归属前缀（人工 approved）。
3. G3 收紧：干净候选 + G3 单独 revise + Q≥7 → 人审不重写；
   低 Q 或带机械缺陷 → 仍走重写挽救。
"""
from __future__ import annotations

from agent_system.pipeline import _CREATOR_ACTION_RE, _foreign_evidence_ratio
from agent_system.signals import unsupported_numeric_facts


# --- 1. 数字守卫（V 集实测：幼童 c1、下颌 c7 被误排除 → 候选白跑重写） ---

def test_fuzzy_quantifier_supported_by_same_word():
    """「数小时」对证据「数小时」→ 支持。旧实现 value=None 永不匹配，
    supported 的 claim 被整条 excluded（人体科学视频实况）。"""
    assert unsupported_numeric_facts("每天持续数小时托头", ["每天持续数小时托头"]) == []


def test_fuzzy_quantifier_not_replaced_by_exact_number():
    """模糊量级不能被明确数值隐式顶替（既有契约，见 test_approximate_numbers）。"""
    assert unsupported_numeric_facts("动画串起数亿年大陆聚散史", ["3亿年前大陆合体"])


def test_fuzzy_quantifier_without_evidence_stays_unsupported():
    assert unsupported_numeric_facts("需要几个字", [])


def test_existential_quantifier_is_not_a_count_claim():
    """「一名幼童」是存在描述不是数量主张——幼童任务 c1 因它被误排除。"""
    assert unsupported_numeric_facts("视频中一名幼童坐在餐椅上吃午餐", ["OCR:鸡腿 排骨汤 西兰花 饭团"]) == []


def test_count_claims_still_require_evidence():
    assert unsupported_numeric_facts("四人用餐", [])
    assert unsupported_numeric_facts("一万元", [])


def test_question_evidence_does_not_support_assertion():
    assert unsupported_numeric_facts("总价872元", ["总价872元吗？"])


def test_foreign_evidence_skips_numeric_gate():
    """英文 ASR/OCR 的证据：中文数字词永远词法匹配不上，跨语言跳过。"""
    refs = ["Do it for hours every day, and that uneven pressure can strain the joint"]
    assert _foreign_evidence_ratio(refs) > 0.5
    assert _foreign_evidence_ratio(["幼童吃午餐，餐盘有鸡腿"]) < 0.5


# --- 2. 制作者语义（人工 rejected 2 条同理由） ---

def test_creator_action_phrases_are_flagged():
    assert _CREATOR_ACTION_RE.search("博主把钓鱼圈稀奇古怪片段剪成合集")
    assert _CREATOR_ACTION_RE.search("博主合集：钓鱼圈那些稀奇古怪场面")


def test_attribution_prefix_collection_is_not_flagged():
    """「博主：X合集」人工 approved——归属前缀 + 内容宾语，不是制作者动作。"""
    assert not _CREATOR_ACTION_RE.search("博主：钓鱼圈稀奇古怪片段合集")
    assert not _CREATOR_ACTION_RE.search("博主演示手法放松小腿肌肉和跟腱")


# --- 3. G3 收紧（V 集人工 approved 的白跑重写） ---

def _decide(g3_extra: dict | None = None, g3_status: str = "revise", quality=(3, 2, 2)):
    from agent_system.pipeline import _decide_candidate
    g3 = {"status": g3_status, "reason": "漏掉核心承诺", "evidence_ids": [], "rule_ids": []}
    if g3_extra:
        g3.update(g3_extra)
    gates = {"G1": {"status": "pass", "reason": "r", "evidence_ids": [], "rule_ids": []},
             "G2": {"status": "pass", "reason": "r", "evidence_ids": [], "rule_ids": []},
             "G3": g3,
             "G4": {"status": "pass", "reason": "r", "errors": []}}
    review = {"candidate_id": "c1", "gates": gates, "issues": [],
              "quality": {f"Q{i+1}": {"score": s, "reason": "r"} for i, s in enumerate(quality)}}
    policy = {"status": "approved", "rules": [{"rule_id": "P1", "text": "测试政策"}]}
    return _decide_candidate(review, {"status": "pass", "errors": [], "policy_hits": []},
                             schema_errors=[], business_policy=policy)


def test_soft_g3_revise_goes_to_human_not_rewrite():
    """覆盖不全但承诺兑现（core_covered=False + delivery_supported=True + matched）：
    V 集 3/3 人工 approved——软伤交人工，不烧重写。"""
    decision = _decide(g3_extra={"core_covered": False, "delivery_supported": True})
    assert decision["status"] == "needs_review"
    assert decision["reason"] == "G3_REVISION_REQUIRES_HUMAN_CALL"


def test_hard_g3_failure_still_rewrites():
    """视频没兑现标题承诺（delivery_supported=False）：必须重写真错位。"""
    decision = _decide(g3_extra={"core_covered": False, "delivery_supported": False})
    assert decision["status"] == "rewrite"


def test_mismatched_promise_still_rewrites():
    decision = _decide(g3_extra={"core_covered": False, "delivery_supported": True,
                                 "promise_comparison": {"alignment": "mismatched"}})
    assert decision["status"] == "rewrite"


def test_low_quality_g3_revise_still_rewrites():
    decision = _decide(quality=(2, 1, 1))
    assert decision["status"] == "rewrite"
