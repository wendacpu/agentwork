"""身份路由：标题主体该用本名、机构名、中性身份还是省略。

判据来自人工反馈（2026-11-04）：卢伟冰是小米总裁，可以直呼其名；
体量不明的个人账号不该把账号名捧成标题主体，应降级为中性身份。
"""
from agent_system.labels import route_identity


def test_org_account_uses_org_name():
    route = route_identity(author="上游新闻", delivery_type="资讯")
    assert route["mode"] == "org"
    assert route["label"] == "上游新闻"


def test_known_public_figure_uses_real_name():
    route = route_identity(author="卢伟冰", creator_known=True, delivery_type="资讯")
    assert route["mode"] == "name"
    assert route["label"] == "卢伟冰"


def test_whitelisted_figure_works_without_search_evidence():
    route = route_identity(author="雷军", delivery_type="资讯")
    assert route["mode"] == "name"


def test_unknown_creator_falls_back_to_neutral_identity():
    route = route_identity(author="感性城市SCity", domain={"一级": "商业"}, delivery_type="观点")
    assert route["mode"] == "neutral"
    assert route["label"] == "商业观察"
    assert "知名度" in route["reason"]


def test_neutral_identity_uses_domain_then_generic():
    science = route_identity(author="智识科普", domain={"一级": "知识科普"})
    assert science["mode"] == "neutral"
    assert science["label"] == "科普博主"
    generic = route_identity(author="某个账号")
    assert generic["mode"] == "neutral"
    assert generic["label"] == "博主"


def test_tutorial_without_author_omits_identity():
    route = route_identity(author="", delivery_type="教程")
    assert route["mode"] == "omit"
    assert route["label"] == ""


def test_explicit_unknown_overrides_whitelist():
    """外部检索明确查不到时，即使名字像公众人物也不冒用本名。"""
    route = route_identity(author="卢伟冰", creator_known=False, domain={"一级": "科技"})
    assert route["mode"] == "neutral"


def test_public_figures_can_be_extended_by_config():
    route = route_identity(author="某企业家", public_figures=["某企业家"])
    assert route["mode"] == "name"


def test_category_channel_selects_specific_neutral_identity():
    """领域字段为空时用采集频道名，财经商业应降成商业观察而不是泛称博主。"""
    route = route_identity(author="感性城市SCity", category="资讯-财经商业")
    assert route["mode"] == "neutral"
    assert route["label"] == "商业观察"
