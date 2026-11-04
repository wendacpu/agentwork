"""对照批次不得覆盖旧数据或丢弃缺产样本。"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_compare_review import pick_delivery_candidate, title_hash
from agent_system.batch_review import title_hash as review_hash

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'build_compare_review.py'


def prepare(tmp_path, baseline_ids=('v1',), current_ids=('v1',), baseline_model='m1', current_model='m1'):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps({
        'model_provider': 'juflow', 'generation_model': baseline_model,
        'results': [{'video_id': vid, 'title': '原标题'} for vid in baseline_ids]}))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'samples': [{'index': i, 'video_id': vid, 'category': '测试'}
                                                 for i, vid in enumerate(dict.fromkeys([*baseline_ids, *current_ids]) or ['v1'], 1)]}))
    batch = tmp_path / 'source'
    batch.mkdir()
    (batch / 'config.json').write_text(json.dumps({'model_provider': 'juflow', 'generation_model': current_model}))
    with sqlite3.connect(batch / 'agent.db') as db:
        db.execute('create table tasks(task_id text, video_id text, status text, state_json text)')
        for vid in current_ids:
            db.execute('insert into tasks values (?,?,?,?)', (f't-{vid}', vid, 'stopped', json.dumps({'stop_reason': 'NO_USABLE_CLAIM'})))
    return baseline, manifest, batch


def run(baseline, manifest, batch, out):
    return subprocess.run([sys.executable, str(SCRIPT), '--baseline', str(baseline), '--manifest', str(manifest), '--current-batch', str(batch), '--out', str(out)], capture_output=True, text=True)


def test_existing_source_directory_never_overwritten(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    before = (batch / 'agent.db').read_bytes()
    result = run(baseline, manifest, batch, batch)
    assert result.returncode != 0
    assert (batch / 'agent.db').read_bytes() == before


def test_missing_system_title_retains_video_and_baseline(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    mapping = json.loads((out / 'review_mapping.json').read_text())
    assert len(mapping) == 1
    assert len(mapping[0]['candidates']) == 1
    assert mapping[0]['candidates'][0]['arm'] == 'baseline'
    assert mapping[0]['stop_reason'] == 'NO_USABLE_CLAIM'


def test_stopped_candidate_is_not_a_delivery():
    state = {'route': {'action': 'stop'}, 'candidates': [{'candidate_id': 'c1', 'headline': '标题'}]}
    assert pick_delivery_candidate(state) == (None, 'none')


def test_unequal_arm_sample_sets_are_refused(tmp_path):
    """一臂多跑了几条就不是配对：不能取交集硬凑。"""
    baseline, manifest, batch = prepare(tmp_path, baseline_ids=('v1', 'v2'))
    result = run(baseline, manifest, batch, tmp_path / 'new')
    assert result.returncode != 0
    assert '不是配对实验' in result.stderr


def test_both_arms_missing_manifest_video_are_refused(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    data = json.loads(manifest.read_text())
    data['samples'].append({'index': 2, 'video_id': 'v2', 'category': '测试'})
    manifest.write_text(json.dumps(data))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '未完整覆盖 manifest' in result.stderr
    assert 'v2' in result.stderr
    assert not out.exists()


def test_manifest_coverage_accepts_explicit_failure(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    data = json.loads(baseline.read_text())
    data['results'] = []
    data['errors'] = [{'video_id': 'v1', 'error': 'failed'}]
    baseline.write_text(json.dumps(data))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    note = json.loads((out / 'compare_note.json').read_text())
    assert note['comparability']['videos_covered_both_arms'] == 1
    assert len(note['incomplete_pairs']) == 1
    assert not json.loads((out / 'review_mapping.json').read_text())[0]['candidates']


@pytest.mark.parametrize('error', [None, '', '  ', 123, {}, []])
def test_baseline_failure_requires_error_message(tmp_path, error):
    baseline, manifest, batch = prepare(tmp_path)
    data = json.loads(baseline.read_text())
    data['results'] = []
    data['errors'] = [{'video_id': 'v1', 'error': error}]
    baseline.write_text(json.dumps(data))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '错误信息' in result.stderr
    assert not out.exists()


@pytest.mark.parametrize('overlap', [False, True])
def test_baseline_duplicate_failure_or_result_conflict_refused(tmp_path, overlap):
    baseline, manifest, batch = prepare(tmp_path)
    data = json.loads(baseline.read_text())
    if not overlap:
        data['results'] = []
    data['errors'] = [{'video_id': 'v1', 'error': '失败'}] * (1 if overlap else 2)
    baseline.write_text(json.dumps(data))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '重复或与 results 冲突' in result.stderr
    assert not out.exists()


@pytest.mark.parametrize('second_container', ['delivery_candidates', 'pending_review_candidates'])
def test_conflicting_candidate_titles_refused(tmp_path, second_container):
    baseline, manifest, batch = prepare(tmp_path)
    state = {'route': {'action': 'handoff', 'recommended_candidate_id': 'c1'},
             'candidates': [{'candidate_id': 'c1', 'rendered_title': '标题一'}],
             second_container: [{'candidate_id': 'c1', 'headline': '标题二'}]}
    with sqlite3.connect(batch / 'agent.db') as db:
        db.execute('update tasks set state_json=?', (json.dumps(state),))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '候选 ID 对应多个标题' in result.stderr
    assert not out.exists()


def test_same_candidate_title_across_containers_is_allowed():
    state = {'route': {'action': 'handoff', 'recommended_candidate_id': 'c1'},
             'candidates': [{'candidate_id': 'c1', 'rendered_title': '同一标题'}],
             'delivery_candidates': [{'candidate_id': 'c1', 'headline': '同一标题'}]}
    assert pick_delivery_candidate(state)[1] == 'recommended'


def test_missing_baseline_arm_is_refused(tmp_path):
    baseline, manifest, batch = prepare(tmp_path, baseline_ids=('v1',), current_ids=('v1', 'v2'))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '不是配对实验' in result.stderr
    assert not out.exists()


def test_complete_manifest_reports_actual_coverage(tmp_path):
    baseline, manifest, batch = prepare(tmp_path, baseline_ids=('v1', 'v2'), current_ids=('v1', 'v2'))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    note = json.loads((out / 'compare_note.json').read_text())
    assert note['comparability']['videos_covered_both_arms'] == 2
    assert note['comparability']['same_manifest'] is True


def test_model_mismatch_blocks_formal_comparison(tmp_path):
    baseline, manifest, batch = prepare(tmp_path, baseline_model='m-baseline', current_model='m-system')
    result = run(baseline, manifest, batch, tmp_path / 'new')
    assert result.returncode != 0
    assert '换实验条件' in result.stderr


def test_missing_model_info_is_refused(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    payload = json.loads(baseline.read_text())
    payload.pop('generation_model')
    baseline.write_text(json.dumps(payload))
    result = run(baseline, manifest, batch, tmp_path / 'new')
    assert result.returncode != 0
    assert '无法确认两臂模型' in result.stderr


def test_video_outside_manifest_is_refused(tmp_path):
    baseline, manifest, batch = prepare(tmp_path, baseline_ids=('v1', 'v9'), current_ids=('v1', 'v9'))
    data = json.loads(manifest.read_text())
    data['samples'] = [s for s in data['samples'] if s['video_id'] == 'v1']
    manifest.write_text(json.dumps(data))
    result = run(baseline, manifest, batch, tmp_path / 'new')
    assert result.returncode != 0
    assert 'manifest 之外' in result.stderr


def test_unfinished_system_task_cannot_be_a_formal_comparison(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    with sqlite3.connect(batch / 'agent.db') as db:
        db.execute("update tasks set status='generating', state_json=?", (json.dumps({'route': {'action': 'handoff', 'recommended_candidate_id': 'c1'}, 'candidates': [{'candidate_id': 'c1', 'headline': '旧标题'}]}),))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '尚未终态' in result.stderr
    assert not out.exists()


def test_empty_manifest_is_refused(tmp_path):
    baseline, manifest, batch = prepare(tmp_path, baseline_ids=(), current_ids=())
    manifest.write_text(json.dumps({'samples': []}))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode != 0
    assert '非空样本' in result.stderr
    assert not out.exists()


def test_technical_failure_never_delivers_stale_candidate(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    with sqlite3.connect(batch / 'agent.db') as db:
        db.execute("update tasks set status='technical_failed', state_json=?", (json.dumps({'route': {'action': 'handoff', 'recommended_candidate_id': 'c1'}, 'candidates': [{'candidate_id': 'c1', 'headline': '旧标题'}]}),))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    mapping = json.loads((out / 'review_mapping.json').read_text())
    assert [c['arm'] for c in mapping[0]['candidates']] == ['baseline']
    assert mapping[0]['selection'] == 'none'


def test_legacy_baseline_without_audit_status_is_diagnostic_only(tmp_path):
    """旧基线结果没有审核状态：标题仍可诊断，但不能当正式对照证据。"""
    baseline, manifest, batch = prepare(tmp_path)
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    note = json.loads((out / 'compare_note.json').read_text())
    comparability = note['comparability']
    assert comparability['baseline_audit_status_counts'] == {'legacy_unknown': 1}
    assert comparability['formal_comparison'] is False
    assert '缺少明确审核状态' in comparability['not_formal_reason']


def test_audited_baseline_is_formal_when_models_match(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    payload = json.loads(baseline.read_text())
    payload['results'][0]['audit_status'] = 'passed'
    baseline.write_text(json.dumps(payload))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    comparability = json.loads((out / 'compare_note.json').read_text())['comparability']
    assert comparability['baseline_audit_status_counts'] == {'passed': 1}
    assert comparability['formal_comparison'] is True
    assert comparability['not_formal_reason'] == ''


def test_rejected_baseline_title_is_not_offered_to_review(tmp_path):
    baseline, manifest, batch = prepare(tmp_path)
    payload = json.loads(baseline.read_text())
    payload['results'][0]['audit_status'] = 'rejected'
    baseline.write_text(json.dumps(payload))
    out = tmp_path / 'new'
    result = run(baseline, manifest, batch, out)
    assert result.returncode == 0, result.stderr
    mapping = json.loads((out / 'review_mapping.json').read_text())
    # 审核拒绝的标题不进评审面：两臂都没有可评候选，样本仍保留在 incomplete_pairs。
    assert mapping[0]['candidates'] == []
    note = json.loads((out / 'compare_note.json').read_text())
    assert len(note['incomplete_pairs']) == 1


def test_hash_is_exactly_review_hash():
    assert title_hash(' 标题 ') == review_hash(' 标题 ')


def test_single_arm_report_refuses_blind_comparison(tmp_path):
    from scripts.review_quality_report import load
    import pytest
    (tmp_path / 'review_mapping.json').write_text(json.dumps([{'task_id': 't', 'candidates': [{'arm': 'baseline'}]}]))
    with pytest.raises(ValueError, match='对照盲评'):
        load(tmp_path)
