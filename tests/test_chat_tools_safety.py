"""对话工具安全回归，不调用外网模型。"""
import pytest

from agent_system import chat_tools
from agent_system.evidence_index import parse_state


def test_resolve_reference_all_branches():
    ctx = chat_tools.ToolContext()
    assert chat_tools.resolve_reference({}, ctx)['data']['needs_disambiguation']
    ctx.focus = {'task_id': 't', 'candidate_ids': ['a', 'b']}
    assert chat_tools.resolve_reference({}, ctx)['data']['needs_disambiguation']
    ctx.focus['candidate_id'] = 'a'
    assert chat_tools.resolve_reference({'phrase': '它'}, ctx)['data']['resolved']['candidate_id'] == 'a'


def test_find_tasks_preserves_batch_identity_and_handles_errors(monkeypatch):
    def fake(path):
        if path == 'bad':
            raise OSError()
        return [{'task_id': 'same', 'video_id': 'v', 'title': 't', 'status': 'ok'}]
    monkeypatch.setattr(chat_tools, 'list_tasks_from_db', fake)
    ctx = chat_tools.ToolContext(db_aliases={'a': 'one', 'b': 'two', 'c': 'bad'})
    result = chat_tools.find_tasks({}, ctx)
    assert result['refs_seen'] == ['task:a:same', 'task:b:same']
    assert result['data'][2]['_batch'] == 'c'


def test_review_rejects_orphan_and_dangling_refs_and_preserves_focus(monkeypatch):
    state = {'video': {'evidence': [{'evidence_id': 'real', 'text': 'x'}]},
             'candidates': [{'candidate_id': 'c'}],
             'reviews': [{'candidate_id': 'c', 'gates': {'G': {'evidence_ids': ['real', 'fake']}},
                          'issues': [{'issue_id': 'i', 'evidence_ids': ['fake']}]},
                         {'candidate_id': 'orphan', 'issues': [{'issue_id': 'bad'}]}]}
    parsed = parse_state(state)
    monkeypatch.setattr(chat_tools, 'load_task', lambda *_: parsed)
    ctx = chat_tools.ToolContext(db_aliases={'a': 'x'}, default_alias='a',
                                 focus={'task_id': 'other', 'batch': 'a', 'candidate_id': 'old'})
    assert not chat_tools.get_review({'task_id': 't', 'candidate_id': 'orphan'}, ctx)['ok']
    result = chat_tools.get_review({'task_id': 't', 'candidate_id': 'c'}, ctx)
    assert result['refs_seen'] == ['ev:real', 'ri:i']
    assert ctx.focus['candidate_id'] == 'old'


def test_coverage_does_not_claim_sufficiency(monkeypatch):
    parsed = parse_state({'video': {'evidence': [{'evidence_id': 'e'}]},
                          'candidates': [{'candidate_id': 'c', 'evidence_ids': ['e']}]})
    monkeypatch.setattr(chat_tools, 'load_task', lambda *_: parsed)
    ctx = chat_tools.ToolContext(db_aliases={'a': 'x'}, default_alias='a')
    result = chat_tools.check_evidence_coverage({'task_id': 't', 'candidate_id': 'c'}, ctx)
    assert result['data']['verdict_hint'] != 'covered'


def test_coverage_reports_missing_claim_and_review_evidence(monkeypatch):
    parsed = parse_state({'video': {'evidence': [{'evidence_id': 'e'}]},
                          'candidates': [{'candidate_id': 'c', 'evidence_ids': ['e'], 'claim_ids': ['lost']}],
                          'reviews': [{'candidate_id': 'c', 'gates': {'G1': {'evidence_ids': ['missing']}}}]})
    monkeypatch.setattr(chat_tools, 'load_task', lambda *_: parsed)
    ctx = chat_tools.ToolContext(db_aliases={'a': 'x'}, default_alias='a')
    data = chat_tools.check_evidence_coverage({'task_id': 't', 'candidate_id': 'c'}, ctx)['data']
    assert data['missing_claims'] == ['lost']
    assert data['dangling_review_refs'] == ['missing']
    assert data['verdict_hint'] == 'record_gap'


def test_limits_and_list_type():
    ctx = chat_tools.ToolContext()
    for value in (0, -1, True, '5', None):
        with pytest.raises(ValueError):
            chat_tools.find_tasks({'limit': value}, ctx)
    assert not chat_tools.get_evidence({'evidence_ids': 'abc'}, ctx)['ok']
    assert not chat_tools.get_evidence({'evidence_ids': ['x'] * 31}, ctx)['ok']


def test_corrupt_snapshot_lists_do_not_iterate_scalars():
    parsed = parse_state({'candidates': [{'candidate_id': 'c', 'evidence_ids': 'abc', 'claim_ids': 1}],
                          'reviews': [{'candidate_id': 'c', 'gates': {'g': {'evidence_ids': 123, 'rule_ids': 'abc'}},
                                       'issues': [{'evidence_ids': 1}]}]})
    assert parsed['candidates'][0]['evidence_ids'] == []
    assert parsed['reviews_by_candidate']['c'][0]['gates']['g']['evidence_ids'] == []
