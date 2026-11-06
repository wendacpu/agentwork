import copy
import hashlib
import json
import sqlite3

import pytest

from scripts.build_authorized_retry_review import build, sha


def db(path, task, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks(task_id TEXT, video_id TEXT, status TEXT, state_json TEXT)')
        conn.execute('INSERT INTO tasks VALUES (?,?,?,?)', (task, 'bv-' + task, 'completed', json.dumps(state)))


def fixture(tmp_path):
    source = tmp_path / 'source' / 'agent.db'
    candidates = [{'candidate_id': 'c1', 'headline': '原稿', 'rendered_title': '展示稿'},
                  {'candidate_id': 'c2', 'headline': '另一条'}]
    frozen = [{'candidate_id': c['candidate_id'], 'headline_sha256': sha(c['headline']),
               'title_sha256': sha(c.get('rendered_title') or c['headline'])} for c in candidates]
    video = {'video_id': 'bv-t1', 'title': '原视频', 'asset_paths': {'video': '/original.mp4'}}
    state = {'video': video, 'candidates': candidates, 'route': {'recommended_candidate_id': 'c1'},
             'candidate_decisions': [{'candidate_id': 'c1', 'status': 'rewrite'}]}
    db(source, 't1', state)
    with sqlite3.connect(source) as conn:
        old2 = copy.deepcopy(state)
        old2['video']['video_id'] = 'bv-t2'
        conn.execute('INSERT INTO tasks VALUES (?,?,?,?)', ('t2', 'bv-t2', 'failed', json.dumps(old2)))
    batch = tmp_path / 'run'
    result = batch / 'rereviews' / 'r1' / 'agent.db'
    new = copy.deepcopy(state)
    new['candidate_decisions'][0]['status'] = 'needs_review'
    new['rereview_isolation'] = {'task_id': 't1', 'source_database_path': str(source),
        'video_snapshot_sha256': sha(json.dumps(video, sort_keys=True, ensure_ascii=False)), 'frozen_candidates': frozen}
    db(result, 't1', new)
    manifest = {'source_database_path': str(source), 'tasks': [
        {'task_id': 't1', 'video_id': 'bv-t1', 'status': 'completed', 'candidates': frozen, 'result_db': str(result)},
        {'task_id': 't2', 'video_id': 'bv-t2', 'status': 'failed', 'candidates': frozen, 'error': '测试失败'}]}
    path = batch / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return path, manifest, source, result


def test_mixed_results_and_isolation(tmp_path):
    path, _, source, result = fixture(tmp_path)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, result, path)]
    snap = build(path, tmp_path / 'review', expected_candidates=4)
    assert snap['task_count'] == 2 and snap['candidate_count'] == 4
    a, b = snap['tasks']
    assert a['candidates'][0]['decision']['status'] == 'needs_review'
    assert a['candidates'][0]['title_hash'] == sha('展示稿')
    assert a['input_quality'] is None
    assert b['candidates'][0]['decision']['status'] == 'rewrite'
    assert '无新结论' in b['conclusion_origin'] and b['error'] == '测试失败'
    assert a['video']['asset_paths']['video'] == '/original.mp4'
    assert json.loads((tmp_path / 'review' / 'snapshot.json').read_text()) == snap
    assert hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, result, path)]


def test_refuse_running_and_mismatch_before_creating_output(tmp_path):
    path, manifest, source, _ = fixture(tmp_path)
    manifest['tasks'][0]['status'] = 'running'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='尚未全部结束'):
        build(path, tmp_path / 'out', 4)
    assert not (tmp_path / 'out').exists()
    manifest['tasks'][0]['status'] = 'completed'
    manifest['tasks'][1]['candidates'][0]['headline_sha256'] = 'wrong'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='哈希'):
        build(path, tmp_path / 'out', 4)
    assert not (tmp_path / 'out').exists()


def test_strict_count_and_duplicate(tmp_path):
    path, manifest, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='93'):
        build(path, tmp_path / 'out')
    manifest['tasks'][1]['task_id'] = 't1'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='重复'):
        build(path, tmp_path / 'out', 4)
