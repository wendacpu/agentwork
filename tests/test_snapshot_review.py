"""重评快照离线展示：不调用模型、不写快照，意见写独立数据库。"""
import json
import threading
from http.client import HTTPConnection

import pytest

from agent_system.batch_review import title_hash
from agent_system.snapshot_review import SnapshotReviewStore, make_snapshot_server


@pytest.fixture
def snapshot(tmp_path):
    long_title = '超长完整候选' * 20
    tasks = [
        {'task_id': 'ok', 'video_id': 'v1', 'status': 'completed', 'original_status': 'pending',
         'conclusion_origin': 'new', 'video': {'title': '原视频', 'url': 'https://example.org/video', 'asset_paths': {}},
         'route': {}, 'delivery_slots': {'missing': ['core_hook']},
         'candidates': [{'candidate': {'candidate_id': 'c1', 'rendered_title': '新标题'},
                         'decision': {'status': 'rewrite'}, 'recommended': False},
                        {'candidate': {'candidate_id': 'c2', 'rendered_title': long_title,
                                       'mechanical_checks': {'errors': [{'code': 'G4_LENGTH_EXCEEDED'}]}},
                         'decision': {'status': 'reject'}, 'recommended': False}]},
        {'task_id': 'bad', 'video_id': 'v2', 'status': 'failed', 'original_status': 'old_status',
         'conclusion_origin': 'old', 'error_type': 'Error', 'error': '<错误详情>',
         'video': {'title': '旧原视频', 'asset_paths': {}}, 'route': {}, 'delivery_slots': {},
         'candidates': [{'candidate': {'candidate_id': 'old', 'rendered_title': '旧标题'},
                         'decision': {'status': 'needs_review'}, 'recommended': False}]},
    ]
    for task in tasks:
        for entry in task['candidates']:
            candidate = entry['candidate']
            entry['title_hash'] = title_hash(candidate.get('rendered_title') or candidate.get('headline') or '')
            entry['headline_hash'] = title_hash(candidate.get('headline') or '')
    data = {'task_count': 2, 'candidate_count': 3, 'tasks': tasks, 'manifest_sha256': 'x'}
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path, long_title


def test_completed_and_failed_states_and_isolation(snapshot):
    path, long_title = snapshot
    before = path.read_bytes()
    store = SnapshotReviewStore(path)
    assert '成功' in store.list_tasks()[0]['source_status']
    assert '无新结果' in store.list_tasks()[1]['source_status']
    detail = store.get_task('ok')
    assert detail['state']['candidates'][0]['status'] == 'rewrite'
    assert detail['state']['video']['title'] == '原视频'
    assert detail['length_excluded_candidates'][0]['rendered_title'] == long_title
    assert store.get_task('bad')['snapshot_error'] == '<错误详情>'
    body = {'task_id': 'ok', 'candidate_id': 'c1', 'title_hash': title_hash('新标题'), 'decision': 'approved'}
    store.record_decision(body)
    assert store.feedback()['decisions'][0]['original_headline'] == '新标题'
    with pytest.raises(ValueError, match='旧状态仅供查看'):
        store.record_decision({**body, 'task_id': 'bad', 'candidate_id': 'old'})
    with pytest.raises(ValueError, match='超长候选'):
        store.record_decision({**body, 'candidate_id': 'c2', 'title_hash': title_hash(long_title)})
    assert path.read_bytes() == before
    assert store.feedback_db.name == 'snapshot_feedback.db'
    with pytest.raises(ValueError):
        SnapshotReviewStore(path, path)


def test_snapshot_pending_is_independent_of_route_and_errors(snapshot):
    path, _ = snapshot
    data = json.loads(path.read_text())
    task = data['tasks'][0]
    task['route'] = {'action': 'needs_review', 'recommended_candidate_id': None}
    entry = task['candidates'][0]
    entry['decision'] = {'status': 'ready_for_human_review'}
    entry['candidate']['mechanical_checks'] = {
        'errors': [{'code': 'G4_META_PHRASE'}],
        'limit_pending': [{'claim_id': 'c1', 'limit': '限定', 'reason': '词法未识别'}],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    store = SnapshotReviewStore(path)
    c = store.get_task('ok')['state']['candidates'][0]
    assert c['status'] == 'ready_for_human_review'
    assert c['deliverable'] is False
    assert [r['code'] for r in c['blocking_reasons']] == ['G4_META_PHRASE']
    assert c['pending_limits'][0]['reason'] == '词法未识别'
    store.record_decision({'task_id': 'ok', 'candidate_id': 'c1', 'decision': 'approved'})
    assert store.feedback()['reviewed'] == 1


def test_rejects_incomplete_and_cross_snapshot_feedback(snapshot, tmp_path):
    path, _ = snapshot
    store = SnapshotReviewStore(path)
    data = json.loads(path.read_text())
    data['tasks'][0]['status'] = 'running'
    other = tmp_path / 'other.json'
    other.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='未结束'):
        SnapshotReviewStore(other, store.feedback_db)
    data['tasks'][0]['status'] = 'completed'
    other.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='另一份快照'):
        SnapshotReviewStore(other, store.feedback_db)


def test_http_page_and_disabled_generation(snapshot):
    path, _ = snapshot
    server = make_snapshot_server(SnapshotReviewStore(path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        conn.request('GET', '/')
        response = conn.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert '本次失败：无新结果' in page
        assert '${esc(task.snapshot_error' in page
        conn.request('GET', '/api/task?task_id=bad')
        response = conn.getresponse()
        assert json.loads(response.read())['task']['snapshot_status'] == 'failed'
        conn.request('POST', '/api/run', '{}', {'X-Batch-Review': '1'})
        response = conn.getresponse()
        response.read()
        assert response.status == 404
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
