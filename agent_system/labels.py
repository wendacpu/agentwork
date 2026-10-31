"""标题前缀（平台标注位）与长度预算。

前缀是平台侧标注，不是表达内容：
- 只有两类（剧情演绎 / AI 生成），固定枚举，**由程序按规则判定**，不让模型逐条自由判
  （踩过的坑：attribution_required 让模型逐条判，结果娱乐/表演类也被要求归属，直接推高字数）。
- 前缀与正文分开存储（`prefix_labels` + `headline`），渲染时拼成 `rendered_title`。
- **总长度口径：前缀 + 正文 ≤ max_chars**（业务方 2026-10-31 定，前缀不豁免）。

模型常把「【剧情演绎】」直接写进 headline，`extract_inline_prefixes` 负责把它剥回前缀字段；
长度只对渲染后的完整串计算，因此前缀吃掉的预算会实时反映在 `body_budget` 上。
"""
from __future__ import annotations

from typing import Any, Iterable

PREFIX_OPEN = "【"
PREFIX_CLOSE = "】"

# 固定枚举。顺序即渲染顺序；新增前缀必须同时改 rulepacks/common.md 的 U-P 段。
PREFIX_RULES: dict[str, dict[str, list[str]]] = {
    "剧情演绎": {
        "formats": ["剧情"],
        "attributes": ["剧情演绎", "虚构", "演绎", "短剧", "情景剧"],
    },
    "AI生成": {
        "formats": [],
        "attributes": ["AI生成", "AI 生成", "AI合成", "AIGC", "AI创作", "ai生成"],
    },
}

# 正文预算低于这个值就认为"前缀 + 正文"无法共存：报 G4_PREFIX_BUDGET_EXHAUSTED，
# 走业务决策（减标注/放宽上限/放弃该视频），而不是让模型硬挤出一句读不懂的话。
MIN_BODY_BUDGET = 10


def render_prefix(label: str) -> str:
    return f"{PREFIX_OPEN}{label}{PREFIX_CLOSE}"


def render_labels(labels: Iterable[str]) -> str:
    return "".join(render_prefix(str(x)) for x in labels if str(x))


def required_prefixes(*, format: str = "", attributes: list[str] | None = None) -> list[str]:
    """按「形态 × 属性」查表判定必须渲染哪些前缀；无匹配返回空列表。"""
    fmt = str(format or "")
    attrs = [str(x) for x in (attributes or [])]
    out: list[str] = []
    for label, rule in PREFIX_RULES.items():
        if any(key and key in fmt for key in rule.get("formats", [])):
            out.append(label)
            continue
        if any(key and any(key in attr for attr in attrs) for key in rule.get("attributes", [])):
            out.append(label)
    return out


def render_title(candidate: dict[str, Any]) -> str:
    return render_labels(candidate.get("prefix_labels") or []) + str(candidate.get("headline") or "")


def body_budget(max_chars: int, labels: Iterable[str]) -> int:
    return int(max_chars) - len(render_labels(labels))


def extract_inline_prefixes(text: str) -> tuple[list[str], str]:
    """把模型写在正文开头的【剧情演绎】/【AI生成】剥出来，返回 (labels, 剩余正文)。

    兼容半角方括号与全角圆括号写法，避免模型换一种包裹就漏判。
    """
    labels: list[str] = []
    rest = str(text or "")
    while True:
        stripped = rest.lstrip()
        matched = False
        for label in PREFIX_RULES:
            for tag in (render_prefix(label), f"[{label}]", f"({label})", f"（{label}）", f"{label}：" , f"{label}:"):
                if stripped.startswith(tag):
                    labels.append(label)
                    rest = stripped[len(tag):]
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break
    seen: list[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen, rest.strip()


def normalize_prefix_labels(labels: Any, required: list[str]) -> list[str]:
    """合并「程序判定」与「模型自报」，只保留枚举内标签，按枚举顺序输出。

    程序判定的必填前缀一定保留（平台义务，模型漏写不代表不用标）；模型自报且枚举内的
    额外前缀也保留，但由调用方记 `self_declared_prefixes`，便于观测模型是否总在自造标注。
    枚举外的标签（如【震惊】）一律丢弃——前缀位不是修辞位。
    """
    raw = labels if isinstance(labels, list) else ([labels] if isinstance(labels, str) else [])
    kept = {str(x) for x in raw if str(x) in PREFIX_RULES}
    return [label for label in PREFIX_RULES if label in kept or label in required]


def self_declared_prefixes(labels: Any, required: list[str]) -> list[str]:
    """模型自报、但程序规则没有要求的前缀（用于观测，不参与判定）。"""
    raw = labels if isinstance(labels, list) else ([labels] if isinstance(labels, str) else [])
    return [str(x) for x in raw if str(x) in PREFIX_RULES and str(x) not in required]
