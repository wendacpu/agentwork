import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_system.diagnostic import _open, diagnose, get_task_info, list_tasks


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / '测试?#.db'
        self.state = {
            'video': {'title': '原视频', 'evidence': [{'evidence_id': 'e1', 'text': '原文一', 'locator': '00:02', 'quality': 'raw'}]},
            'understanding': {'claims': [{'claim_id': 'cl1', 'text': '断言', 'evidence_refs': ['e1', 'missing'], 'verification': None}],
                              'extracted_sources': [{'source_id': 's1', 'name': '来源', 'url': 'https://example.org', 'evidence_refs': ['e1']}]},
            'candidates': [{'candidate_id': 'c1', 'headline': '标题一', 'evidence_ids': ['e1']},
                           {'candidate_id': 'c2', 'headline': '标题二'}],
            'reviews': [{'candidate_id': 'c1', 'issues': [{'reason': '问题一'}]}],
            'candidate_decisions': [{'candidate_id': 'c1', 'status': 'needs_review'}],
            'route': {'recommended_candidate_id': 'c2', 'action': 'handoff'},
        }
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
            conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('t1', 'v1', 'pending_human', '{}', json.dumps(self.state, ensure_ascii=False), '1', '1'))
            conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('t2', 'v2', 'prepared', '{}', json.dumps({'candidates': [{'candidate_id': 'other', 'headline': '另一任务'}]}), '2', '2'))

    def test_read_only_and_isolation(self):
        before = hashlib.sha256(self.db.read_bytes()).digest()
        before_files = set(self.db.parent.iterdir())
        self.assertEqual(len(list_tasks(self.db)), 2)
        self.assertIn('created_at', list_tasks(self.db)[0])
        with _open(self.db) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE tasks SET status='changed'")
        self.assertEqual(get_task_info(self.db, 't1')['candidates'][0]['candidate_id'], 'c1')
        result = diagnose(self.db, 't1', 'review', 'c1')
        self.assertEqual(result['candidate_id'], 'c1')
        self.assertIn('needs_review', json.dumps(result))
        self.assertIn('c2', json.dumps(result))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).digest())
        self.assertEqual(before_files, set(self.db.parent.iterdir()))
        with self.assertRaises(ValueError):
            diagnose(self.db, 't1', 'review', 'other')
        with self.assertRaises(KeyError):
            diagnose(self.db, 'no-task', 'review')
        with self.assertRaises(sqlite3.OperationalError):
            list_tasks(self.db.parent / 'not-created.db')
        self.assertFalse((self.db.parent / 'not-created.db').exists())

    def test_evidence_and_missing_verification(self):
        result = diagnose(self.db, 't1', 'evidence', 'c1', '原文一')
        text = json.dumps(result, ensure_ascii=False)
        for expected in ('原文一', '00:02', 'raw', 'https://example.org', '悬空', '未核查', '字面'):
            self.assertIn(expected, text)
        self.assertNotIn('已证实：', text)
        cited = next(s for s in result['sections'] if s['heading'] == '候选关联证据原文、定位与质量')
        self.assertIn('原文一', ''.join(cited['items']))

    def test_selection_and_missing(self):
        result = diagnose(self.db, 't1', 'evidence')
        self.assertIsNone(result['candidate_id'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertIn('请指定', ''.join(result['notices']))
        result = diagnose(self.db, 't2', 'evidence')
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn('未记录证据', text)
        self.assertIn('未记录断言', text)
        self.assertEqual(result['candidate_id'], 'other')

    def test_understanding_available_without_candidate_selection(self):
        result = diagnose(self.db, 't1', 'understanding')
        self.assertIsNone(result['candidate_id'])
        self.assertIn('主旨契约', [s['heading'] for s in result['sections']])
        self.assertIn('原文一', json.dumps(result, ensure_ascii=False))

    def test_two_databases_same_ids(self):
        other = Path(self.tmp.name) / 'other.db'
        with sqlite3.connect(other) as conn:
            conn.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
            state = {'candidates': [{'candidate_id': 'c1', 'headline': '不同库标题'}]}
            conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('t1', 'v-other', 'prepared', '{}', json.dumps(state), '1', '1'))
        a = diagnose(self.db, 't1', 'review', 'c1')
        b = diagnose(other, 't1', 'review', 'c1')
        self.assertNotEqual(a['db_path'], b['db_path'])
        self.assertNotEqual(a['title'], b['title'])
        self.assertNotEqual(a['video_id'], b['video_id'])


if __name__ == '__main__':
    unittest.main()
