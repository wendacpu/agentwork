"""反馈修订、事务、重试及幂等回填。"""
import json
import sqlite3

import pytest

from agent_system.repairs import RepairStore, backfill
from agent_system.storage import Storage


def fixture_state():
    return {'video': {'video_id': 'v1', 'evidence': [{'evidence_id': 'e1', 'text': '本视频AI生成'}]}, 'candidates': [{'candidate_id': 'c1', 'headline': '猫吃饭', 'evidence_ids': ['e1']}], 'route': {}, 'human_decisions': []}


def test_live_feedback_reopen_and_repeat_are_not_lost(tmp_path):
    storage = Storage(tmp_path / 'agent.db')
    task = storage.create_task('v1', {})
    storage.set_state(task, 'pending_human', fixture_state())
    d = {'candidate_id': 'c1', 'decision': 'rejected', 'reason': '漏标AI生成'}
    storage.record_human_decision(task, dict(d))
    store = RepairStore(storage.conn)
    first = store.list()[0]
    assert first['diagnosis']['family'] == 'content_nature'
    assert first['diagnosis']['status'] == 'hypothesis'
    assert first['proposal']['release_eligible'] is False
    assert first['status'] == 'triage'
    storage.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'reopen'})
    assert store.list()[0]['status'] == 'withdrawn'
    storage.record_human_decision(task, dict(d))
    assert store.list()[0]['status'] == 'triage'
    assert store.list()[0]['revision'] != first['revision']
    assert store.drain()['processed'] == 0


def test_outbox_rolls_back_with_human_decision(tmp_path, monkeypatch):
    s = Storage(tmp_path / 'agent.db'); task = s.create_task('v1', {})
    s.set_state(task, 'pending_human', fixture_state())
    def fail(*args): raise RuntimeError('模拟入库失败')
    monkeypatch.setattr(s, '_auto_case', fail)
    with pytest.raises(RuntimeError):
        s.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'rejected', 'reason': '错误'})
    assert s.conn.execute('select count(*) from repair_outbox').fetchone()[0] == 0
    assert s.conn.execute('select count(*) from human_decisions').fetchone()[0] == 0


def test_reviewed_proposal_invalidated_by_changed_verdict(tmp_path):
    s = Storage(tmp_path / 'a.db'); task = s.create_task('v1', {})
    s.set_state(task, 'pending_human', fixture_state())
    s.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'marginal', 'reason': '数字错'})
    store = RepairStore(s.conn); row = store.list()[0]
    store.review_proposal(row['feedback_key'], row['revision'], 'tester', root_cause='x', change='x', regression_tests='x', counterexamples='x', acceptance='x')
    s.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'approved'})
    assert store.list()[0]['status'] == 'no_defect'
    with pytest.raises(ValueError):
        store.review_proposal(row['feedback_key'], row['revision'], 'tester', root_cause='x', change='x', regression_tests='x', counterexamples='x', acceptance='x')


def test_history_backfill_is_idempotent_and_readonly(tmp_path):
    source = Storage(tmp_path / 'src.db'); task = source.create_task('v1', {'model_provider': 'deepseek'})
    source.set_state(task, 'pending_human', fixture_state())
    source.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'rejected', 'reason': '边角看点'})
    conn = sqlite3.connect(tmp_path / 'ledger.db'); store = RepairStore(conn); conn.commit()
    assert backfill(store, tmp_path / 'src.db') == 1
    assert store.drain()['processed'] == 1
    assert backfill(store, tmp_path / 'src.db') == 1
    assert store.drain()['processed'] == 0
    assert len(store.list()) == 1


def test_separate_case_bank_still_delivers_feedback(tmp_path):
    storage = Storage(tmp_path / 'tasks.db', case_bank_path=tmp_path / 'cases.db')
    task = storage.create_task('v1', {})
    storage.set_state(task, 'pending_human', fixture_state())
    storage.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'rejected', 'reason': '数字错误'})
    assert RepairStore(storage.conn).list()[0]['status'] == 'triage'
    assert storage.case_conn.execute('select count(*) from feedback').fetchone()[0] == 1


def test_failed_event_retries_without_losing_human_verdict(tmp_path, monkeypatch):
    import agent_system.repairs as repairs
    s = Storage(tmp_path / 'a.db'); task = s.create_task('v1', {})
    s.set_state(task, 'pending_human', fixture_state())
    original = repairs.diagnose
    def fail(_): raise RuntimeError('归因临时故障')
    monkeypatch.setattr(repairs, 'diagnose', fail)
    s.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'rejected', 'reason': '错误'})
    assert s.conn.execute('select count(*) from human_decisions').fetchone()[0] == 1
    assert s.conn.execute('select status from repair_outbox').fetchone()[0] == 'failed'
    monkeypatch.setattr(repairs, 'diagnose', original)
    assert s.process_repairs()['processed'] == 1
    assert s.process_repairs()['processed'] == 0


def test_group_defects_collapses_one_decision_per_root_cause(tmp_path):
    from agent_system.repairs import group_defects
    s = Storage(tmp_path / 'a.db')
    for i, reason in enumerate(['边角看点', '没切中核心', '数字错误']):
        task = s.create_task(f'v{i}', {})
        s.set_state(task, 'pending_human', {**fixture_state(), 'video': {'video_id': f'v{i}', 'evidence': []}})
        s.record_human_decision(task, {'candidate_id': 'c1', 'decision': 'rejected', 'reason': reason})
    result = group_defects(RepairStore(s.conn))
    assert result['total_defects'] == 3
    families = {g['family']: g for g in result['groups']}
    assert families['selection']['count'] == 2, '同类理由必须并到一组'
    assert families['selection']['distinct_videos'] == 2
    assert families['numeric']['count'] == 1
    assert all(g['examples'] for g in result['groups'])
    # 聚合只读，不改提案状态
    assert all(r['status'] == 'triage' for r in RepairStore(s.conn).list())
