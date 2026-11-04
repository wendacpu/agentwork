"""旧版不具备身份及未评状态的工具不得输出正式比较。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_incomplete_legacy_labels_cannot_produce_report(tmp_path):
    baseline = tmp_path / 'baseline.json'
    current = tmp_path / 'current.json'
    manifest = tmp_path / 'manifest.json'
    output = tmp_path / 'report.md'
    baseline.write_text(json.dumps({'labels': [{'video_id': 'v1', 'title': '标题', 'verdict': '可用'}]}))
    current.write_text(json.dumps({'labels': []}))
    manifest.write_text(json.dumps({'samples': [{'video_id': 'v1'}, {'video_id': 'v2'}]}))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/eval_compare.py'), '--baseline-labels', str(baseline), '--current-labels', str(current), '--manifest', str(manifest), '--output', str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert '禁止生成正式配对结论' in result.stderr
    assert not output.exists()


def test_legacy_finalize_refuses_before_model_calls():
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/run_eval.py'), '--stage', 'finalize'], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode != 0
    assert '已停用' in result.stderr
    assert 'model_review.py' not in result.stdout
