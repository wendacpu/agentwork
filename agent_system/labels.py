"""标题前缀（平台标注位）与长度预算。

前缀是平台侧标注，不是表达内容：
- 知识科普分类与剧情演绎、AI生成标识使用受控枚举，**由程序按规则判定**，不让模型逐条自由判
  （踩过的坑：attribution_required 让模型逐条判，结果娱乐/表演类也被要求归属，直接推高字数）。
- 前缀与正文分开存储（`prefix_labels` + `headline`），渲染时拼成 `rendered_title`。
- **总长度口径：前缀 + 正文 ≤ max_chars**（业务方 2026-10-31 定，前缀不豁免）。

模型常把「【剧情演绎】」直接写进 headline，`extract_inline_prefixes` 负责把它剥回前缀字段；
长度只对渲染后的完整串计算，因此前缀吃掉的预算会实时反映在 `body_budget` 上。
"""
from __future__ import annotations

from typing import Any, Iterable

PREFIX_OPEN = "【"
PREFIX_CLOSE = "】"

# 身份路由：标题正文里该用「本名 / 机构名 / 中性身份 / 省略」。
# 判据来自人工反馈（2026-11-04）：知名人物（如小米总裁卢伟冰）可以直接用人名当主语；
# 体量不明的个人账号不该把账号名捧成标题主体，应改用中性身份或行业前缀。
# creator_known=None 表示没有外部检索证据，此时只信白名单与机构账号，其余按不知名处理。
DEFAULT_PUBLIC_FIGURES = {
    "雷军", "刘强东", "卢伟冰", "马云", "马化腾", "董明珠", "余承东", "周鸿祎",
    "李彦宏", "张一鸣", "王兴", "黄峥", "俞敏洪", "罗永浩", "任正非", "曹德旺",
}

# 机构账号特征词：命中即按机构名归属，不降级成「博主」。
ORG_MARKERS = ("新闻", "日报", "晚报", "电视台", "广播", "财经", "体育", "观察", "时报", "通讯社", "官方", "发布", "政府", "委员会", "管理局")

# 领域 → 中性身份（不知名创作者时使用）。顺序即优先级：
# 「财经商业」优先命中「商业观察」，避免降成泛化的「财经博主」。
NEUTRAL_IDENTITY = {
    "商业": "商业观察", "美妆": "美妆博主", "科技": "科技博主", "数码": "数码博主",
    "科普": "科普博主", "知识科普": "科普博主", "财经": "财经博主",
    "美食": "美食博主", "健身": "健身博主", "游戏": "游戏博主", "汽车": "汽车博主", "三农": "三农博主",
}


def _domain_value(domain: Any) -> str:
    if isinstance(domain, dict):
        return "".join(str(v) for v in domain.values() if v)
    return str(domain or "")


def _neutral_label(domain_text: str, delivery_type: str) -> str:
    """按领域子串选中性身份；领域没写就用通称「博主」。"""
    for key, label in NEUTRAL_IDENTITY.items():
        if key in domain_text:
            return label
    return "博主"


def route_identity(*, author: str = "", creator_known: bool | None = None,
                   delivery_type: str = "", domain: Any = None, category: str = "",
                   public_figures: Iterable[str] | None = None) -> dict[str, Any]:
    """输出标题主体身份建议，供生成提示词使用；不直接改写标题。

    mode:
      name    —— 用本名（知名人物、机构高管、白名单命中、外部检索有据）
      org     —— 用机构/媒体名（发布方是机构账号时）
      neutral —— 用中性身份（个人账号且无知名度证据）
      omit    —— 无需身份前置（教程等不依赖人物也能读懂的内容）

    category 是采集元数据里的频道名（如「资讯-财经商业」），领域字段为空时用它选中性身份。
    """
    name = str(author or "").strip()
    figures = set(DEFAULT_PUBLIC_FIGURES) | {str(x) for x in (public_figures or [])}
    dtype = str(delivery_type or "")
    dom = _domain_value(domain) + str(category or "")

    if name and any(marker in name for marker in ORG_MARKERS):
        return {"mode": "org", "label": name, "reason": "发布方是机构/媒体账号，用机构名归属"}
    if creator_known is True:
        return {"mode": "name", "label": name, "reason": "外部检索确认可识别，可直接用人名当主语"}
    if creator_known is False:
        # 检索明确查不到：以检索为准，即使名字在白名单里也不冒用本名。
        return {"mode": "neutral", "label": _neutral_label(dom, dtype), "reason": "外部检索未找到相关内容证据，不把账号名当标题主体"}
    if name and name in figures:
        return {"mode": "name", "label": name, "reason": "发布方在知名人物名单内，可直接用人名当主语"}
    if name:
        # 有账号名但无检索证据：不冒用本名，按领域给中性身份。
        return {"mode": "neutral", "label": _neutral_label(dom, dtype), "reason": "缺少可核实知名度证据，不把账号名当标题主体"}
    if dtype in {"教程", "演示", "测评"}:
        return {"mode": "omit", "label": "", "reason": "教程/演示可由内容本身读懂，不强制身份前置"}
    return {"mode": "omit", "label": "", "reason": "无身份前置需求"}


# 固定枚举。顺序即渲染顺序；新增前缀必须同时改 rulepacks/common.md 的 U-P 段。
PREFIX_RULES: dict[str, dict[str, list[str]]] = {
    "科普": {"formats": [], "attributes": []},
    "知识科普": {"formats": [], "attributes": []},
    "动画科普": {"formats": [], "attributes": []},
    "剧情演绎": {
        "formats": ["剧情"],
        "attributes": ["剧情演绎", "虚构", "演绎", "短剧", "情景剧"],
    },
    "AI生成": {
        "formats": [],
        "attributes": ["AI生成", "AI 生成", "AI合成", "AIGC", "AI创作", "ai生成"],
    },
}

# 正文预算低于这个值就认为"前缀 + 正文"无法共存：报 G4_PREFIX_BUDGET_EXHAUSTED，
# 走业务决策（减标注/放宽上限/放弃该视频），而不是让模型硬挤出一句读不懂的话。
MIN_BODY_BUDGET = 10


def render_prefix(label: str) -> str:
    return f"{PREFIX_OPEN}{label}{PREFIX_CLOSE}"


def render_labels(labels: Iterable[str]) -> str:
    return "".join(render_prefix(str(x)) for x in labels if str(x))


def required_prefixes(*, format: str = "", attributes: list[str] | None = None, domain: Any = None, evidence: Iterable[Any] | None = None) -> list[str]:
    """按「形态 × 属性」查表判定必须渲染哪些前缀；无匹配返回空列表。"""
    fmt = str(format or "")
    attrs = [str(x) for x in (attributes or [])]
    out: list[str] = []
    domain_value = domain.get("一级", "") if isinstance(domain, dict) else str(domain or "")
    if domain_value == "知识科普":
        # 细分形态只采纳原始材料的明确动画声明；模型衍生描述不当作可核实依据。
        trusted = []
        for item in evidence or []:
            field = item.get("field", "") if isinstance(item, dict) else getattr(item, "field", "")
            quality = item.get("quality", "") if isinstance(item, dict) else getattr(item, "quality", "")
            text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
            if field in {"title", "asr", "ocr"} and quality not in {"model_derived_uncalibrated", "unverified_source_metadata"}:
                trusted.append(str(text))
        animation = "动画" in fmt or any("动画" in text for text in trusted)
        out.append("动画科普" if animation else "知识科普")
    for label, rule in PREFIX_RULES.items():
        if any(key and key in fmt for key in rule.get("formats", [])):
            out.append(label)
            continue
        if any(key and any(key in attr for attr in attrs) for key in rule.get("attributes", [])):
            out.append(label)
    return out


def render_title(candidate: dict[str, Any]) -> str:
    return render_labels(candidate.get("prefix_labels") or []) + str(candidate.get("headline") or "")


def body_budget(max_chars: int, labels: Iterable[str]) -> int:
    return int(max_chars) - len(render_labels(labels))


def extract_inline_prefixes(text: str) -> tuple[list[str], str]:
    """把模型写在正文开头的【剧情演绎】/【AI生成】剥出来，返回 (labels, 剩余正文)。

    兼容半角方括号与全角圆括号写法，避免模型换一种包裹就漏判。
    """
    labels: list[str] = []
    rest = str(text or "")
    while True:
        stripped = rest.lstrip()
        matched = False
        for label in PREFIX_RULES:
            for tag in (render_prefix(label), f"[{label}]", f"({label})", f"（{label}）", f"{label}：" , f"{label}:"):
                if stripped.startswith(tag):
                    labels.append(label)
                    rest = stripped[len(tag):]
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break
    seen: list[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen, rest.strip()


def normalize_prefix_labels(labels: Any, required: list[str]) -> list[str]:
    """合并「程序判定」与「模型自报」，只保留枚举内标签，按枚举顺序输出。

    程序判定的必填前缀一定保留（平台义务，模型漏写不代表不用标）；模型自报且枚举内的
    额外前缀也保留，但由调用方记 `self_declared_prefixes`，便于观测模型是否总在自造标注。
    枚举外的标签（如【震惊】）一律丢弃——前缀位不是修辞位。
    """
    raw = labels if isinstance(labels, list) else ([labels] if isinstance(labels, str) else [])
    # 科普分类仅由程序选择，不能让模型自报多个互相冲突的分类标签。
    category_labels = {"科普", "知识科普", "动画科普"}
    kept = {str(x) for x in raw if str(x) in PREFIX_RULES and str(x) not in category_labels}
    return [label for label in PREFIX_RULES if label in kept or label in required]


def self_declared_prefixes(labels: Any, required: list[str]) -> list[str]:
    """模型自报、但程序规则没有要求的前缀（用于观测，不参与判定）。"""
    raw = labels if isinstance(labels, list) else ([labels] if isinstance(labels, str) else [])
    return [str(x) for x in raw if str(x) in PREFIX_RULES and str(x) not in required]
