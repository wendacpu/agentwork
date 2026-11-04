"""主旨契约：视频级「核心观看承诺」的规范化与对齐校验。

主旨卡回答的是「这条视频到底要交付什么、删掉什么标题就变味」，而不是「视频里出现了什么」。
它把语义重要性（core/support/peripheral）与事实可写性（边界允许/限定/排除）分开：
- 主题可以来自原题 / description / caption 的选题线索，但不能据此证明新增事实；
- 高重要性 + 低可写性时保留主题与缺口、转待核，不能悄悄退成有据但无关的边角。

这里只做结构校验，不做关键词级别的语义判断；「模型自报 core」不是通过的证明，
上游必须给出 thesis_contract 与 highlights 之间的 ID 绑定关系。
"""
from __future__ import annotations

from typing import Any

REQUIRED_THESIS_FIELDS = ("thesis_id", "topic", "delivery_type", "core_promise", "required_semantics")
RELATIONS = ("core", "support", "peripheral")
SLOTS = ("source_rewrite", "core_summary", "core_hook")

# 证据可靠性分层。依据是「这条证据离原始素材有多远」：
#   source                     上传者自己对这条视频的说明（原题/标签）
#   unverified_source_metadata 平台元数据里的来源描述，未经核验
#   raw_asr / raw_ocr / raw_frame  真实采集到的音频转写、画面文字、画面本身
#   model_derived_uncalibrated 模型对上述素材的**推断**，本身已经是结论而非素材
# 分层只用来判断「两份互相矛盾的解读各自站在多硬的地基上」，
# 不用于给事实盖真伪章——原始素材同样可能有转写错误。
EVIDENCE_QUALITY_TIERS = {
    "source": 3,
    "unverified_source_metadata": 2,
    "raw_asr": 2,
    "raw_ocr": 2,
    "raw_frame": 2,
    "model_derived_uncalibrated": 1,
}

# 两侧必保语义/核心承诺的相似度「都低于此值」才判分歧。
#
# 这个数字不是调出来的，是实测逼出来的：真实故障（2026-11-01 canary 剧情）那次
# overlap=0.333，而人读定性为「详略差异、误报」的那次是 overlap=0.333 / promise=0.117
# ——同一个 overlap 值。也就是说**任何阈值都分不开真分歧与同义详略**，只能取「最小
# 能抓住已知真实故障」的值。0.2 会漏掉那条真实故障，0.35 才能抓住。
#
# 代价已量化（51 任务 / 11 视频）：0.2 → 7 任务 4 视频；0.35 → 14 任务 6 视频，
# 其中约半数是人读的详略差异。之所以接受，是因为该判据只用于**摘推荐章**、不阻断
# 交付；要真正分开三类必须读语义（一次判定调用），词法做不到。
CONTRACT_DISAGREEMENT_OVERLAP = 0.35

_STOP_CHARS = set("，。；、！？,;!? \n\t（）()「」“”\"'：:／/·—-")


def _grams(text: Any) -> set[str]:
    clean = "".join(ch for ch in str(text or "") if ch not in _STOP_CHARS)
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def semantics_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    """两侧必保语义的最大两两相似度：任一条对得上就算还在讲同一件事。"""
    left_items = [str(x) for x in (left.get("required_semantics") or []) if str(x).strip()]
    right_items = [str(x) for x in (right.get("required_semantics") or []) if str(x).strip()]
    if not left_items or not right_items:
        return 0.0
    return max(_jaccard(_grams(a), _grams(b)) for a in left_items for b in right_items)


def evidence_hardness(contract: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """一份契约所引用证据的可靠性分布。

    `derived_from` 目前普遍为空，无法真正追溯推断链，所以这里只能看 quality 标签。
    这是当前能力的上限，不是充分判据。
    """
    tiers: dict[str, int] = {}
    for evidence_id in (contract.get("evidence_ids") or []):
        item = evidence_by_id.get(str(evidence_id)) or {}
        quality = str(item.get("quality") or "unknown")
        tiers[quality] = tiers.get(quality, 0) + 1
    return {
        "tiers": tiers,
        "max_tier": max((EVIDENCE_QUALITY_TIERS.get(q, 0) for q in tiers), default=0),
        "soft_only": bool(tiers) and all(EVIDENCE_QUALITY_TIERS.get(q, 0) <= 1 for q in tiers),
    }


def contracts_disagree(
    left: dict[str, Any],
    right: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]] | None = None,
    threshold: float = CONTRACT_DISAGREEMENT_OVERLAP,
) -> dict[str, Any] | None:
    """比对生成侧与独立复核侧的主旨契约；一致返回 None。

    真实故障（2026-11-01，BV1PwsR6oEKW）：两套契约给出两个不同的故事
    （女生送吃的 / 女生睡过头），程序侧没有任何比对，R01 也只用生成侧契约核对标题，
    于是错的那条被推荐给人工。这里补的就是那个缺失的比对。
    """
    if not isinstance(left, dict) or not isinstance(right, dict) or not left or not right:
        return None
    overlap = semantics_overlap(left, right)
    promise_similarity = _jaccard(_grams(left.get("core_promise")), _grams(right.get("core_promise")))
    # 两个指标**都**低才算分歧。只堵一个会误伤：核心承诺一字不差、只是必保语义
    # 换了说法（「从功能到配饰」与「产品从功能走向配饰化」）时语义重合度只有 0.14，
    # 但那是详略差异，不是两个故事。
    if overlap >= threshold or promise_similarity >= threshold:
        return None
    evidence_by_id = evidence_by_id or {}
    ll, rr = evidence_hardness(left, evidence_by_id), evidence_hardness(right, evidence_by_id)
    if ll["max_tier"] > rr["max_tier"] and not ll["soft_only"]:
        better = "generated"
    elif rr["max_tier"] > ll["max_tier"] and not rr["soft_only"]:
        better = "review"
    else:
        # 两侧站在同样硬的地基上（或都只有模型推断）：没有依据说谁对，
        # 此时按任何一侧重写都只是把一次猜测换成另一次猜测。
        better = None
    return {
        "semantics_overlap": round(overlap, 3),
        "promise_similarity": round(promise_similarity, 3),
        "generated_promise": str(left.get("core_promise") or ""),
        "review_promise": str(right.get("core_promise") or ""),
        "generated_evidence": ll,
        "review_evidence": rr,
        "better_grounded": better,
    }



def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unit_ids(understanding: Any) -> set[str]:
    raw = getattr(understanding, "semantic_units", None) if hasattr(understanding, "semantic_units") else None
    if raw is None:
        raw = _dict(understanding).get("semantic_units", [])
    return {str(u.get("unit_id")) if isinstance(u, dict) else str(getattr(u, "unit_id", "")) for u in (raw or [])}


def _unit_decisions(boundary: Any) -> dict[str, str]:
    raw = getattr(boundary, "unit_decisions", None) if hasattr(boundary, "unit_decisions") else None
    if raw is None:
        raw = _dict(boundary).get("unit_decisions", [])
    out: dict[str, str] = {}
    for d in (raw or []):
        if isinstance(d, dict):
            out[str(d.get("unit_id"))] = str(d.get("decision") or "")
        else:
            out[str(getattr(d, "unit_id", ""))] = str(getattr(d, "decision", "") or "")
    return out


def normalize_thesis_contract(raw: Any, understanding: Any, boundary: Any, *, independent: bool = False) -> tuple[dict[str, Any], list[str]]:
    """从 N07 输出（或独立 thesis_review 输出）规范化主旨卡。

    返回 (contract, errors)。errors 非空表示主旨卡不完整或引用悬空，调用方不得静默
    用单条 claim 自动造主旨；是否停机/待审由调用方决定。不接受空 raw 当「无主旨」。
    """
    errors: list[str] = []
    source = _dict(raw)
    candidate = _dict(source.get("thesis_contract") or source.get("ThesisContract"))
    # 兼容模型直接把主旨字段平铺在外层
    if not candidate and any(k in source for k in REQUIRED_THESIS_FIELDS):
        candidate = source

    for field in REQUIRED_THESIS_FIELDS:
        if not str(candidate.get(field) or "").strip():
            errors.append(f"thesis_contract 缺少字段: {field}")

    required_semantics = candidate.get("required_semantics") or []
    if not isinstance(required_semantics, list) or not required_semantics:
        errors.append("required_semantics 必须是非空列表（对象/方法/关系/目的价值等不可删语义）")

    core_unit_ids = [str(x) for x in (candidate.get("core_unit_ids") or [])]
    if not core_unit_ids and not independent:
        errors.append("core_unit_ids 不能为空：主旨必须绑定至少一个语义单元")
    else:
        known = _unit_ids(understanding)
        dangling = [x for x in core_unit_ids if x and x not in known]
        if dangling:
            errors.append(f"core_unit_ids 引用不存在的单元: {', '.join(dangling)}")
        decisions = _unit_decisions(boundary)
        excluded = [x for x in core_unit_ids if decisions.get(x) == "excluded"]
        if excluded:
            errors.append(f"core_unit_ids 引用已排除单元: {', '.join(excluded)}")

    evidence_ids = candidate.get("evidence_ids") or []
    if not isinstance(evidence_ids, list):
        errors.append("evidence_ids 必须是列表")

    contract = {
        "thesis_id": str(candidate.get("thesis_id") or "").strip(),
        "topic": str(candidate.get("topic") or "").strip(),
        "delivery_type": str(candidate.get("delivery_type") or "").strip(),
        "core_promise": str(candidate.get("core_promise") or "").strip(),
        "required_semantics": [str(x) for x in required_semantics],
        "core_unit_ids": core_unit_ids,
        "evidence_ids": [str(x) for x in evidence_ids],
        "qualifiers": [str(x) for x in (candidate.get("qualifiers") or [])],
        "pending_questions": [str(x) for x in (candidate.get("pending_questions") or [])],
    }
    return contract, errors


def validate_highlight_alignment(highlights: list[Any], contract: dict[str, Any]) -> list[str]:
    """校验看点池与主旨卡对齐：主看点必须是 core 且绑定同一 thesis_id；备选同主旨。"""
    errors: list[str] = []
    items = [_dict(h) if isinstance(h, dict) else {"highlight_id": getattr(h, "highlight_id", ""),
             "role": getattr(h, "role", ""), "relation": getattr(h, "relation", ""),
             "thesis_id": getattr(h, "thesis_id", ""), "unit_id": getattr(h, "unit_id", "")} for h in (highlights or [])]
    thesis_id = contract.get("thesis_id") or ""
    mains = [h for h in items if h.get("role") == "main"]
    if not mains:
        errors.append("highlights 缺少主看点（role=main）")
        return errors
    if len(mains) > 1:
        errors.append("highlights 有多个主看点，只允许一个")
    main = mains[0]
    relation = str(main.get("relation") or "")
    if relation != "core":
        errors.append(f"主看点 relation 必须是 core，实际为 {relation or '（空）'}")
    if not thesis_id or main.get("thesis_id") != thesis_id:
        errors.append(f"主看点 thesis_id({main.get('thesis_id')}) 与主旨卡({thesis_id})不一致或缺失")
    if contract.get("core_unit_ids") and main.get("unit_id") not in contract["core_unit_ids"]:
        errors.append("主看点 unit_id 不属于主旨卡 core_unit_ids")
    for h in items:
        if h.get("role") == "backup" and thesis_id and h.get("thesis_id") and h.get("thesis_id") != thesis_id:
            errors.append(f"备选看点 {h.get('highlight_id')} 与主旨卡 thesis_id 不一致")
    return errors
