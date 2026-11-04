from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Rule:
    rule_id: str
    version: str
    kind: str
    trigger: str
    action: str
    priority: int = 100


@dataclass
class RuleBundle:
    """The traceable rule slice selected for one video run."""

    version: str
    source: str
    common: str
    domain_rules: list[dict[str, str]]
    form_rules: list[dict[str, str]]
    attribute_rules: list[dict[str, str]]
    rule_ids: list[str]
    match_trace: list[dict[str, str]]
    gaps: list[str]
    conflicts: list[str]
    sources: list[str] = field(default_factory=list)

    def as_context(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "sources": self.sources,
            "common": self.common,
            "domain_rules": self.domain_rules,
            "form_rules": self.form_rules,
            "attribute_rules": self.attribute_rules,
            "rule_ids": self.rule_ids,
            "match_trace": self.match_trace,
            "gaps": self.gaps,
            "conflicts": self.conflicts,
        }

    def as_prompt_text(self) -> str:
        """规则全文的提示词形态：进 system 的公共前缀段，不进每次的 user 载荷。"""
        return prompt_text_from_context(self.as_context())


def prompt_text_from_context(context: dict[str, Any] | None) -> str:
    """把规则包还原成提示词文本。

    这里的输入是 `RuleBundle.as_context()` 的结果，不是 RuleBundle 实例：调用方
    手里只有反序列化后的 state，靠它就能逐字重建同一段规则，不需要把 8K 字符
    再抄一份存进 checkpoint/事件表。同一份 context 必须得到同一份文本——它是
    缓存前缀的一部分，任何顺序抖动都会让后面整段失效。
    """
    if not context:
        return ""
    version = str(context.get("version") or "")
    rule_ids = [str(x) for x in (context.get("rule_ids") or [])]
    header = "## 本次适用规则"
    if version or rule_ids:
        header += f"（{version or '未标注版本'}"
        header += f"，命中 {' + '.join(rule_ids)}）" if rule_ids else "）"
    parts = [header]
    common = str(context.get("common") or "").strip()
    if common:
        parts.append("### 通用规则\n" + common)
    for title, key in (("领域规则", "domain_rules"), ("形态规则", "form_rules"), ("属性规则", "attribute_rules")):
        blocks = []
        for rule in context.get(key) or []:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("rule_id") or "")
            name = str(rule.get("name") or "")
            text = str(rule.get("text") or "").strip()
            if not text:
                continue
            label = f"#### {rule_id} {name}".strip()
            blocks.append(f"{label}\n{text}")
        if blocks:
            parts.append(f"### {title}\n" + "\n\n".join(blocks))
    return "\n\n".join(parts).strip()
