from __future__ import annotations

from .loader import COMMON_PATH, DOMAINS_PATH, FORMS_PATH, load_common, load_common_rule_ids, load_domain_rules, load_form_rules
from .models import Rule, RuleBundle, prompt_text_from_context
from .router import pick_domains, pick_forms
from .rubric import REVIEW_RUBRIC_VERSION, TEMP_RULES, TEMP_RUBRIC, load_rules, rubric

RED_DOT_VERSION = "red-dot-v0.2-modular"


def load_rule_bundle(*, domain: dict[str, str] | None = None, form: str | None = None,
                     attributes: list[str] | None = None,
                     version: str = RED_DOT_VERSION) -> RuleBundle:
    """Load U plus only the domain and form rule blocks that match this video."""
    common = load_common()
    domain_blocks = load_domain_rules()
    form_blocks = load_form_rules()
    selected_domains = pick_domains(domain, attributes)
    selected_forms = pick_forms(form, attributes)

    for rule_id, _ in selected_domains:
        if rule_id not in domain_blocks:
            raise ValueError(f"route references missing domain rule {rule_id} in {DOMAINS_PATH}")
    for rule_id, _ in selected_forms:
        if rule_id not in form_blocks:
            raise ValueError(f"route references missing form rule {rule_id} in {FORMS_PATH}")

    domain_rules = [domain_blocks[rule_id] for rule_id, _ in selected_domains]
    form_rules = [form_blocks[rule_id] for rule_id, _ in selected_forms]
    trace = ([{"rule_id": rule_id, "reason": reason, "scope": "domain"} for rule_id, reason in selected_domains] +
             [{"rule_id": rule_id, "reason": reason, "scope": "form"} for rule_id, reason in selected_forms])
    gaps = []
    if not selected_domains:
        gaps.append("no_domain_rule_matched")
    if not selected_forms:
        gaps.append("no_form_rule_matched")

    sources = [str(COMMON_PATH), str(DOMAINS_PATH), str(FORMS_PATH)]
    return RuleBundle(
        version=version, source=str(COMMON_PATH), sources=sources, common=common,
        domain_rules=domain_rules, form_rules=form_rules, attribute_rules=[],
        rule_ids=["U", *[rule["rule_id"] for rule in domain_rules + form_rules]],
        match_trace=trace, gaps=gaps, conflicts=[],
    )


__all__ = [
    "Rule", "RuleBundle", "RED_DOT_VERSION", "REVIEW_RUBRIC_VERSION", "TEMP_RULES", "TEMP_RUBRIC",
    "prompt_text_from_context",
    "load_rule_bundle", "load_rules", "load_common_rule_ids", "rubric",
]
