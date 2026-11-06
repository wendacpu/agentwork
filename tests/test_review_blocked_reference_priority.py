"""暂缓核验引用：refs 保留供追溯，裁决权交评审 gate。"""
from agent_system.pipeline import _decide_candidate


def test_blocked_claim_refs_preserved_but_review_gate_decides():
    """blocked_claim_refs 留在 gates.G1 供人工追溯；G1 pass 时不再一票否决。"""
    review = {'candidate_id': 'c1', 'gates': {'G1': {'status': 'pass', 'reason': '已核对归属', 'evidence_ids': ['e1']}, 'G2': {'status': 'pass'}, 'G3': {'status': 'pass'}}, 'quality': {}, 'issues': []}
    mechanical = {'status': 'pass', 'errors': [], 'blocked_claim_refs': ['claim-1']}
    policy = {'status': 'approved', 'rules': [{'rule_id': 'P1'}]}
    result = _decide_candidate(review, mechanical, schema_errors=[], business_policy=policy)
    assert result['gates']['G1']['blocked_claim_refs'] == ['claim-1']
    # G1 pass → 不再被程序压成 needs_review（评审确认标题没写成断言）
    assert result['status'] != 'needs_review' or result.get('reason') != 'CORE_FACT_OR_POLICY_UNRESOLVED'


def test_blocked_claim_ref_does_not_override_g1_unclear():
    """G1 自己判 unclear/revise 时仍按 gate 走，不受本次改动影响。"""
    review = {'candidate_id': 'c1', 'gates': {'G1': {'status': 'needs_review', 'reason': '主张是否写成断言无法确定'}, 'G2': {'status': 'pass'}, 'G3': {'status': 'pass'}}, 'quality': {}, 'issues': []}
    mechanical = {'status': 'pass', 'errors': [], 'blocked_claim_refs': ['claim-1']}
    policy = {'status': 'approved', 'rules': [{'rule_id': 'P1'}]}
    result = _decide_candidate(review, mechanical, schema_errors=[], business_policy=policy)
    assert result['status'] == 'needs_review'
