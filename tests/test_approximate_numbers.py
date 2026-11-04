from agent_system.signals import numeric_facts, unsupported_numeric_facts


def test_approximate_duration_never_becomes_zero_or_is_implicitly_supported():
    facts = numeric_facts('动画串起数亿年大陆聚散史')
    assert len(facts) == 1
    assert facts[0]['value'] is None
    assert unsupported_numeric_facts('动画串起数亿年大陆聚散史', ['3亿年前大陆合体'])


def test_approximate_evidence_does_not_prove_exact_claim():
    assert unsupported_numeric_facts('历经三亿年', ['历经数亿年'])
    assert not unsupported_numeric_facts('历经三亿年', ['历经300000000年'])


def test_approximate_counts_are_not_ignored():
    assert unsupported_numeric_facts('需要几个字', [])
    assert unsupported_numeric_facts('约3年', ['3年'])
