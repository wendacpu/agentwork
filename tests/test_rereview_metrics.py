import json
import subprocess
import sys
from pathlib import Path

from scripts.compare_rereview_labels import compare
from scripts.rereview_metrics import metrics
from tests.test_compare_rereview_labels import feedback, isolated


def test_strict_loose_pending_and_unknown(tmp_path):
    human = tmp_path / 'human.db'
    labels = [('a', 'approved', 'ready_for_human_review'), ('b', 'marginal', 'ready_for_human_review'),
              ('c', 'rejected', 'rewrite'), ('d', 'approved', 'reject'),
              ('e', 'approved', 'needs_review'), ('f', 'rejected', 'needs_review'),
              ('g', 'approved', 'unreviewed')]
    feedback(human, [('b', 't', cid, cid, 'frozen', verdict) for cid, verdict, _ in labels])
    db = tmp_path / 'iso.db'
    isolated(db, 't', [(cid, cid, cid, status) for cid, _, status in labels])
    result = metrics(compare(human, [db]), True)
    strict = result['strict']
    assert strict['confusion'] == {'tp': 1, 'fp': 1, 'fn': 1, 'tn': 1}
    assert strict['precision']['value'] == strict['recall']['value'] == 0.5
    assert strict['needs_review'] == {'total': 2, 'by_human': {'approved': 1, 'rejected': 1}}
    assert len(strict['unknown']) == 1
    assert result['loose']['confusion'] == {'tp': 2, 'fp': 0, 'fn': 1, 'tn': 1}
    assert result['loose']['recall']['value'] == 2 / 3


def test_missing_duplicates_hash_drift_and_empty_denominators(tmp_path):
    human = tmp_path / 'human.db'
    feedback(human, [('x', 't', 'a', '标题', 'frozen', 'approved'),
                     ('y', 't', 'a', '标题', 'frozen', 'rejected'),
                     ('x', 't', 'b', '原题', 'frozen', 'approved'),
                     ('x', 't', 'c', '未评', 'frozen', 'approved'),
                     ('x', 't', 'd', '缺判', 'frozen', 'approved')])
    db = tmp_path / 'iso.db'
    isolated(db, 't', [('a', '标题', '标题', 'rewrite'), ('b', '新题', '新题', 'rewrite'),
                       ('d', '缺判', '缺判', None)])
    result = metrics(compare(human, [db]))
    assert result['coverage']['ambiguous'] == 1
    assert result['coverage']['missing_rereview'] == 2
    assert result['coverage']['missing_feedback'] == 1
    assert result['coverage']['incomplete'] == 1
    assert result['strict']['confusion'] == dict(tp=0, fp=0, fn=0, tn=0)
    assert result['strict']['precision']['value'] is None
    assert result['strict']['recall']['value'] is None
    assert 'loose' not in result


def test_cli_preserves_databases(tmp_path):
    human = tmp_path / 'human.db'
    feedback(human, [('b', 't', 'a', '标题', 'frozen', 'approved')])
    db = tmp_path / 'iso.db'
    isolated(db, 't', [('a', '标题', '标题', 'ready_for_human_review')])
    before = human.read_bytes(), db.read_bytes()
    script = Path(__file__).resolve().parents[1] / 'scripts/rereview_metrics.py'
    proc = subprocess.run([sys.executable, str(script), str(human), str(db), '--loose'],
                          capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)['strict']['precision']['value'] == 1
    assert before == (human.read_bytes(), db.read_bytes())
