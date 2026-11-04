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


# ---------------------------------------------------------------------------
# 核查选择：off / static / dynamic 三层
#
# static 是历史行为（按品类路由）；dynamic 让理解节点给出「这条视频要不要外部核查」
# 的建议，但建议必须经过程序这一层——命中强制清单的项模型无权关闭。
# 设计意图：安全网（mandatory）与收益口（model 可以说不查）分开，
# 模型只能在非强制项上做减法。
# ---------------------------------------------------------------------------

VERIFICATION_MODES = ("off", "static", "dynamic")

_SUGGESTION_KEYS = ("verification_suggestion", "verificationSuggestion")
_ENVELOPE_KEYS = ("Understanding", "understanding", "result", "output", "data")


def normalize_verification_mode(config: Any) -> str:
    """未知取值一律退回 static（历史行为），不静默升级为 dynamic。"""
    raw = str(getattr(config, "verification_mode", "static") or "static").strip().lower()
    return raw if raw in VERIFICATION_MODES else "static"


def mandatory_verification(claims: list[Any], video: Any, config: Any) -> tuple[bool, str]:
    """程序强制条件：命中即必须核查，dynamic 模式下模型无权关闭。

    两份清单默认都是空的，需业务侧显式配置——强制清单属于业务政策，
    不该由代码默认拍板。推荐清单见 docs/verification-dynamic-routing.md。
    """
    dom = getattr(video, "domain", None) or {}
    mandatory_domains = {
        str(x).strip() for x in (getattr(config, "verification_mandatory_domains", []) or []) if str(x).strip()
    }
    if mandatory_domains:
        for level in ("一级", "二级"):
            name = str(dom.get(level, "")).strip()
            if name and name in mandatory_domains:
                return True, f"mandatory_domain:{name}"
    keywords = [
        str(x).strip() for x in (getattr(config, "verification_mandatory_keywords", []) or []) if str(x).strip()
    ]
    if keywords:
        for claim in claims or []:
            text = str(getattr(claim, "text", "") or "")
            if not text:
                continue
            for word in keywords:
                if word in text:
                    return True, f"mandatory_keyword:{word}"
    return False, ""


def _find_suggestion(raw: Any, depth: int = 0) -> dict[str, Any] | None:
    """模型有时把建议套在 Understanding/result 这类信封里，逐层找。"""
    if depth > 3 or not isinstance(raw, dict):
        return None
    for key in _SUGGESTION_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    for wrapper in _ENVELOPE_KEYS:
        inner = raw.get(wrapper)
        if isinstance(inner, dict):
            found = _find_suggestion(inner, depth + 1)
            if found is not None:
                return found
    return None


def extract_verification_suggestion(raw: Any) -> dict[str, Any] | None:
    """从理解节点的原始输出里读核查建议。

    只有两种返回：合法建议，或 None（=「没有建议」）。
    字段缺失、类型不对、verification_needed 不是布尔值，一律 None——
    调用方据此回退静态路由，绝不能把解析失败当成「模型说不用查」。
    """
    found = _find_suggestion(raw)
    if not isinstance(found, dict):
        return None
    needed = found.get("verification_needed")
    if not isinstance(needed, bool):
        return None
    claim_ids = found.get("verification_claim_ids")
    if not isinstance(claim_ids, list):
        claim_ids = []
    return {
        "needed": needed,
        "reason": str(found.get("verification_reason") or found.get("reason") or "").strip(),
        "skip_reason": str(found.get("skip_reason") or "").strip(),
        "claim_ids": [str(x).strip() for x in claim_ids if str(x).strip()],
    }


def resolve_verification(
    video: Any, claims: list[Any], suggestion: dict[str, Any] | None, config: Any
) -> dict[str, Any]:
    """决定这次任务要不要做外部核查，并留下判定来源（可审计）。

    decider 取值：off / disabled / mandatory / model / static / static_fallback
    dynamic 下没有可用建议时回退 static，并标 static_fallback，便于统计回退比例。
    """
    mode = normalize_verification_mode(config)
    if mode == "off":
        return {"needed": False, "decider": "off", "reason": "verification_mode=off"}
    if not getattr(config, "verification_enabled", False):
        return {"needed": False, "decider": "disabled", "reason": "verification_enabled=false"}

    forced, why = mandatory_verification(claims, video, config)
    if forced:
        return {"needed": True, "decider": "mandatory", "reason": why}

    if mode == "dynamic" and suggestion is not None:
        needed = bool(suggestion.get("needed"))
        reason = suggestion.get("reason") if needed else (suggestion.get("skip_reason") or suggestion.get("reason"))
        return {
            "needed": needed,
            "decider": "model",
            "reason": str(reason or ("model_requested" if needed else "model_skipped")),
        }

    needed = is_verifiable(getattr(video, "domain", None), config)
    return {
        "needed": needed,
        "decider": "static_fallback" if mode == "dynamic" else "static",
        "reason": "domain_route" if needed else "domain_not_covered",
    }



def pick_core_claims(claims: list[Any], config: Any, preferred_ids: list[str] | None = None) -> list[Any]:
    """断言筛选：只查可核查的事实性断言，取前 max_verification_claims 条。

    模型显式点了 claim_ids 时优先——但仍须先通过 claim_type 可核查过滤，
    否则模型可以把「观点/预测」这类不可核查内容塞进检索队列。
    """
    verifiable_types = set(getattr(config, "verifiable_claim_types", []) or ["observable", "source_claim"])
    candidates = [c for c in claims if getattr(c, "claim_type", "") in verifiable_types]
    limit = int(getattr(config, "max_verification_claims", 3))
    preferred = [str(x).strip() for x in (preferred_ids or []) if str(x).strip()]
    if preferred:
        wanted = set(preferred)
        by_id = {str(getattr(c, "claim_id", "")): c for c in candidates}
        picked = [by_id[cid] for cid in preferred if cid in by_id]
        rest = [c for c in candidates if str(getattr(c, "claim_id", "")) not in wanted]
        return (picked + rest)[:limit]
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
