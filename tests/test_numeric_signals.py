"""数字信号独立测试：不联网、不调用模型、不默认原题真值。"""
import pytest


def test_rhetorical_crunch_is_not_a_precise_count():
    assert numeric_facts("脆脆泡菜吃播，一口一个脆") == []
    assert numeric_facts("一口一个饺子")


def test_demonstrative_zheme_does_not_turn_duration_into_question():
    text = "我这个锅是不就乾的,我就压个三分钟就够了,如果你们用的是那种锅"
    facts = numeric_facts(text)
    assert facts[0]["role"] == "assertion"
    assert not unsupported_numeric_facts("压三分钟", [text])
    assert not unsupported_numeric_facts("压三分钟", ["我這個鍋就壓個三分鐘就夠了"])
    assert numeric_facts("三分鐘")[0]["unit"] == "分钟"
    assert numeric_facts("怎么就800元了")[0]["role"] == "question"


from agent_system.signals import numeric_facts, unsupported_numeric_facts


@pytest.mark.parametrize("text,value", [
    ("872元", "872"), ("八百七十二元", "872"),
    ("1万元", "10000"), ("10000元", "10000"),
    ("一万元", "10000"), ("1.25万元", "12500"),
    ("二〇二四年", "2024"), ("两百元", "200"),
    ("一千零二元", "1002"), ("1,000元", "1000"),
    ("三点五元", "3.5"), ("一亿两千万元", "120000000"),
])
def test_number_normalization(text, value):
    fact = numeric_facts(text)[0]
    assert fact["value"] == value
    assert text[slice(*fact["span"])] == text


def test_units_and_metrics_are_not_interchangeable():
    facts = numeric_facts("2024年，四人吃饭，总价872元，单价218元，孩子8岁，住了3年")
    assert [(f["value"], f["metric"]) for f in facts] == [
        ("2024", "year"), ("4", "people"), ("872", "total_price"),
        ("218", "unit_price"), ("8", "age"), ("3", "duration"),
    ]
    assert unsupported_numeric_facts("总价872元", ["单价872元"])
    assert unsupported_numeric_facts("8人", ["8岁"])
    assert unsupported_numeric_facts("2024年", ["2024元"])


def test_question_role_and_original_location():
    text = "店里四人用餐。总价八百七十二元吗？每人218元。"
    facts = numeric_facts(text)
    assert [f["role"] for f in facts] == ["assertion", "question", "assertion"]
    assert text[slice(*facts[1]["span"])] == "八百七十二元"
    assert unsupported_numeric_facts("总价872元", ["总价872元吗？"])


def test_equivalent_amounts_are_supported_without_using_headline_as_truth():
    assert unsupported_numeric_facts("花872元", ["花八百七十二元"]) == []
    assert unsupported_numeric_facts("花1万元", ["花10000元"]) == []
    assert unsupported_numeric_facts("总价872元", [])
    assert unsupported_numeric_facts("花872元", ["花827元"])


def test_multiple_entities_or_metrics_do_not_create_conflicts():
    assert unsupported_numeric_facts("四人用餐，总价872元", ["四人用餐，总价872元，年龄28岁"]) == []
    assert unsupported_numeric_facts("甲花872元", ["甲花872元，乙花900元"]) == []
    # 不因别的指标碰巧有同一数值就宣称支持。
    assert unsupported_numeric_facts("总价872元", ["872人，单价872元"])


def test_unknown_metric_matching_is_conservative():
    assert unsupported_numeric_facts("编号872", ["编号872"]) == []
    assert unsupported_numeric_facts("872", ["872元"])
    assert unsupported_numeric_facts("872元", ["872"])
    assert unsupported_numeric_facts("总价872元", ["872元"])


def test_per_unit_suffix_and_nearest_price_label():
    facts = numeric_facts("总价872元单价218元，218元/人，218元一碗")
    assert [f["metric"] for f in facts] == ["total_price", "unit_price", "unit_price", "unit_price", "count"]


def test_plain_text_without_numbers():
    assert numeric_facts("没有数字") == []
    assert numeric_facts("一共花872元")[0]["value"] == "872"
    assert numeric_facts("万一有人不同意") == []
    assert unsupported_numeric_facts("普通标题", []) == []


def test_currency_unit_is_part_of_the_fact():
    """真实人评：标题写「马斯克400亿购芯片快讯有误导」被判「读不顺」。

    根因不只在措辞：`numeric_facts` 不认货币单位，「400亿美元」被抽成 unit=''，
    于是标题丢掉「美元」写成「400亿」也和证据「单位一致」，数字核验拦不住。
    """
    assert numeric_facts("马斯克400亿美元购买英伟达芯片")[0]["unit"] == "美元"
    assert numeric_facts("馬斯克做到宣布400億美元購買因為達新片")[0]["unit"] == "美元"
    assert numeric_facts("馬斯克做到宣布400億美元購買因為達新片")[0]["value"] == "40000000000"
    assert numeric_facts("SpaceX已完成400亿美元")[0]["metric"] == "amount"
    assert unsupported_numeric_facts("博主：马斯克400亿购芯片快讯有误导", ["SpaceX已完成400亿美元"])
    # 补齐单位就应当通过，不能把「带单位」也判成冲突。
    assert not unsupported_numeric_facts("博主：马斯克400亿美元购芯片快讯有误导", ["SpaceX已完成400亿美元"])


def test_traditional_wan_and_yi_are_recognized():
    assert numeric_facts("400億美元")[0]["value"] == "40000000000"
    assert numeric_facts("3萬人")[0]["value"] == "30000"
