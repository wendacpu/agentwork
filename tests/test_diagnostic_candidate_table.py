"""任务候选表的历史候选只能作为诊断口径，不能污染冻结评审。"""
import hashlib
import json
import sqlite3

from agent_system.batch_review import BatchReviewStore


def test_task_scoped_candidates_are_diagnostic_only(tmp_path):
    db = tmp_path / 'agent.db'
    title = '冻结候选'
    frozen = {'candidate_id': 'f', 'headline': title, 'rendered_title': title}
    historical = {'candidate_id': 'h', 'headline': '历史草稿', 'rendered_title': '历史草稿'}
    state = {'video': {}, 'candidates': [frozen], 'route': {}}
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE tasks(task_id TEXT, status TEXT, state_json TEXT)')
        c.execute('CREATE TABLE candidates_by_task(task_id TEXT, candidate_id TEXT, payload_json TEXT)')
        c.execute('INSERT INTO tasks VALUES(?,?,?)', ('t', 'pending_human', json.dumps(state)))
        for task, candidate in [('t', historical), ('t', frozen), ('other', {'candidate_id': 'other', 'headline': '其他任务'})]:
            c.execute('INSERT INTO candidates_by_task VALUES(?,?,?)', (task, candidate['candidate_id'], json.dumps(candidate)))
    mapping = [{'task_id': 't', 'video_id': 'v', 'blind_id': 'R01', 'category': '测试',
                'candidates': [{'candidate_id': 'f', 'headline': title,
                                'title_sha256': hashlib.sha256(title.encode()).hexdigest()}]}]
    (tmp_path / 'review_mapping.json').write_text(json.dumps(mapping), encoding='utf-8')
    before = db.read_bytes()
    store = BatchReviewStore(tmp_path)
    detail = store.get_task('t')
    assert [c['candidate_id'] for c in detail['state']['candidates']] == ['f']
    assert [c['candidate_id'] for c in detail['diagnostic_candidates']] == ['h']
    assert detail['diagnostic_candidates'][0]['status'] == 'unknown'
    assert store.list_tasks()[0]['candidate_count'] == 1
    # 历史候选可以评，但只进诊断口径；冻结进度与冻结口径账本不受影响。
    saved = store.record_decision({'task_id': 't', 'candidate_id': 'h', 'decision': 'approved'})
    assert saved['decision']['scope'] == 'diagnostic'
    assert saved['decision']['original_headline'] == '历史草稿'
    assert store.feedback()['decisions'] == []
    assert [d['candidate_id'] for d in store.feedback()['diagnostic_decisions']] == ['h']
    assert store.list_tasks()[0]['review_status'] == 'unreviewed'
    assert db.read_bytes() == before
