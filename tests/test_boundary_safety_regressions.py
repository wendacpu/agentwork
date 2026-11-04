"""N05 决策合并不得放宽边界，也不能按位置串换事实。"""
import pytest

from agent_system.pipeline import _normalize_boundary, _missing_boundary_claims


def test_group_exclusion_wins_over_object_and_keeps_limits():
    raw = {'decisions': [{'claim_id': 'c1', 'decision': 'usable', 'required_limits': ['须注明来源']}], 'excluded_claims': ['c1']}
    d = _normalize_boundary(raw, {'c1': '甲'}, {}).decisions[0]
    assert d.decision == 'excluded'
    assert d.required_limits == ['须注明来源']


def test_nested_object_conflict_and_constraints():
    raw = {'claims': [{'claim_id': 'c1', 'decision': 'usable', 'required_limits': ['甲']}],
           'boundary_set': {'claims': [{'claim_id': 'c1', 'decision': 'excluded', 'required_limits': ['乙'], 'verification_required': True}]}}
    d = _normalize_boundary(raw, {'c1': '甲'}, {}).decisions[0]
    assert d.decision == 'excluded'
    assert set(d.required_limits) == {'甲', '乙'}
    assert d.verification_required


@pytest.mark.parametrize('field', ['decision', 'status', 'verdict'])
@pytest.mark.parametrize('value', ['verification_required', 'pending_verification', 'needs_verification'])
def test_explicit_verification_status_is_not_writable(field, value):
    d = _normalize_boundary({'claims': [{'claim_id': 'c1', field: value}]}, {'c1': '甲'}, {}).decisions[0]
    assert d.verification_required
    assert not (d.decision != 'excluded' and not d.verification_required)


def test_completely_missing_claim_is_reported():
    b = _normalize_boundary({'claims': [{'claim_id': 'c1', 'decision': 'usable'}]}, {'c1': '甲', 'c2': '乙'}, {})
    assert _missing_boundary_claims(b) == ['c2']
    assert {d.claim_id for d in b.decisions} == {'c1'}


def test_id_alias_never_inherits_different_claim_evidence():
    b = _normalize_boundary({'claims': [{'id': 'c2', 'decision': 'usable'}]}, {'c1': '甲', 'c2': '乙'}, {'c1': ['e1'], 'c2': ['e2']})
    assert [(d.claim_id, d.evidence_refs) for d in b.decisions] == [('c2', ['e2'])]
    assert _missing_boundary_claims(b) == ['c1']


def test_all_group_and_pending_aliases_are_merged():
    raw = {'usable_claims': ['c1'], 'allowed_claims': ['c2'], 'verification_pending': ['c1'], 'verification_required_claims': ['c2']}
    b = _normalize_boundary(raw, {'c1': '甲', 'c2': '乙'}, {})
    assert {d.claim_id for d in b.decisions} == {'c1', 'c2'}
    assert all(d.verification_required for d in b.decisions)
