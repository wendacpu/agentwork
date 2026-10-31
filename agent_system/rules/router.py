from __future__ import annotations


DOMAIN_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("V01", ("宠物", "动物")), ("V02", ("美妆", "彩妆")), ("V03", ("美甲",)),
    ("V04", ("社会", "城市", "时事")), ("V05", ("肉类", "料理", "烹饪")),
    ("V06", ("探店", "餐饮")), ("V07", ("水果", "品鉴")), ("V08", ("健身", "运动", "体态")),
    ("V09", ("兽装",)), ("V10", ("音乐", "演出", "现场")),
    # 一级领域规则（对齐 data/source/content_taxonomy.md，2026-11 补）
    ("V11", ("资讯",)), ("V12", ("时尚", "美妆")), ("V13", ("美食", "烘焙", "小吃")),
    ("V14", ("生活", "vlog", "手工", "居家")), ("V15", ("健康", "运动", "健身")),
    ("V16", ("知识", "科普")),
)

FORM_ROUTES: tuple[tuple[str, str], ...] = (
    ("F01", "教程演示"), ("F02", "讲解分享"), ("F03", "测评试用"), ("F04", "开箱展示"),
    ("F05", "探店体验"), ("F06", "成果展示"), ("F07", "现场记录"), ("F08", "表演展示"),
    ("F09", "剧情短片"), ("F10", "访谈对谈"), ("F11", "资讯播报"), ("F12", "观点评论"),
    ("F13", "直播切片"), ("F14", "混剪合集"),
)


def pick_domains(domain: dict[str, str] | None, attributes: list[str] | None) -> list[tuple[str, str]]:
    values = list((domain or {}).values()) + (attributes or [])
    text = " ".join(str(value) for value in values).lower()
    return [(rule_id, "；".join(keywords)) for rule_id, keywords in DOMAIN_ROUTES if any(word in text for word in keywords)]


def pick_forms(form: str | None, attributes: list[str] | None) -> list[tuple[str, str]]:
    text = " ".join([str(form or ""), *(str(value) for value in attributes or [])])
    return [(rule_id, label) for rule_id, label in FORM_ROUTES if label in text]
