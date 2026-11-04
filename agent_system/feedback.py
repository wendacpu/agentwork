"""人工判定 → 分层回流。

每个判定都归到一个设计层，后续由该层的检查/规则承接，而不是"一个 case 补一条规则"。
- extraction：事实/证据抽取出错（数字、主体、证据）
- constraint：关键背景/锚点/限制条件缺失
- selection：看点选错（边角料）
- expression：语法/用词/表达
- unknown：需要人工定位
"""
from __future__ import annotations

LAYER_BY_REASON: dict[str, tuple[str, str]] = {
    "fact": ("extraction", "G1 事实/证据：数字/金额/主体是否有对应原始证据支持"),
    "context": ("constraint", "约束层：关键背景/地域/主体锚点是否保留"),
    "point": ("selection", "选择层：主看点是否为视频核心（主线测试）"),
    "grammar": ("expression", "表达层：标题是否语义自足且自然"),
    "readability": ("expression", "表达层：用词/缩写是否可读"),
}


def classify(raw_reason_type: str, problem_type: str, reason: str = "") -> tuple[str, str]:
    key = str(raw_reason_type or "").strip()
    if key in LAYER_BY_REASON:
        return LAYER_BY_REASON[key]
    if problem_type == "事实与信源":
        return ("extraction", "G1 事实/证据：数字/金额/主体是否有对应原始证据支持")
    if problem_type == "表达":
        return ("expression", "表达层：语法/用词")
    return ("unknown", "需要人工定位到具体层")
