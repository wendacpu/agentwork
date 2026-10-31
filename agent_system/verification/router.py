"""品类路由 + 权威站点白名单 + 断言筛选（搜索核查 Agent 的第 1、2 层）。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

WHITELIST_PATH = Path(__file__).with_name("whitelist.json")

# 画面/剪辑描述词：出现在检索词里会把"视频里拍到了什么"当成"世界上发生了什么"去搜。
VISUAL_NOISE = (
    "压屏", "绿字", "白字", "红字", "黑底", "字幕条", "字幕", "台标", "角标", "水印",
    "画外音", "配音", "镜头", "画面", "特写", "俯拍", "仰拍", "全景", "近景", "中景",
    "推进", "拉远", "跟拍", "跟随", "剪辑", "切换", "插入", "叠影", "分屏", "转场",
    "角落", "底部", "顶部", "左上", "右上", "左下", "右下", "中段", "随后", "紧接着",
    "滚动", "闪现", "浮现", "打出", "屏幕上", "开场", "结尾", "节奏",
)

_SENTENCE_SPLIT = re.compile(r"[；;。！!？?\n]+")
_QUOTED = re.compile(r"[“\"《]([^”\"》]{6,80})[”\"》]")


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


def _noise_ratio(text: str) -> float:
    hits = sum(text.count(word) for word in VISUAL_NOISE)
    return hits / max(len(text), 1)


def _strip_noise_prefix(text: str) -> str:
    """反复剥掉开头的画面描述词，保留其后的真实断言。"""
    for _ in range(4):
        for word in VISUAL_NOISE:
            if text.startswith(word):
                text = text[len(word):].lstrip("，、：:；; ")
                break
        else:
            return text
    return text


def build_verification_query(claim: Any, max_chars: int = 40) -> str:
    """把断言转成一条能搜的检索词。

    模型显式给了 verification_query 就直接用；否则从文本里挑最像"事实断言"的一句——
    引号内的原话优先，画面/剪辑描述度高的句子降权。检索词进的是公开搜索引擎，
    描述"视频怎么拍"只会搜到无关内容。
    """
    explicit = str(getattr(claim, "verification_query", "") or "").strip()
    if explicit:
        return explicit[:max_chars]
    text = str(getattr(claim, "text", "") or "").strip()
    if not text:
        return ""
    candidates = [*_QUOTED.findall(text), *_SENTENCE_SPLIT.split(text)]
    scored: list[tuple[float, str]] = []
    for candidate in candidates:
        cleaned = _strip_noise_prefix(str(candidate).strip())
        if cleaned.startswith(("“", "\"", "《")):
            cleaned = cleaned.strip("“”\"《》").strip()
        if len(cleaned) < 6:
            continue
        score = 0.0
        if 8 <= len(cleaned) <= max_chars:
            score += 3.0
        elif len(cleaned) <= max_chars * 2:
            score += 1.0
        score -= 8.0 * _noise_ratio(cleaned)
        score += 1.5 * len(re.findall(r"\d", cleaned))
        scored.append((score, cleaned))
    if not scored:
        return text[:max_chars]
    best = max(scored, key=lambda item: item[0])[1]
    return best[:max_chars]
