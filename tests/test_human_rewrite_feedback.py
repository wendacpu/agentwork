import hashlib
import json
import sqlite3

import pytest

from agent_system.human_rewrite import load_feedback


def fixture(tmp_path):
    title = '博主称间奏封神'
    digest = hashlib.sha256(title.encode()).hexdigest()
    snapshot = {'tasks': [{'task_id': 't1', 'video_id': 'v1', 'status': 'completed', 'candidates': [
        {'candidate': {'candidate_id': 'c1', 'headline': title}, 'title_hash': digest}]}]}
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(snapshot, ensure_ascii=False))
    db = tmp_path / 'feedback.db'
    c = sqlite3.connect(db)
    c.execute('create table snapshot_identity (digest text)')
    c.execute('insert into snapshot_identity values (?)', (hashlib.sha256(path.read_bytes()).hexdigest(),))
    c.execute('create table candidate_decisions_v2 (batch_id text, task_id text, candidate_id text, title_hash text, payload_json text)')
    f = {'task_id': 't1', 'video_id': 'v1', 'candidate_id': 'c1', 'title_hash': digest, 'scope': 'frozen', 'decision': 'rejected', 'reason': '不可自称封神'}
    c.execute('insert into candidate_decisions_v2 values (?,?,?,?,?)', (tmp_path.name, 't1', 'c1', digest, json.dumps(f)))
    c.commit(); c.close()
    return path, db, digest


def test_valid_feedback_and_identity_isolation(tmp_path):
    path, db, digest = fixture(tmp_path)
    _, result = load_feedback(str(path), str(db), 't1', 'c1', digest)
    assert result['reason'] == '不可自称封神'
    for tid, cid, h in [('other', 'c1', digest), ('t1', 'other', digest), ('t1', 'c1', '0' * 64)]:
        with pytest.raises(ValueError):
            load_feedback(str(path), str(db), tid, cid, h)


def test_reopened_or_stale_feedback_rejected(tmp_path):
    path, db, digest = fixture(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute('delete from candidate_decisions_v2')
    with pytest.raises(ValueError):
        load_feedback(str(path), str(db), 't1', 'c1', digest)
    path.write_text(path.read_text() + ' ')
    with pytest.raises(ValueError, match='快照身份'):
        load_feedback(str(path), str(db), 't1', 'c1', digest)
