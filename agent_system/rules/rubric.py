from __future__ import annotations

from typing import Any

from ..labels import PREFIX_RULES
from .models import Rule


REVIEW_RUBRIC_VERSION = "red-dot-review-v2-uncalibrated"

# 平台标注前缀的固定枚举（单一真相源在 agent_system/labels.py）
PREFIX_ENUM = list(PREFIX_RULES)

# D05 表达策略：固定枚举，条件不满足时必须落到「直接描述」
STRATEGY_ENUM = ["问题方法", "对照比较", "前后变化", "疑问留答", "直接描述"]

# D05 条件 → 策略（与 rulepacks/forms.md 与 U-W02 对齐）
STRATEGY_TRIGGERS = {
    "问题方法": "视频给出方法、步骤或可照做的操作",
    "对照比较": "材料包含两个及以上对象的同条件对比",
    "前后变化": "材料展示同一对象的前后两个状态",
    "疑问留答": "视频确实在该片段内给出答案",
    "直接描述": "以上条件都不满足时的兜底",
}

# D08 检查词 → 现有维度（语言归 Q1：读不懂仍有程度差别，不直接判不可用）
DIMENSION_MAP = {
    "事实": "G1", "业务规则": "G2", "匹配度": "G3",
    "完整性": "Q2", "语言": "Q1", "精彩度": "Q3",
}

TEMP_RULES = [
    Rule("B-FACT", "temporary-v1", "hard", "all", "只表达材料与证据支持的信息"),
    Rule("B-SOURCE", "temporary-v1", "conditional", "归属会影响理解或 boundary 明确要求", "按要求保留信息归属"),
    Rule("B-TIME", "temporary-v1", "conditional", "历史或动态信息会被误读为当前状态", "保留必要时间范围"),
    Rule("B-LIMIT", "temporary-v1", "conditional", "limited claim", "保留影响断言含义的限定"),
    Rule("B-LENGTH", "temporary-v1", "hard", "调用方提供展示上限", "标题必须符合配置的长度"),
    Rule("P-FOCUS", "temporary-v1", "preferred", "all", "优先代表性主看点"),
    Rule("P-CLARITY", "temporary-v1", "preferred", "all", "表达清楚、自然"),
]

TEMP_RUBRIC: dict[str, Any] = {
    "version": REVIEW_RUBRIC_VERSION,
    "calibration_status": "uncalibrated",
    "gates": {
        "G1": {"name": "事实与断言许可", "criteria": "证据支持、实体角色、来源传播与权威性、时间状态、断言强度", "owner": "reviewer", "statuses": ["pass", "revise", "needs_review", "reject"]},
        "G2": {"name": "已批准业务政策", "criteria": "仅按已加载且已批准的政策；缺失或冲突时 needs_review", "owner": "reviewer", "statuses": ["pass", "revise", "needs_review", "reject"]},
        "G3": {"name": "内容交付与承诺", "criteria": "标题是否描述所选看点并由视频兑现；不要求列出全部事实", "owner": "reviewer", "statuses": ["pass", "revise", "needs_review", "reject"]},
        "G4": {"name": "机械约束", "criteria": "长度、格式、结构字段、有效引用", "owner": "program", "statuses": ["pass", "fail"]},
    },
    "quality": {
        "Q1": {"name": "表达清晰度", "anchors": {1: "指代、关系或句意难以理解", 2: "能独立理解且表达自然，仍可优化", 3: "句意明确、简洁自然；不要求固定句式"}},
        "Q2": {"name": "信息价值", "anchors": {1: "空泛或缺少可辨识内容", 2: "内容可辨但较宽泛", 3: "具体、相关且有辨识度；不重复评事实支持或看点覆盖"}},
        "Q3": {"name": "观看吸引力", "anchors": {1: "没有清晰观看理由或表达套路化", 2: "有明确理由但表达常规", 3: "真实看点形成清楚、可信的观看动机"}},
    },
    "thresholds": {"quality_min_each": 2, "sort_by_sum": True, "sum_is_gate": False},
    "title_policy": {
        "max_chars": 20,
        "counting": "unicode_characters_including_punctuation_digits_spaces",
        "ending_question_mark_forbidden": True,
        "priority_order": ["evidence_and_fact_boundary", "approved_business_policy", "triggered_conditional_rules", "independent_comprehension_and_delivery", "expression_preferences", "memory_examples"],
        "hard": ["do_not_add_unsupported_facts", "approved_business_policy", "configured_display_constraints"],
        "conditional": ["attribute_source_when_boundary_requires_it", "retain_entity_role_when_omission_changes_meaning", "retain_limits_when_claim_is_limited"],
        "preferred": ["specific_relevant_detail", "information_anchor", "natural_expression", "appeal"],
        "fixed_sentence_pattern_required": False,
        "all_named_entities_required": False,
        "strategy_enum": STRATEGY_ENUM,
        "strategy_triggers": STRATEGY_TRIGGERS,
    },
    "issue_codes": [
        "G1_UNSUPPORTED_FACT", "G1_ENTITY_ROLE_MISATTRIBUTED", "G1_SOURCE_ATTRIBUTION_MISSING", "G1_SOURCE_UNVERIFIED",
        "G1_ASSERTION_OVERSTATED", "G1_CURRENTNESS_ERROR", "G1_MISSING_LIMIT",
        "G1_PENDING_VERIFICATION",
        # 陈述只有模型描述/画面路径支撑，没引用能用的一手文本（ASR/OCR/原题）：
        # 这是"描述编造"的入口，比事后判定事实错更早一层
        "G1_PRIMARY_EVIDENCE_MISSING",
        "G2_POLICY_VIOLATION", "G2_POLICY_MISSING", "G3_CORE_POINT_OMITTED", "G3_OVER_PROMISE", "G3_NOT_DELIVERED",
        "Q2_STRATEGY_MISMATCH",
        "Q1_UNCLEAR", "Q2_TOO_GENERIC", "Q3_LOW_APPEAL",
        "G4_EMPTY_TITLE", "G4_LENGTH_EXCEEDED", "G4_ENDING_QUESTION_MARK", "G4_REQUIRED_FIELD_MISSING", "G4_INVALID_REFERENCE", "G4_SCHEMA_INVALID",
        "G4_META_PHRASE",
        # 平台标注前缀（剧情演绎 / AI生成）：与正文合计计长，由程序判定而非模型自由判
        "G4_PREFIX_MISSING", "G4_PREFIX_UNEXPECTED", "G4_PREFIX_BUDGET_EXHAUSTED", "G4_PREFIX_SELF_DECLARED", "G4_INLINE_PREFIX_IN_BODY",
    ],
    "prefix_policy": {
        "enum": list(PREFIX_ENUM),
        "counting": "prefix_plus_body_total",
        "owner": "program",
        "stage": "render",
        "min_body_budget": 6,
    },
    "rule_ids": ["U", *[f"V{i:02d}" for i in range(1, 11)], *[f"F{i:02d}" for i in range(1, 15)], *[rule.rule_id for rule in TEMP_RULES]],
}


def load_rules(version: str = "temporary-v1") -> list[Rule]:
    return [rule for rule in TEMP_RULES if rule.version == version]


def rubric() -> dict[str, Any]:
    return TEMP_RUBRIC
