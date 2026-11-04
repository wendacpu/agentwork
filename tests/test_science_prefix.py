from types import SimpleNamespace

from agent_system.labels import required_prefixes, normalize_prefix_labels, render_labels, body_budget, extract_inline_prefixes


def evidence(text, field='ocr', quality='raw_ocr'):
    return SimpleNamespace(text=text, field=field, quality=quality)


def test_science_default_and_non_science():
    assert required_prefixes(domain={'一级': '知识科普'}, format='讲解分享') == ['知识科普']
    assert required_prefixes(domain={'一级': '生活记录'}, format='讲解分享') == []


def test_animation_requires_trusted_material():
    assert required_prefixes(domain={'一级': '知识科普'}, evidence=[evidence('动画演示大陆漂移')]) == ['动画科普']
    assert required_prefixes(domain={'一级': '知识科普'}, evidence=[evidence('动画演示', field='caption', quality='model_derived_uncalibrated')]) == ['知识科普']
    assert required_prefixes(domain={'一级': '知识科普'}, format='动画讲解') == ['动画科普']


def test_category_is_program_selected_and_combines_with_disclosures():
    required = required_prefixes(domain={'一级': '知识科普'}, format='动画剧情', attributes=['AI生成'])
    assert required == ['动画科普', '剧情演绎', 'AI生成']
    assert normalize_prefix_labels(['科普', '动画科普'], ['知识科普']) == ['知识科普']
    assert normalize_prefix_labels([], required) == required
    assert all(len(label) <= 4 for label in required)
    assert body_budget(20, ['动画科普']) == 14
    assert render_labels(['动画科普']) == '【动画科普】'
    assert extract_inline_prefixes('【动画科普】地球漂移') == (['动画科普'], '地球漂移')
