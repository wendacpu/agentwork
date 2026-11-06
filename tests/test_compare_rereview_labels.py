import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.compare_rereview_labels import compare


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def feedback(path, entries):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE candidate_decisions_v2(batch_id,task_id,candidate_id,title_hash,payload_json)')
        for batch, task, cid, title, scope, decision in entries:
            h = sha(title)
            p = dict(task_id=task, candidate_id=cid, title_hash=h, original_headline=title,
                     scope=scope, decision=decision)
            c.execute('INSERT INTO candidate_decisions_v2 VALUES(?,?,?,?,?)',
                      (batch, task, cid, h, json.dumps(p)))


def isolated(path, task, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    frozen = [dict(candidate_id=cid, title_sha256=sha(title)) for cid, title, actual, status in entries]
    state = {'candidates': [dict(candidate_id=cid, rendered_title=actual) for cid, title, actual, status in entries],
             'candidate_decisions': [dict(candidate_id=cid, status=status) for cid, title, actual, status in entries if status],
             'route': {'action': 'needs_review'}}
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE rereview_snapshot(task_id,candidates_json)')
        c.execute('CREATE TABLE tasks(task_id,state_json,status)')
        c.execute('INSERT INTO rereview_snapshot VALUES(?,?)', (task, json.dumps(frozen)))
        c.execute('INSERT INTO tasks VALUES(?,?,?)', (task, json.dumps(state), 'pending_human'))


def test_strict_pairs_missing_diagnostic_and_status(tmp_path):
    db = tmp_path / 'human.db'
    feedback(db, [('b', 't', 'a', '标题一', 'frozen', 'approved'),
                  ('b', 't', 'b', '标题二', 'frozen', 'rejected'),
                  ('b', 't', 'c', '诊断', 'diagnostic', 'approved'),
                  ('b', 't', 'd', '不变', 'frozen', 'marginal')])
    isolated(tmp_path / 'nested' / 'agent.db', 't', [('a', '标题一', '标题一', 'ready'),
                                                        ('b', '标题二', '标题变了', 'ready'),
                                                        ('e', '额外', '额外', 'rewrite')])
    report = compare(db, [tmp_path / 'nested'])
    assert report['summary']['paired'] == 1
    assert report['matrix'] == [{'human_decision': 'approved', 'system_status': 'ready', 'count': 1}]
    assert len(report['incomplete']) == len(report['missing_rereview']) == len(report['missing_feedback']) == 1
    assert len(report['diagnostic']) == 1


def test_duplicate_and_different_hash_never_guess(tmp_path):
    db = tmp_path / 'human.db'
    feedback(db, [('a', 't', 'x', '原题', 'frozen', 'approved'),
                  ('b', 't', 'x', '原题', 'frozen', 'rejected'),
                  ('a', 't', 'y', '原题', 'frozen', 'approved')])
    iso = tmp_path / 'agent.db'
    isolated(iso, 't', [('x', '原题', '原题', 'ready'), ('y', '新题', '新题', 'ready')])
    report = compare(db, [iso])
    assert report['summary']['ambiguous'] == 1
    assert report['summary']['missing_rereview'] == report['summary']['missing_feedback'] == 1
    assert not report['paired']


def test_missing_status_and_repeat_isolated(tmp_path):
    db = tmp_path / 'human.db'
    feedback(db, [('a', 't', 'x', '标题', 'frozen', 'approved'), ('a', 't', 'y', '标题', 'frozen', 'approved')])
    isolated(tmp_path / 'i1' / 'agent.db', 't', [('x', '标题', '标题', 'ready'), ('y', '标题', '标题', None)])
    isolated(tmp_path / 'i2' / 'agent.db', 't', [('x', '标题', '标题', 'ready')])
    report = compare(db, [tmp_path / 'i1', tmp_path / 'i2'])
    assert report['summary']['ambiguous'] == 1
    assert report['summary']['incomplete'] == 1


def test_cli_read_only_and_bad_explicit_database(tmp_path):
    db = tmp_path / 'human.db'
    feedback(db, [])
    other = tmp_path / 'other.db'
    with sqlite3.connect(other) as c:
        c.execute('CREATE TABLE unrelated(x)')
    before = (db.read_bytes(), other.read_bytes())
    script = Path(__file__).resolve().parents[1] / 'scripts/compare_rereview_labels.py'
    proc = subprocess.run([sys.executable, str(script), str(db), str(other)], capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)['summary']['database_problems'] == 1
    assert before == (db.read_bytes(), other.read_bytes())
