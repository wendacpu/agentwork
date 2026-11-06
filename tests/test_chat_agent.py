"""对话 Agent 零成本测试：mock transport，不调真实模型。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_system.chat_agent import ChatClient, FinalAnswer, TurnBudget, build_messages
from agent_system.chat_tools import TOOLS, ToolContext


def _make_test_db(tmp: str) -> str:
    """构造一个含真实结构的最小任务库。"""
    db = str(Path(tmp) / 'test.db')
    state = {
        'video': {'video_id': 'BV001', 'title': '测试视频', 'author': 'A', 'url': '', 'format': '口播',
                  'domain': {'一级': '三农'}, 'duration': 60.0, 'published_at': '2026-01-01',
                  'evidence': [
                      {'evidence_id': 'e-asr-0000', 'field': 'asr', 'text': '我用这双手走着', 'locator': 'asr:5.0-8.0', 'quality': 'raw_asr'},
                      {'evidence_id': 'e-description', 'field': 'description', 'text': '博主爬行放羊', 'locator': None, 'quality': 'model_derived_uncalibrated'},
                  ]},
        'understanding': {'claims': [{'claim_id': 'c1', 'text': '双腿无法行走', 'claim_type': 'observation',
                                      'evidence_refs': ['e-asr-0000'], 'verification': None}]},
        'candidates': [{'candidate_id': 'cand-1', 'headline': '三农博主自述双腿无法行走靠双手爬行放羊',
                        'rendered_title': '三农博主自述双腿无法行走靠双手爬行放羊',
                        'claim_ids': ['c1'], 'evidence_ids': ['e-asr-0000', 'e-description'],
                        'slot_id': 'source_rewrite', 'highlight_id': 'h1'}],
        'reviews': [{'candidate_id': 'cand-1', 'gates': {
            'G1': {'status': 'pass', 'reason': '已用自述归属', 'evidence_ids': ['e-asr-0000'], 'rule_ids': ['U-B01']},
            'G3': {'status': 'revise', 'reason': '漏掉供妹妹上学核心承诺', 'evidence_ids': [], 'rule_ids': ['U-H01']}},
            'issues': [{'issue_id': 'cand-1-issue-1', 'issue_code': 'G3_CORE_POINT_OMITTED', 'severity': 'medium',
                        'reason': '标题只写爬行放羊，漏掉撑起家庭的核心承诺', 'action': '补入核心愿望',
                        'problem_span': '靠双手爬行放羊', 'evidence_ids': []}],
            'quality': {'Q1': {'score': 3, 'reason': '通顺'}}}],
        'candidate_decisions': [{'candidate_id': 'cand-1', 'status': 'rewrite', 'reason': 'REPAIRABLE_GATE_OR_QUALITY_ISSUE', 'repairable': True}],
        'route': {'action': 'handoff', 'recommended_candidate_id': 'cand-1', 'eligible_candidate_ids': ['cand-1'], 'reason': 'ranked'},
        'thesis_contract': {'thesis_id': 't1', 'topic': '博主用双手爬行放羊', 'core_promise': '看到博主坚持劳作供养家庭'},
        'highlights': [{'highlight_id': 'h1', 'role': 'main', 'value': '双手爬行放羊'}],
        'boundary': {},
    }
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
        conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('task-001', 'BV001', 'pending_human', '{}', json.dumps(state, ensure_ascii=False), '2026-11-05T10:00:00', '2026-11-05T10:00:00'))
    return db


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = _make_test_db(self.tmp.name)
        self.ctx = ToolContext(db_aliases={'test': self.db}, default_alias='test')

    def test_find_tasks_by_video(self):
        r = TOOLS['find_tasks']({'video_id': 'BV001'}, self.ctx)
        self.assertTrue(r['ok'])
        self.assertEqual(len(r['data']), 1)
        self.assertEqual(r['data'][0]['task_id'], 'task-001')

    def test_get_task_summary(self):
        r = TOOLS['get_task']({'task_id': 'task-001'}, self.ctx)
        self.assertTrue(r['ok'])
        self.assertEqual(r['data']['route']['recommended_candidate_id'], 'cand-1')
        self.assertIn('三农博主', r['data']['candidates'][0]['title'])

    def test_get_review_projects_gates(self):
        r = TOOLS['get_review']({'task_id': 'task-001', 'candidate_id': 'cand-1'}, self.ctx)
        self.assertTrue(r['ok'])
        review = r['data']['reviews'][0]
        self.assertEqual(review['gates']['G3']['status'], 'revise')
        self.assertIn('ri:cand-1-issue-1', r['refs_seen'])

    def test_evidence_coverage_detects_gap(self):
        r = TOOLS['check_evidence_coverage']({'task_id': 'task-001', 'candidate_id': 'cand-1'}, self.ctx)
        self.assertTrue(r['ok'])
        self.assertEqual(r['data']['gate_refs']['G3'], 0)
        self.assertIn('c1', r['data']['claims_unverified'])

    def test_resolve_reference_single_candidate(self):
        self.ctx.focus = {'task_id': 'task-001', 'candidate_ids': ['cand-1'], 'candidate_id': 'cand-1'}
        r = TOOLS['resolve_reference']({'phrase': '它为什么被拦'}, self.ctx)
        self.assertFalse(r['data']['needs_disambiguation'])

    def test_unknown_batch_rejected(self):
        ctx = ToolContext(db_aliases={'only': self.db}, default_alias='only')
        # 工具层直接抛错；dispatch loop 会捕获成 _err envelope（见 ChatLoopTests）
        with self.assertRaises(ValueError):
            TOOLS['find_tasks']({'batch': 'ghost'}, ctx)


class ChatLoopTests(unittest.TestCase):
    def test_dispatch_loop_with_mock_transport(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = _make_test_db(tmp.name)
        ctx = ToolContext(db_aliases={'test': db}, default_alias='test')
        budget = TurnBudget(max_tool_steps=4)

        calls = [
            # 第一轮：模型要求调 find_tasks
            {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'tc1', 'function': {'name': 'find_tasks', 'arguments': '{"video_id":"BV001"}'}}]}}],
             'usage': {'total_tokens': 100}},
            # 第二轮：模型要求调 get_review
            {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'tc2', 'function': {'name': 'get_task', 'arguments': '{"task_id":"task-001"}'}}]}}],
             'usage': {'total_tokens': 200}},
            # 第三轮：最终回答
            {'choices': [{'message': {'role': 'assistant', 'content': '记录显示这条候选因 G3 漏掉核心承诺被标记为可修复重写。'}}],
             'usage': {'total_tokens': 300}},
        ]
        idx = [0]
        def fake_post(payload, key):
            body = calls[idx[0]]
            idx[0] += 1
            return body

        client = ChatClient.__new__(ChatClient)
        client.base_url = 'http://fake'
        client.api_key_env = 'FAKE_KEY'
        client.model = 'fake-model'
        client.timeout = 10
        import os
        os.environ['FAKE_KEY'] = 'test-key'
        try:
            messages = build_messages({}, [], 'BV001 那条标题为什么被拦了？')
            final = client.run_turn(messages, ctx, budget, transport=fake_post)
        finally:
            del os.environ['FAKE_KEY']

        self.assertIsInstance(final, FinalAnswer)
        self.assertIn('G3', final.text)
        self.assertEqual(final.steps, 3)
        self.assertEqual(len(budget.calls), 2)
        self.assertIn('task:task-001', budget.refs_seen)

    def test_budget_exceeded_returns_partial(self):
        ctx = ToolContext(db_aliases={}, default_alias='')
        budget = TurnBudget(max_tool_steps=1)
        # 唯一一步就返回 tool_call → 超限
        def loop_post(payload, key):
            return {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'x', 'function': {'name': 'list_batches', 'arguments': '{}'}}]}}], 'usage': {}}
        client = ChatClient.__new__(ChatClient)
        client.base_url = 'http://fake'; client.api_key_env = 'FK'; client.model = 'm'; client.timeout = 5
        import os; os.environ['FK'] = 'k'
        try:
            final = client.run_turn([{'role': 'user', 'content': 'q'}], ctx, budget, transport=loop_post)
        finally:
            del os.environ['FK']
        self.assertIn('超限', final.text)


if __name__ == '__main__':
    unittest.main()
