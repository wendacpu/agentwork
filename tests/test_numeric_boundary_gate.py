"""数字证据守卫对 usable 与 limited 一视同仁，不恢复已排除事实。"""
from types import SimpleNamespace as NS

import pytest

from agent_system.pipeline import _apply_conflict_gate, _apply_numeric_conflict_gate


def run_gate(decision, claim_text='价格50元', evidence_text='价格5元'):
    evidence = NS(evidence_id='e1', text=evidence_text, quality='source', field='asr')
    claim = NS(claim_id='c1', text=claim_text, evidence_refs=['e1'])
    d = NS(claim_id='c1', decision=decision, verification_required=False, reason='原有理由')
    _apply_numeric_conflict_gate(NS(evidence=[evidence]), NS(claims=[claim]), NS(decisions=[d]))
    return d


@pytest.mark.parametrize('decision', ['usable', 'limited'])
def test_unsupported_number_is_excluded_and_pending(decision):
    d = run_gate(decision)
    assert d.decision == 'excluded'
    assert d.verification_required is True
    assert '原有理由' in d.reason


def test_supported_number_preserves_limited():
    d = run_gate('limited', evidence_text='价格50元')
    assert d.decision == 'limited'
    assert d.verification_required is False


def test_conflict_demotion_does_not_bypass_numeric_gate():
    claim = NS(claim_id='c1', text='价格50元', evidence_refs=['e1'])
    evidence = NS(evidence_id='e1', text='价格5元', quality='source', field='asr')
    understanding = NS(claims=[claim], conflicts=[{'severity': 'medium', 'evidence_ids': ['e1']}])
    decision = NS(claim_id='c1', decision='usable', required_limits=[], verification_required=False, reason='')
    boundary = NS(decisions=[decision])
    _apply_conflict_gate(understanding, boundary)
    assert decision.decision == 'limited'
    _apply_numeric_conflict_gate(NS(evidence=[evidence]), understanding, boundary)
    assert decision.decision == 'excluded'
    assert decision.verification_required


def test_existing_exclusion_is_not_reversed():
    d = run_gate('excluded')
    assert d.decision == 'excluded'
    assert d.reason == '原有理由'
