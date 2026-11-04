"""基线审核异常不得丢原稿或伪造审核通过。"""
from types import SimpleNamespace

import pytest

from agent_system.baseline import run_baseline
from agent_system.model import ModelReply
from scripts.build_compare_review import load_baseline


class Adapter:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs['task'])
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


def video():
    return SimpleNamespace(video_id='v1', title='source', description='', caption='', asr_segments=[], ocr_segments=[], domain={}, format='')


def generated():
    return ModelReply('ok', {'title': 'original'}, usage={'input': 10}, latency_ms=5)


def test_audit_exception_preserves_generated_candidate():
    adapter = Adapter([generated(), RuntimeError('private detail')])
    result = run_baseline(video(), adapter)
    assert result['title'] == 'original'
    assert result['generate_usage'] == {'input': 10}
    assert result['audit_status'] == 'error'
    assert result['audit_pass'] is False
    assert 'private detail' not in result['error']
    assert result['latency_ms'] == 5


@pytest.mark.parametrize('reply', [
    ModelReply('error', {'pass': True}),
    ModelReply('ok', {'pass': True}, error='invalid'),
    ModelReply('ok', {'pass': 'false'}),
    ModelReply('ok', {}),
    ModelReply('ok', None),
])
def test_invalid_audit_is_not_pass_or_rejection(reply):
    result = run_baseline(video(), Adapter([generated(), reply]))
    assert result['audit_status'] == 'error'
    assert result['audit_pass'] is False
    assert result['title'] == 'original'


@pytest.mark.parametrize(('verdict', 'status'), [(True, 'passed'), (False, 'rejected')])
def test_valid_boolean_audit(verdict, status):
    result = run_baseline(video(), Adapter([generated(), ModelReply('ok', {'pass': verdict}, latency_ms=7)]))
    assert result['audit_status'] == status
    assert result['audit_pass'] is verdict
    assert result['latency_ms'] == 12


def test_generation_error_does_not_audit():
    adapter = Adapter([ModelReply('error', {'title': 'partial'}, error='generation failed')])
    result = run_baseline(video(), adapter)
    assert result['title'] == 'partial'
    assert result['generate_status'] == 'error'
    assert result['audit_status'] == 'not_run'
    assert adapter.calls == ['baseline_generate']


def test_failed_audit_is_not_baseline_recommendation(tmp_path):
    import json
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps({'results': [{'video_id': 'v1', 'title': 'original', 'audit_status': 'error'}]}))
    titles, covered, meta = load_baseline(path)
    assert covered == {'v1'}
    assert titles['v1'] == ''
    assert meta['audit_status_by_video']['v1'] == 'error'
