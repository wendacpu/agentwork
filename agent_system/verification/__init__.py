"""搜索核查 Agent：分品类外部实时核查 + 高危两层处置。"""
from .judge import compute_risk, parse_verification
from .router import (
    VERIFICATION_MODES,
    build_verification_query,
    extract_verification_suggestion,
    is_verifiable,
    load_whitelist,
    mandatory_verification,
    normalize_verification_mode,
    pick_core_claims,
    resolve_verification,
    whitelist_for,
)
from .search import SearchProvider, UnconfiguredSearchProvider, WsaSearchProvider, create_search_provider

__all__ = [
    "SearchProvider",
    "UnconfiguredSearchProvider",
    "WsaSearchProvider",
    "VERIFICATION_MODES",
    "build_verification_query",
    "create_search_provider",
    "compute_risk",
    "extract_verification_suggestion",
    "is_verifiable",
    "load_whitelist",
    "mandatory_verification",
    "normalize_verification_mode",
    "parse_verification",
    "pick_core_claims",
    "resolve_verification",
    "whitelist_for",
]

