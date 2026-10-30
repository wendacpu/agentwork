from __future__ import annotations

import re
from pathlib import Path


RULEPACK_DIR = Path(__file__).resolve().parents[2] / "rulepacks"
COMMON_PATH = RULEPACK_DIR / "common.md"
DOMAINS_PATH = RULEPACK_DIR / "domains.md"
FORMS_PATH = RULEPACK_DIR / "forms.md"


def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"rule source not found: {path}")
    return path.read_text(encoding="utf-8")


def load_common() -> str:
    text = _read(COMMON_PATH)
    _, separator, body = text.partition("\n")
    if not separator or not body.strip():
        raise ValueError(f"common rules are empty: {COMMON_PATH}")
    return body.strip()


def load_common_rule_ids() -> list[str]:
    return sorted(set(re.findall(r"\bU-[A-Z]\d{2}\b", load_common())))


def _parse_blocks(path: Path, prefix: str) -> dict[str, dict[str, str]]:
    text = _read(path)
    pattern = re.compile(rf"^### ({prefix}\d{{2}}) ([^\n]+)\n(.*?)(?=^### {prefix}\d{{2}} |\Z)", re.M | re.S)
    blocks: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(text):
        rule_id, name, body = match.groups()
        if rule_id in blocks:
            raise ValueError(f"duplicate rule id {rule_id} in {path}")
        if not body.strip():
            raise ValueError(f"empty rule block {rule_id} in {path}")
        blocks[rule_id] = {"rule_id": rule_id, "name": name.strip(), "text": body.strip()}
    if not blocks:
        raise ValueError(f"no {prefix} rules found in {path}")
    return blocks


def load_domain_rules() -> dict[str, dict[str, str]]:
    return _parse_blocks(DOMAINS_PATH, "V")


def load_form_rules() -> dict[str, dict[str, str]]:
    return _parse_blocks(FORMS_PATH, "F")
