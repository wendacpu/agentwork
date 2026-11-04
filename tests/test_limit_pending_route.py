"""词法无法证明限定成立时，不能将机械 pass 当作可交付。"""
from types import SimpleNamespace

import pytest

from agent_system.pipeline import _mechanical_check, _decide_candidate


@pytest.mark.parametrize('limit,title', [
    ('限定为原神+微信视频测试条件', '新手机玩所有游戏续航翻倍'),
    ('须注明未经证实', '部分商场发生恐怖袭击'),
    ('须写明时间范围为2024年', '2025年全行业暴涨'),
    ('可写司机昏迷斜靠座椅，但病因须归属', '司机因酒驾昏迷，辅警破窗救援'),
    ('须注明未经证实且写明时间范围', '2025年商场发生恐怖袭击'),
    ('不得推断中毒', '司机因中毒昏迷'),
    ('病因须归属', '警方称已介入，男子因中毒昏迷'),
    ('须归属旁白，司机未死亡', '司机当场死亡'),
    ('不得写成“已证实”', '事件已证实'),
    ('不得写“死亡”', '男子身亡'),
    ('ASR“攻艦步”须标注为转写推断', '运动员做攻艦步'),
])
def test_ambiguous_limits_go_to_review_even_with_invalid_review(limit, title):
    """评审不可用（schema_errors）时，挂着待答限定的候选不能被机械 pass 放行。

    新契约：词法不再判「限定语义」；限定登记为 limit_requirements 由评审逐条判定。
    评审都坏了 ⇒ 没人判过 ⇒ 留给人（LIMIT_CHECK_UNANSWERED）。
    例外：被禁词引号括出且字面命中（如「已证实」），词法测得出违反，走可修重写。
    """
    decision = SimpleNamespace(claim_id='c1', decision='limited', required_limits=[limit])
    candidate = {'candidate_id': 'x', 'headline': title, 'claim_ids': ['c1'], 'evidence_ids': ['e1'], 'highlight_id': 'h1'}
    mechanical = _mechanical_check(candidate, allowed_claims={'c1'}, allowed_highlights={'h1'}, evidence_ids={'e1'}, max_chars=20, claim_decisions=[decision])
    assert mechanical['limit_pending'] or mechanical['limit_requirements'] or any(
        e['code'] == 'G1_PROHIBITION_HIT' for e in mechanical['errors'])
    result = _decide_candidate({'candidate_id': 'x'}, mechanical, schema_errors=['invalid'], business_policy={})
    if any(e['code'] == 'G1_PROHIBITION_HIT' for e in mechanical['errors']):
        assert result['status'] == 'rewrite'
        assert result['reason'] == 'G4_REPAIRABLE_WITH_INVALID_REVIEW'
    else:
        assert result['status'] == 'needs_review'
        assert result['reason'] == 'LIMIT_CHECK_UNANSWERED'


def test_ordinal_assertion_still_requires_evidence():
    from agent_system.signals import unsupported_numeric_facts

    assert unsupported_numeric_facts('全球第1例', ['无相关序号证据'])


def test_invalid_review_does_not_weaken_program_policy_reject():
    mechanical = {'status': 'pass', 'errors': [], 'limit_pending': [{'claim_id': 'c1'}], 'policy_hits': [{'rule_id': 'P9'}]}
    result = _decide_candidate({'candidate_id': 'x'}, mechanical, schema_errors=['invalid'], business_policy={'status': 'approved', 'rules': [{'rule_id': 'P9', 'action': 'reject'}]})
    assert result['status'] == 'reject'


def test_pending_does_not_weaken_policy_reject():
    mechanical = {'status': 'pass', 'errors': [], 'limit_pending': [{'claim_id': 'c1'}]}
    review = {'candidate_id': 'x', 'gates': {'G1': {'status': 'pass'}, 'G2': {'status': 'reject', 'rule_ids': ['P1']}, 'G3': {'status': 'pass'}}, 'quality': {}}
    result = _decide_candidate(review, mechanical, schema_errors=[], business_policy={'status': 'approved', 'rules': [{'rule_id': 'P1', 'action': 'reject'}]})
    assert result['status'] == 'reject'
