import hashlib
import http.client
import json
import sqlite3
import threading

import pytest

from agent_system.diagnostic_server import make_server


def _request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, raw


@pytest.fixture
def running(tmp_path):
    db = tmp_path / 'test.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY,video_id TEXT,status TEXT,config_json TEXT,state_json TEXT,created_at TEXT,updated_at TEXT)')
        conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)', ('t1', 'v1', 'generating', '{}', json.dumps({'video': {'video_id': 'v1', 'title': '测试', 'evidence': []}, 'candidates': []}), '2026', '2026'))
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    server = make_server(str(db), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, db, before
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_server_serves_readonly_and_does_not_change_db(running):
    server, db, before = running
    assert _request(server, 'GET', '/')[0] == 200
    status, data = _request(server, 'GET', '/api/tasks')
    assert status == 200
    assert json.loads(data)['tasks'][0]['task_id'] == 't1'
    status, data = _request(server, 'POST', '/api/diagnose', json.dumps({'task_id': 't1', 'scenario': 'understanding'}), {'Content-Type': 'application/json'})
    assert status == 200
    assert json.loads(data)['task_id'] == 't1'
    assert _request(server, 'POST', '/api/run', '{}', {'Content-Type': 'application/json'})[0] == 404
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'generating'


def test_rejects_cross_site_and_bad_input(running):
    server, _, _ = running
    assert _request(server, 'GET', '/api/tasks', headers={'Host': f'evil.example:{server.server_port}'})[0] == 403
    assert _request(server, 'GET', '/api/tasks', headers={'Origin': 'http://evil.example'})[0] == 403
    assert _request(server, 'GET', '/api/tasks', headers={'Sec-Fetch-Site': 'cross-site'})[0] == 403
    assert _request(server, 'POST', '/api/diagnose', '{}', {'Content-Type': 'text/plain'})[0] == 415
    assert _request(server, 'POST', '/api/diagnose', '{"task_id": []}', {'Content-Type': 'application/json'})[0] == 400


def test_local_chat_requires_confirmation_and_preserves_readonly(running, monkeypatch):
    server, db, before = running
    calls = []
    def fake_answer(result, question, history=None):
        calls.append((result['task_id'], question, history))
        return {'answer': '测试回答', 'citations': [], 'evidence': [], 'notice': '待核'}
    monkeypatch.setattr('agent_system.diagnostic_server.answer_local', fake_answer)
    body = {'task_id': 't1', 'scenario': 'understanding', 'question': '核心是什么？'}
    headers = {'Content-Type': 'application/json'}
    assert _request(server, 'POST', '/api/chat', json.dumps(body), headers)[0] == 400
    assert calls == []
    body.update(confirm_local_model=True, history=[{'role': 'user', 'content': '前问'}])
    status, data = _request(server, 'POST', '/api/chat', json.dumps(body), headers)
    assert status == 200 and json.loads(data)['chat']['answer'] == '测试回答'
    assert calls == [('t1', '核心是什么？', body['history'])]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_loopback_only_and_existing_database(tmp_path):
    with pytest.raises(ValueError):
        make_server(str(tmp_path / 'none.db'), host='0.0.0.0', port=0)
    with pytest.raises(FileNotFoundError):
        make_server(str(tmp_path / 'none.db'), port=0)
