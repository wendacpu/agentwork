"""离线 HTTP 端到端：仅替换模型传输，不替换对话循环或工具。"""
import copy
import http.client
import json
import sqlite3
import threading
import uuid

import pytest

from agent_system import chat_server
from agent_system.config import RunConfig


TEXT = '原始口述：我用双手走过这条路。'
REASON = '标题漏掉了核心承诺'


def make_db(path, text=TEXT):
    state = {
        'video': {'video_id': 'BV001', 'title': '测试视频', 'evidence': [
            {'evidence_id': 'e1', 'field': 'asr', 'text': text,
             'locator': 'asr:1-2', 'quality': 'raw_asr'}]},
        'candidates': [{'candidate_id': 'c1', 'headline': '测试标题',
                        'rendered_title': '测试标题', 'evidence_ids': ['e1']}],
        'reviews': [{'candidate_id': 'c1', 'gates': {}, 'issues': [
            {'issue_id': 'i1', 'reason': REASON, 'evidence_ids': ['e1']}]}],
        'candidate_decisions': [{'candidate_id': 'c1', 'status': 'rewrite'}],
        'route': {}, 'understanding': {}, 'thesis_contract': {}, 'highlights': [],
    }
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                   ('t1', 'BV001', 'pending_human', '{}', json.dumps(state, ensure_ascii=False), '2026-01-01', '2026-01-01'))


def call(name, args, ident):
    return {'choices': [{'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': ident, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}]}}]}


def answer(text):
    return {'choices': [{'message': {'role': 'assistant', 'content': text}}]}


def request(port, path, data, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('POST', path, json.dumps(data, ensure_ascii=False).encode(),
                     {'Content-Type': 'application/json', **(headers or {})})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    db1, db2 = tmp_path / 'one.db', tmp_path / 'two.db'
    make_db(db1)
    make_db(db2, '另一批次原文')
    before = {p: p.read_bytes() for p in (db1, db2)}
    monkeypatch.setenv('OFFLINE_CHAT_KEY', 'test-key')
    script, sent, servers = [], [], []

    def post(self, payload, key):
        assert key == 'test-key'
        sent.append(copy.deepcopy(payload))
        assert script, '未预期的模型请求'
        return script.pop(0)

    monkeypatch.setattr(chat_server._DeepSeekClient, '_post', post)
    cfg = RunConfig(output_dir=str(tmp_path / 'output'), model_provider='deepseek',
                    base_url='https://api.deepseek.com/v1', generation_model='deepseek-chat',
                    api_key_env='OFFLINE_CHAT_KEY')

    def start(multiple=False):
        aliases = {'one': str(db1)}
        if multiple:
            aliases['two'] = str(db2)
        server = chat_server.make_chat_server(aliases, port=0, config=cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        status, body = request(server.server_port, '/api/session', {})
        assert status == 200
        assert str(uuid.UUID(body['session_id'])) == body['session_id']
        return server.server_port, body['session_id']

    yield start, script, sent, tmp_path
    for server, thread in servers:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()
    assert {p: p.read_bytes() for p in before} == before


def chat(port, sid, text, consent=True):
    return request(port, '/api/chat', {'session_id': sid, 'message': text, 'confirm_cloud': consent})


def tool_results(payload):
    return [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']


def test_two_turns_real_loop_focus_citations_and_consent(offline):
    start, script, sent, tmp = offline
    port, sid = start()
    script.extend([call('find_tasks', {'video_id': 'BV001'}, 'a'),
                   call('get_task', {'task_id': 't1', 'batch': 'one'}, 'b'),
                   call('get_review', {'candidate_id': 'c1'}, 'c'),
                   answer('历史评审判断：' + REASON + ' [[ri:i1]]'),
                   call('get_evidence', {}, 'd'),
                   answer('记录显示：' + TEXT + ' [[ev:e1]]')])
    assert chat(port, sid, '查 BV001', False)[0] == 403
    assert chat(port, str(uuid.uuid4()), '查 BV001')[0] == 404
    assert request(port, '/api/chat', {'session_id': sid, 'message': '查 BV001', 'confirm_cloud': True},
                   {'Origin': 'https://evil.example'})[0] == 403
    assert sent == []
    status, first = chat(port, sid, '查 BV001')
    assert status == 200
    assert first['citations'] == ['ri:i1']
    assert first['citation_records']['ri:i1']['reason'] == REASON
    assert tool_results(sent[1])[0]['data'][0]['task_id'] == 't1'
    assert tool_results(sent[2])[-1]['data']['candidates'][0]['candidate_id'] == 'c1'
    assert tool_results(sent[3])[-1]['data']['reviews'][0]['issues'][0]['reason'] == REASON
    assert chat(port, sid, '证据呢', False)[0] == 403
    assert len(sent) == 4
    status, second = chat(port, sid, '证据呢')
    assert status == 200
    assert second['citations'] == ['ev:e1']
    assert second['citation_records']['ev:e1']['text'] == TEXT
    assert TEXT in sent[5]['messages'][-1]['content']
    assert '"task_id": "t1"' in sent[4]['messages'][0]['content']
    assert '"batch": "one"' in sent[4]['messages'][0]['content']
    assert sent[4]['messages'][-3:] == [
        {'role': 'user', 'content': '查 BV001'},
        {'role': 'assistant', 'content': first['answer']},
        {'role': 'user', 'content': '证据呢'}]
    with sqlite3.connect(tmp / 'output' / 'chat' / 'sessions.db') as db:
        focus = json.loads(db.execute('SELECT focus_json FROM sessions WHERE id=?', (sid,)).fetchone()[0])
        assert focus == {'task_id': 't1', 'batch': 'one', 'candidate_ids': ['c1'], 'candidate_id': 'c1'}
        ledger = db.execute('SELECT tool_calls_json FROM turn_ledger WHERE session_id=? ORDER BY turn', (sid,)).fetchall()
        assert [[c['tool'] for c in json.loads(row[0])] for row in ledger] == [
            ['find_tasks', 'get_task', 'get_review'], ['get_evidence']]
    assert not script


def test_duplicate_task_ids_require_batch(offline):
    start, script, sent, _ = offline
    port, sid = start(multiple=True)
    script.extend([call('find_tasks', {'video_id': 'BV001'}, 'a'),
                   call('get_task', {'task_id': 't1'}, 'b'),
                   call('get_task', {'task_id': 't1', 'batch': 'two'}, 'c'),
                   call('get_evidence', {}, 'd'), answer('另一批次 [[ev:e1]]')])
    status, result = chat(port, sid, '查同名任务')
    assert status == 200
    found = tool_results(sent[1])[0]
    assert {(x['batch'], x['task_id']) for x in found['data']} == {('one', 't1'), ('two', 't1')}
    assert found['notices']
    assert tool_results(sent[2])[-1]['ok'] is False
    assert tool_results(sent[3])[-1]['ok'] is True
    assert result['citation_records']['ev:e1']['text'] == '另一批次原文'
    assert not script


def test_missing_evidence_cannot_forge_citation(offline):
    start, script, sent, _ = offline
    port, sid = start()
    script.extend([call('get_task', {'task_id': 't1', 'batch': 'one'}, 'a'),
                   call('get_evidence', {'evidence_ids': ['does-not-exist']}, 'b'),
                   answer('编造 [[ev:does-not-exist]] 以及未查询 [[ev:e1]]')])
    status, result = chat(port, sid, '给我证据')
    assert status == 200
    assert tool_results(sent[2])[-1]['data'] == [{'evidence_id': 'does-not-exist', 'missing': True}]
    assert result['citations'] == []
    assert result['citation_records'] == {}
    assert '[[ev:' not in result['answer']
    assert result['answer'].count('（引用未核实）') == 2
    assert not script
