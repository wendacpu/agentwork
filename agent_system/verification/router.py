"""品类路由 + 权威站点白名单 + 断言筛选（搜索核查 Agent 的第 1、2 层）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WHITELIST_PATH = Path(__file__).with_name("whitelist.json")


def load_whitelist() -> dict[str, list[str]]:
    if WHITELIST_PATH.exists():
        return json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
    return {}


def is_verifiable(domain: dict[str, str] | None, config: Any) -> bool:
    """品类路由：阶段①只查资讯-时事社会（含体育/文娱热点）。"""
    if not getattr(config, "verification_enabled", False):
        return False
    dom = domain or {}
    first, second = str(dom.get("一级", "")), str(dom.get("二级", ""))
    configured = set(getattr(config, "verifiable_domains", []) or [])
    if second in set(getattr(config, "excluded_domains", []) or []):
        return False
    if first == "资讯":
        return True  # 用户确认：资讯全域均纳入（含体育赛事、文娱热点）
    return second in configured or first in configured


def whitelist_for(domain: dict[str, str] | None) -> list[str]:
    """平台路由：按二级领域查权威站点，回退到一级领域，再回退到空。"""
    whitelist = load_whitelist()
    dom = domain or {}
    second = str(dom.get("二级", ""))
    first = str(dom.get("一级", ""))
    return whitelist.get(second) or whitelist.get(first) or whitelist.get("default", [])


def pick_core_claims(claims: list[Any], config: Any) -> list[Any]:
    """断言筛选：只查可核查的事实性断言，取前 max_verification_claims 条。"""
    verifiable_types = set(getattr(config, "verifiable_claim_types", []) or ["observable", "source_claim"])
    candidates = [c for c in claims if getattr(c, "claim_type", "") in verifiable_types]
    limit = int(getattr(config, "max_verification_claims", 3))
    return candidates[:limit]
