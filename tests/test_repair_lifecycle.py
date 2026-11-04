"""发布后复发线索、人工结案与改判失效。"""
import sqlite3
import pytest
from agent_system.repairs import RepairStore
from agent_system.repair_lifecycle import register, close, resolve


def add(store, task, video):
    candidate = {'candidate_id': 'c', 'headline': '错标题'}
    state = {'video': {'video_id': video}}
    store.enqueue(task, state, {'decision': 'rejected', 'reason': '数字错误'}, candidate)
    store.conn.commit(); store.drain()
    return next(r for r in store.list() if r['feedback_key'] == task + ':c')


def test_observe_close_and_new_video_reopens(tmp_path):
    conn = sqlite3.connect(tmp_path / 'a.db'); store = RepairStore(conn)
    row = add(store, 't1', 'v1')
    store.review_proposal(row['feedback_key'], row['revision'], 'reviewer', root_cause='数字绑定', change='单位检查', regression_tests='t1', counterexamples='t2', acceptance='回放')
    pid = row['feedback_key'] + '@' + row['revision']
    register(conn, {'proposal_ids': [pid], 'id': 'm1'})
    with pytest.raises(ValueError): close(conn, pid, 'm1', 'r', '样本', confirmed=False)
    close(conn, pid, 'm1', 'r', '已核验20个独立视频，没有同类错误', confirmed=True)
    assert conn.execute('select status from repair_deployments').fetchone()[0] == 'closed'
    add(store, 't2', 'v2')
    assert conn.execute('select status from repair_deployments').fetchone()[0] == 'recurrence_suspected'
    with pytest.raises(ValueError): close(conn, pid, 'm1', 'r', '无错误', confirmed=True)
    resolve(conn, pid, 'm1', 'r', '核实为另一根因，继续观察', confirmed=True)
    assert conn.execute('select status from repair_deployments').fetchone()[0] == 'observing'


def test_source_verdict_change_invalidates_observation(tmp_path):
    conn = sqlite3.connect(tmp_path / 'a.db'); store = RepairStore(conn)
    row = add(store, 't', 'v')
    store.review_proposal(row['feedback_key'], row['revision'], 'r', root_cause='x', change='x', regression_tests='x', counterexamples='x', acceptance='x')
    pid = row['feedback_key'] + '@' + row['revision']; register(conn, {'proposal_ids': [pid], 'id': 'm'})
    store.enqueue('t', {'video': {'video_id': 'v'}}, {'candidate_id': 'c'}, {'candidate_id': 'c'}, withdrawn=True)
    conn.commit(); store.drain()
    assert conn.execute('select status from repair_deployments').fetchone()[0] == 'invalidated'
