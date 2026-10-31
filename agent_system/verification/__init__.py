"""搜索核查 Agent：分品类外部实时核查 + 高危两层处置。"""
from .judge import compute_risk, parse_verification
from .router import build_verification_query, is_verifiable, load_whitelist, pick_core_claims, whitelist_for
from .search import SearchProvider, UnconfiguredSearchProvider, WsaSearchProvider, create_search_provider

__all__ = [
    "SearchProvider",
    "UnconfiguredSearchProvider",
    "WsaSearchProvider",
    "build_verification_query",
    "create_search_provider",
    "compute_risk",
    "is_verifiable",
    "load_whitelist",
    "parse_verification",
    "pick_core_claims",
    "whitelist_for",
]
