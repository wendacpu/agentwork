import json

import pytest

from agent_system.chat_agent import ChatClient, TurnBudget
from agent_system.chat_tools import ToolContext
from agent_system.model import ModelError


def call(name, args, ident='1'):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def response(content=None, calls=None):
    msg = {'role': 'assistant', 'content': content}
    if calls is not None:
        msg['tool_calls'] = calls
    return {'choices': [{'message': msg}], 'usage': {}}


def runner(monkeypatch, responses, budget=None):
    monkeypatch.setenv('TEST_CHAT_KEY', 'secret')
    client = ChatClient('https://unused.invalid', 'TEST_CHAT_KEY', 'test')
    sent = []
    def transport(payload, key):
        sent.append(json.loads(json.dumps(payload)))
        return responses.pop(0)
    answer = client.run_turn([{'role': 'user', 'content': 'test'}], ToolContext(), budget or TurnBudget(), transport)
    return answer, sent


def test_rejects_non_object_and_unknown_tool(monkeypatch):
    answer, sent = runner(monkeypatch, [response(calls=[call('get_evidence', []), call('unknown-secret', {})]), response('完成')])
    assert answer.text == '完成'
    tool_messages = [m for m in sent[1]['messages'] if m.get('role') == 'tool']
    assert 'JSON 对象' in tool_messages[0]['content']
    assert 'unknown-secret' not in tool_messages[1]['content']


def test_refs_require_returned_original_and_answer_marker(monkeypatch):
    from agent_system import chat_agent
    monkeypatch.setitem(chat_agent.TOOLS, 'fake', lambda args, ctx: {'ok': True, 'refs_seen': ['ev:fake', 'ev:real'], 'data': [{'evidence_id': 'real', 'text': '原文'}, {'evidence_id': 'fake'}]})
    answer, _ = runner(monkeypatch, [response(calls=[call('fake', {})]), response('依据 [[ev:real]] 和 [[ev:fake]]；没标注的不引用')])
    assert answer.citations == ['ev:real']
    assert answer.citation_records['ev:real']['text'] == '原文'
    assert '[[ev:fake]]' not in answer.text


def test_conflicting_duplicate_id_is_not_a_valid_citation(monkeypatch):
    from agent_system import chat_agent
    monkeypatch.setitem(chat_agent.TOOLS, 'fake', lambda args, ctx: {'ok': True, 'data': [
        {'evidence_id': 'same', 'text': '原文一'}, {'evidence_id': 'same', 'text': '原文二'}]})
    answer, _ = runner(monkeypatch, [response(calls=[call('fake', {})]), response('[[ev:same]]')])
    assert not answer.citations
    assert '引用未核实' in answer.text


def test_result_too_large_never_confers_reference(monkeypatch):
    from agent_system import chat_agent
    monkeypatch.setitem(chat_agent.TOOLS, 'fake', lambda args, ctx: {'ok': True, 'data': [{'evidence_id': 'x', 'text': 'x' * 500}]})
    answer, sent = runner(monkeypatch, [response(calls=[call('fake', {})]), response('[[ev:x]]')], TurnBudget(max_tool_result_chars=100))
    assert not answer.citations
    assert 'x' * 100 not in str(sent[1])


def test_tool_and_model_budget_never_exceeded(monkeypatch):
    from agent_system import chat_agent
    invoked = []
    monkeypatch.setitem(chat_agent.TOOLS, 'fake', lambda args, ctx: invoked.append(1) or {'ok': True, 'data': []})
    answer, sent = runner(monkeypatch, [response(calls=[call('fake', {}), call('fake', {}, '2')])], TurnBudget(max_tool_steps=1))
    assert len(sent) == 1 and not invoked and answer.steps == 1
    answer, sent = runner(monkeypatch, [response(calls=[call('fake', {})])], TurnBudget(max_model_calls=1))
    assert len(sent) == 1 and answer.steps == 1


def test_context_limit_prevents_request(monkeypatch):
    answer, sent = runner(monkeypatch, [], TurnBudget(max_context_chars=1))
    assert not sent and answer.steps == 0


def test_exceptions_are_sanitized(monkeypatch):
    from agent_system import chat_agent
    def fail(args, ctx):
        raise RuntimeError('PRIVATE TOKEN')
    monkeypatch.setitem(chat_agent.TOOLS, 'fake', fail)
    answer, sent = runner(monkeypatch, [response(calls=[call('fake', {})]), response('失败')])
    assert 'PRIVATE TOKEN' not in str(sent)
    monkeypatch.setenv('TEST_CHAT_KEY', 'secret')
    client = ChatClient('https://unused.invalid', 'TEST_CHAT_KEY', 'test')
    def broken(payload, key):
        raise RuntimeError('PRIVATE TOKEN')
    with pytest.raises(ModelError) as error:
        client.run_turn([], ToolContext(), TurnBudget(), broken)
    assert 'PRIVATE TOKEN' not in str(error.value)
