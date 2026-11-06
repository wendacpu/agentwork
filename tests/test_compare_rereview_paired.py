import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.compare_rereview_paired import compare_paired
from tests.test_compare_rereview_labels import feedback, isolated, sha


def setup_case(tmp_path):
    human = tmp_path / 'human.db'
    entries = [('a', 'approved', 'ready_for_human_review', 'rewrite'),
               ('b', 'rejected', 'rewrite', 'ready_for_human_review'),
               ('c', 'approved', 'needs_review', 'rewrite'),
               ('d', 'marginal', 'rewrite', 'needs_review'),
               ('e', 'approved', 'needs_review', 'needs_review')]
    feedback(human, [('batch', 't', cid, cid, 'frozen', verdict) for cid, verdict, _, _ in entries])
    supp = tmp_path / 'supp.db'
    with sqlite3.connect(supp) as c:
        c.execute('CREATE TABLE decisions (key, decision, reason, updated_at)')
        c.execute('INSERT INTO decisions VALUES (?, ?, ?, ?)', ('t|s|' + sha('s'), 'approved', '', ''))
    supp_manifest = tmp_path / 'supp.json'
    supp_manifest.write_text(json.dumps([{'task_id': 't', 'candidates': [
        {'candidate_id': 's', 'title': 's', 'hash': sha('s')},
        {'candidate_id': 'missing', 'title': 'missing', 'hash': sha('missing')}]}]))
    old = tmp_path / 'old.db'
    with sqlite3.connect(old) as c:
        c.execute('CREATE TABLE tasks (task_id, state_json)')
        state = {'candidates': [{'candidate_id': cid, 'rendered_title': cid} for cid in
                                [x[0] for x in entries] + ['s', 'missing']],
                 'candidate_decisions': [{'candidate_id': cid, 'status': status} for cid, _, status, _ in entries]
                 + [{'candidate_id': 's', 'status': 'rewrite'}, {'candidate_id': 'missing', 'status': 'rewrite'}]}
        c.execute('INSERT INTO tasks VALUES (?, ?)', ('t', json.dumps(state)))
    new = tmp_path / 'new.db'
    items = [(cid, cid, cid, status) for cid, _, _, status in entries] + [('s', 's', 's', 'ready_for_human_review')]
    isolated(new, 't', items)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'dry_run': False, 'tasks': [{'task_id': 't', 'status': 'completed',
                         'result_db': str(new), 'candidates': [{'candidate_id': cid, 'title_sha256': sha(title)}
                                                               for cid, title, _, _ in items]}]}))
    return human, supp, supp_manifest, old, manifest, new


def test_identical_intersection_and_separate_pending(tmp_path):
    human, supp, sm, old, manifest, new = setup_case(tmp_path)
    before = [p.read_bytes() for p in (human, supp, old, new)]
    report = compare_paired(human, supp, sm, old, manifest)
    f = report['frozen']
    assert f['same_intersection'] == 2
    assert f['old']['confusion'] == {'tp': 1, 'fp': 0, 'fn': 0, 'tn': 1}
    assert f['new']['confusion'] == {'tp': 0, 'fp': 1, 'fn': 1, 'tn': 0}
    assert f['pending'] == {'old_only': 1, 'new_only': 1, 'both': 1}
    assert report['supplement_diagnostic']['same_intersection'] == 1
    assert report['combined_selected_biased']['same_intersection'] == 3
    assert report['coverage']['unmatched_or_conflicting'] == 1
    assert before == [p.read_bytes() for p in (human, supp, old, new)]


def test_conflict_hash_drift_and_mismatched_manifest(tmp_path):
    human, supp, sm, old, manifest, new = setup_case(tmp_path)
    with sqlite3.connect(supp) as c:
        c.execute('INSERT INTO decisions VALUES (?, ?, ?, ?)', ('t|a|' + sha('a'), 'rejected', '', ''))
    data = json.loads(sm.read_text())
    data[0]['candidates'].append({'candidate_id': 'a', 'title': 'a', 'hash': sha('a')})
    sm.write_text(json.dumps(data))
    m = json.loads(manifest.read_text())
    m['tasks'][0]['candidates'][0]['title_sha256'] = sha('changed')
    manifest.write_text(json.dumps(m))
    report = compare_paired(human, supp, sm, old, manifest)
    assert report['database_problems']
    assert report['frozen']['same_intersection'] == 0
    assert any('人工反馈重复或跨来源冲突' in x['reasons'] for x in report['exclusions'])


def test_cli_refuses_running_without_opening_database(tmp_path):
    human, supp, sm, old, manifest, new = setup_case(tmp_path)
    m = json.loads(manifest.read_text())
    m['tasks'][0]['status'] = 'running'
    m['tasks'][0]['result_db'] = '/not/a/real/database'
    manifest.write_text(json.dumps(m))
    script = Path(__file__).resolve().parents[1] / 'scripts/compare_rereview_paired.py'
    p = subprocess.run([sys.executable, str(script), '--manifest', str(manifest), '--feedback-db', str(human),
                        '--supplement-db', str(supp), '--supplement-manifest', str(sm), '--old-db', str(old)],
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert '未结束' in p.stderr
