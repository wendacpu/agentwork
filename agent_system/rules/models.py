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
