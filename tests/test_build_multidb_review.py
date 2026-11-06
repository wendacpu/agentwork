import hashlib
import json
import sqlite3

import pytest

from scripts.build_multidb_review import build
from agent_system.batch_review import BatchReviewStore


def make_db(path, status, candidates):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
    state = {'video': {'title': '原题'}, 'candidates': candidates, 'route': {}, 'candidate_decisions': []}
    conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)', ('same', 'v', status, '{}', json.dumps(state), '1', '1'))
    conn.execute('CREATE TABLE candidates_by_task(task_id TEXT, candidate_id TEXT, payload_json TEXT)')
    conn.execute('INSERT INTO candidates_by_task VALUES (?,?,?)', ('same', 'extra', json.dumps({'candidate_id': 'extra', 'headline': '表中候选'})))
    conn.commit()
    conn.close()


def test_multidb_isolated_and_all_candidates(tmp_path):
    a, b = tmp_path / 'a.db', tmp_path / 'b.db'
    make_db(a, 'failed', [{'candidate_id': 'x', 'headline': '失败中的候选'}, {'candidate_id': 'y', 'headline': '另一候选'}])
    make_db(b, 'completed', [])
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)]
    out = tmp_path / 'review'
    result = build([a, b], out)
    assert (result['tasks'], result['candidates']) == (2, 4)
    store = BatchReviewStore(out)
    tasks = store.list_tasks()
    assert [t['source_status'] for t in tasks] == ['failed', 'completed']
    assert [t['candidate_count'] for t in tasks] == [3, 1]
    item = store.get_task('s001:same')
    assert len(item['state']['candidates']) == 3
    assert len({p['task_id'] for p in json.loads((out / 'SOURCE.json').read_text())['tasks']}) == 2
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)] == before
    with pytest.raises(ValueError):
        build([a, b], out)
    with pytest.raises(ValueError):
        build([a, a], tmp_path / 'other')


def test_manifest_missing_database_and_task(tmp_path):
    db = tmp_path / 'results.db'
    make_db(db, 'completed', [])
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'tasks': [
        {'task_id': 'same', 'video_id': 'v', 'status': 'completed', 'result_db': str(db)},
        {'task_id': 'absent', 'video_id': 'v2', 'status': 'failed', 'result_db': str(db)},
        {'task_id': 'lost', 'video_id': 'v3', 'status': 'failed', 'result_db': str(tmp_path / 'missing.db')},
        {'task_id': 'none', 'video_id': 'v4', 'status': 'failed', 'result_db': None},
    ]}))
    out = tmp_path / 'review'
    result = build([], out, manifest)
    assert result['tasks'] == 4
    store = BatchReviewStore(out)
    tasks = store.list_tasks()
    assert [x['source_status'] for x in tasks] == ['completed', 'failed', 'failed', 'failed']
    assert [x['candidate_count'] for x in tasks] == [1, 0, 0, 0]
    provenance = json.loads((out / 'SOURCE.json').read_text())['tasks']
    assert [x['source_task_id'] for x in provenance] == ['same', 'absent', 'lost', 'none']
    assert all(x['placeholder'] for x in provenance[1:])
    assert not (tmp_path / 'missing.db').exists()

