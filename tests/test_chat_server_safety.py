import concurrent.futures
import http.client
import json
import sqlite3
import threading
import time
import uuid

import pytest

from agent_system import chat_server
from agent_system.chat_agent import FinalAnswer
from agent_system.config import RunConfig


@pytest.fixture
def setup_server(tmp_path, monkeypatch):
    db = tmp_path / 'batch.db'
    sqlite3.connect(db).close()
    monkeypatch.setenv('FAKE_CHAT_TEST_KEY', 'fake-only')
    calls = []
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def run_turn(self, messages, ctx, budget):
            calls.append(messages)
            ctx.focus['test'] = 'saved'
            if messages[-1]['content'] == 'fail':
                raise RuntimeError('secret-error')
            time.sleep(0.03)
            if messages[-1]['content'] == '引用':
                budget.refs_seen.add('ev:1')
                return FinalAnswer('回答', ['ev:1', 'ev:fake'], 1, {},
                                   {'ev:1': {'text': '<img src=x onerror=alert(1)>'},
                                    'ev:fake': {'text': '不得返回'}})
            return FinalAnswer('回答', [], 1, {})
    monkeypatch.setattr(chat_server, '_DeepSeekClient', FakeClient)
    cfg = RunConfig(output_dir=str(tmp_path), model_provider='deepseek',
                    base_url='https://api.deepseek.com/v1', generation_model='deepseek-chat',
                    api_key_env='FAKE_CHAT_TEST_KEY')
    servers = []
    def start(config=cfg):
        server = chat_server.make_chat_server({'batch': str(db)}, port=0, config=config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.server_port
    yield start, calls, tmp_path
    for server, thread in servers:
        server.shutdown()
        thread.join()
        server.server_close()


def request(port, path, data=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    body = json.dumps(data).encode() if data is not None else None
    h = {'Content-Type': 'application/json'} if body is not None else {}
    h.update(headers or {})
    try:
        conn.request('POST' if body is not None else 'GET', path, body, h)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw) if resp.getheader('Content-Type').startswith('application/json') else raw.decode()
    finally:
        conn.close()


def session(port):
    status, body = request(port, '/api/session', {})
    assert status == 200
    assert str(uuid.UUID(body['session_id'])) == body['session_id']
    return body['session_id']


def test_consent_unknown_and_limits(setup_server):
    start, calls, _ = setup_server
    port = start()
    sid = session(port)
    for consent in (None, 1, 'true', False):
        assert request(port, '/api/chat', {'session_id': sid, 'message': 'hi', 'confirm_cloud': consent})[0] == 403
    assert request(port, '/api/chat', {'session_id': str(uuid.uuid4()), 'message': 'hi', 'confirm_cloud': True})[0] == 404
    assert request(port, '/api/chat', {'session_id': sid, 'message': 'x' * 2001, 'confirm_cloud': True})[0] == 400
    assert not calls


def test_failure_atomic_and_success_history(setup_server):
    start, calls, tmp = setup_server
    port = start()
    sid = session(port)
    status, data = request(port, '/api/chat', {'session_id': sid, 'message': 'fail', 'confirm_cloud': True})
    assert status == 500 and 'secret-error' not in str(data)
    with sqlite3.connect(tmp / 'chat' / 'sessions.db') as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
        assert json.loads(db.execute('SELECT focus_json FROM sessions').fetchone()[0]) == {}
    for text in ('一', '二'):
        assert request(port, '/api/chat', {'session_id': sid, 'message': text, 'confirm_cloud': True})[0] == 200
    assert calls[-1][-3:] == [{'role': 'user', 'content': '一'}, {'role': 'assistant', 'content': '回答'}, {'role': 'user', 'content': '二'}]
    with sqlite3.connect(tmp / 'chat' / 'sessions.db') as db:
        rows = db.execute('SELECT role,content_json FROM messages ORDER BY seq').fetchall()
        assert [(r, json.loads(c)) for r, c in rows] == [('user', '一'), ('assistant', '回答'), ('user', '二'), ('assistant', '回答')]
        assert json.loads(db.execute('SELECT focus_json FROM sessions').fetchone()[0]) == {'test': 'saved'}


def test_same_session_serialized(setup_server):
    start, calls, _ = setup_server
    port = start()
    sid = session(port)
    def send(text):
        return request(port, '/api/chat', {'session_id': sid, 'message': text, 'confirm_cloud': True})[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(send, ['甲', '乙'])) == [200, 200]
    assert len(calls) == 2
    assert len(calls[1]) == len(calls[0]) + 2


def test_offline_and_origin(setup_server):
    start, calls, tmp = setup_server
    port = start(RunConfig(output_dir=str(tmp)))
    sid = session(port)
    assert request(port, '/api/chat', {'session_id': sid, 'message': 'hi', 'confirm_cloud': True})[0] == 503
    assert not calls
    for site in ('cross-site', 'same-site', 'same-origin', 'none'):
        assert request(port, '/', headers={'Sec-Fetch-Site': site, 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})[0] == 200
    assert request(port, '/api/session', {}, {'Sec-Fetch-Site': 'same-site'})[0] == 403
    assert request(port, '/api/session', {}, {'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'cross-site'})[0] == 403


def test_citation_and_page_safety(setup_server):
    start, _, _ = setup_server
    port = start()
    sid = session(port)
    status, body = request(port, '/api/chat', {'session_id': sid, 'message': '引用', 'confirm_cloud': True})
    assert status == 200
    assert body['citations'] == ['ev:1']
    assert body['citation_records'] == {'ev:1': {'text': '<img src=x onerror=alert(1)>'}}
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('GET', '/')
        response = conn.getresponse()
        page = response.read().decode()
        csp = response.getheader('Content-Security-Policy')
        assert chat_server._CSP == csp and "default-src 'none'" in csp
        assert "script-src 'sha256-" in csp and "script-src 'unsafe-inline'" not in csp
        assert 'id="send" type="submit" disabled' in page
        assert "document.createElement('details')" in page
        assert 'pre.textContent=' in page and 'innerHTML' not in page
        assert "if(busy||!sessionId||!text.trim())return" in page
        assert "if(!input.value)input.value=text" in page
    finally:
        conn.close()
