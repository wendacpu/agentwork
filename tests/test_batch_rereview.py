"""批量重评只用离线数据和模拟调用，不访问付费服务。"""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent_system.pipeline import Pipeline
from agent_system.storage import Storage
from scripts import batch_rereview
from test_safety_contracts import _rereview_state, _review_payload


def source_db(tmp_path):
    path = tmp_path / 'source.db'
    storage = Storage(path)
    ids = []
    for _ in range(2):
        task = storage.create_task('v', {})
        storage.set_state(task, 'pending_human', _rereview_state())
        ids.append(task)
    storage.conn.close()
    return path, ids


def offline_pipeline(config, dataset, storage):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config, pipeline.storage = config, storage
    pipeline.dataset = None
    pipeline.review_adapter = None
    pipeline._call = lambda *args, **kwargs: {'reviews': [_review_payload('a1')]}
    return pipeline


def test_dry_run_freezes_candidates_without_writes_or_model(tmp_path, capsys, monkeypatch):
    source, ids = source_db(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(batch_rereview, 'make_pipeline', lambda *a: pytest.fail('dry-run called pipeline'))
    output = tmp_path / 'batch'
    assert batch_rereview.main(['--db', str(source), '--output', str(output), '--dry-run', '--task-id', ids[0]]) == 0
    report = json.loads(capsys.readouterr().out)
    candidate = report['tasks'][0]['candidates'][0]
    assert candidate['candidate_id'] == 'a1'
    assert candidate['headline_sha256'] == hashlib.sha256('小店开业'.encode()).hexdigest()
    assert report['tasks'][0]['task_id'] == ids[0]
    assert not output.exists() and source.read_bytes() == before


def test_execute_real_rereview_isolated_and_source_unchanged(tmp_path, monkeypatch):
    source, ids = source_db(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(batch_rereview, 'make_pipeline', offline_pipeline)
    output = tmp_path / 'batch'
    assert batch_rereview.main(['--db', str(source), '--output', str(output), '--execute', '--mock']) == 0
    manifest = json.loads((output / 'manifest.json').read_text())
    assert len(manifest['tasks']) == 2
    assert source.read_bytes() == before
    paths = []
    for item in manifest['tasks']:
        assert item['status'] == 'completed'
        db = Path(item['result_db'])
        assert db.is_relative_to(output / 'rereviews')
        paths.append(db)
        with sqlite3.connect(db) as conn:
            task_id, frozen = conn.execute('SELECT task_id,candidates_json FROM rereview_snapshot').fetchone()
            assert task_id == item['task_id']
            assert json.loads(frozen) == item['candidates']
    assert len(set(paths)) == len(ids)


def test_failures_are_recorded_and_other_tasks_continue(tmp_path, monkeypatch):
    source, ids = source_db(tmp_path)
    def factory(config, dataset, storage):
        pipeline = offline_pipeline(config, dataset, storage)
        actual = pipeline.rereview
        def review(task_id):
            if task_id == ids[0]:
                raise RuntimeError('offline failure')
            return actual(task_id)
        pipeline.rereview = review
        return pipeline
    monkeypatch.setattr(batch_rereview, 'make_pipeline', factory)
    output = tmp_path / 'batch'
    assert batch_rereview.main(['--db', str(source), '--output', str(output), '--execute', '--mock', '--task-id', ids[0], '--task-id', 'missing', '--task-id', ids[1]]) == 1
    items = json.loads((output / 'manifest.json').read_text())['tasks']
    assert [x['status'] for x in items] == ['failed', 'failed', 'completed']
    assert items[0]['error'] == 'offline failure' and items[1]['error_type'] == 'KeyError'


def test_guards_paid_output_and_limit(tmp_path, monkeypatch, capsys):
    source, ids = source_db(tmp_path)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'model_provider': 'deepseek'}))
    output = tmp_path / 'batch'
    monkeypatch.delenv('AGENT_MODEL_PROVIDER', raising=False)
    with pytest.raises(SystemExit):
        batch_rereview.main(['--db', str(source), '--output', str(output), '--config', str(config), '--execute'])
    assert not output.exists()
    assert batch_rereview.main(['--db', str(source), '--output', str(output), '--limit', '1']) == 0
    assert len(json.loads(capsys.readouterr().out)['tasks']) == 1
    with pytest.raises(SystemExit):
        batch_rereview.main(['--db', str(source), '--output', str(tmp_path)])
