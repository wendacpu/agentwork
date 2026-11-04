"""提案与原反馈改判的联合发布约束。"""
import sqlite3

import pytest

from agent_system.repairs import RepairStore
from agent_system.repair_release import reviewed_ids


def test_reviewed_import_cannot_survive_source_withdrawal(tmp_path):
    from agent_system.storage import Storage
    from agent_system.repairs import backfill
    source = Storage(tmp_path / 'src.db')
    task = source.create_task('v', {'model_provider': 'deepseek'})
    state = {'video': {'video_id': 'v'}, 'candidates': [{'candidate_id': 'c', 'headline': '原标题'}], 'human_decisions': [], 'route': {}}
    source.set_state(task, 'pending_human', state)
    source.record_human_decision(task, {'candidate_id': 'c', 'decision': 'rejected', 'reason': '事实错误'})
    path = tmp_path / 'ledger.db'; conn = sqlite3.connect(path)
    store = RepairStore(conn); conn.commit(); backfill(store, tmp_path / 'src.db'); store.drain()
    row = store.list()[0]
    store.review_proposal(row['feedback_key'], row['revision'], 'human', root_cause='已核验', change='修守卫', regression_tests='test_original', counterexamples='test_negative', acceptance='不误伤')
    pid = row['feedback_key'] + '@' + row['revision']
    reviewed_ids(str(path), [pid])
    source.record_human_decision(task, {'candidate_id': 'c', 'decision': 'reopen'})
    with pytest.raises(ValueError, match='改判'):
        reviewed_ids(str(path), [pid])


def test_review_gate_rejects_draft(tmp_path):
    path = tmp_path / 'a.db'; conn = sqlite3.connect(path)
    store = RepairStore(conn)
    store.enqueue('t', {}, {'decision': 'rejected', 'reason': '错'}, {'candidate_id': 'c', 'headline': '标题'})
    conn.commit(); store.drain(); row = store.list()[0]
    with pytest.raises(ValueError):
        reviewed_ids(str(path), [row['feedback_key'] + '@' + row['revision']])
