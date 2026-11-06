#!/usr/bin/env python3
"""重评结束后构建隔离评审快照；不调用模型，不修改运行批次或源库。

默认严格核验 93 个冻结候选。仅接受全部 completed/failed 的最终 manifest。
输出是新目录中的 snapshot.json，供独立评审服务使用；不写反馈。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_row(db, task_id):
    if not db.is_file():
        raise ValueError(f'数据库不存在: {db}')
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute('SELECT video_id,status,state_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError(f'任务不存在: {task_id}')
    return row[0], row[1], json.loads(row[2])


def check_candidates(frozen, state):
    pool = state.get('candidates')
    if not isinstance(pool, list):
        raise ValueError('候选列表不存在')
    if len(pool) != len(frozen):
        raise ValueError('候选数量偏移')
    found = {}
    for c in pool:
        cid = c.get('candidate_id')
        if not cid or cid in found:
            raise ValueError('候选身份缺失或重复')
        found[cid] = c
    selected = []
    for f in frozen:
        c = found.get(f.get('candidate_id'))
        if c is None:
            raise ValueError('冻结候选身份不匹配')
        headline = c.get('headline') or ''
        title = c.get('rendered_title') or headline
        if sha(headline) != f.get('headline_sha256') or sha(title) != f.get('title_sha256'):
            raise ValueError('冻结候选标题哈希不匹配')
        selected.append(c)
    if len({f.get('candidate_id') for f in frozen}) != len(frozen):
        raise ValueError('冻结候选身份重复')
    return selected


def build(manifest_path: Path, out: Path, expected_candidates=93):
    manifest_path = manifest_path.resolve(strict=True)
    out = out.resolve()
    batch = manifest_path.parent
    if out.exists() or out == batch or batch in out.parents or out in batch.parents:
        raise ValueError('输出必须为独立且不存在的目录')
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest.get('tasks')
    if not isinstance(entries, list) or not entries:
        raise ValueError('manifest 缺少任务')
    if any(not isinstance(e, dict) or e.get('status') not in ('completed', 'failed') for e in entries):
        raise ValueError('批次尚未全部结束，拒绝构建')
    ids = [e.get('task_id') for e in entries]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('任务身份缺失或重复')
    if any(not isinstance(e.get('candidates'), list) or not e['candidates'] for e in entries):
        raise ValueError('冻结候选缺失')
    if sum(len(e['candidates']) for e in entries) != expected_candidates:
        raise ValueError(f'冻结候选数量不是 {expected_candidates}')
    source = Path(manifest['source_database_path']).resolve(strict=True)
    if out == source.parent or out in source.parents or source.parent in out.parents:
        raise ValueError('输出不得包含或位于源库目录')
    tasks = []
    for e in entries:
        tid, vid = e['task_id'], e.get('video_id')
        original_video_id, original_status, original = load_row(source, tid)
        if not vid or original_video_id != vid or (original.get('video') or {}).get('video_id') != vid:
            raise ValueError(f'原视频身份不匹配: {tid}')
        old_candidates = check_candidates(e['candidates'], original)
        selected, state = old_candidates, original
        if e['status'] == 'completed':
            raw = e.get('result_db')
            if not raw:
                raise ValueError(f'成功任务缺少结果库: {tid}')
            result = Path(raw)
            if not result.is_absolute():
                result = batch / result
            result = result.resolve(strict=True)
            if batch / 'rereviews' not in result.parents or result.name != 'agent.db':
                raise ValueError('结果库不属于本批次隔离目录')
            result_video_id, _, state = load_row(result, tid)
            iso = state.get('rereview_isolation') or {}
            video_sha = sha(json.dumps(original['video'], sort_keys=True, ensure_ascii=False))
            if (result_video_id != vid or iso.get('task_id') != tid
                    or Path(iso.get('source_database_path') or '/').resolve() != source
                    or iso.get('video_snapshot_sha256') != video_sha
                    or state.get('video') != original['video']
                    or iso.get('frozen_candidates') != e['candidates']):
                raise ValueError(f'结果库来源或冻结快照不匹配: {tid}')
            selected = check_candidates(e['candidates'], state)
        route = state.get('route') or {}
        decisions = {d.get('candidate_id'): d for d in state.get('candidate_decisions') or []}
        candidates = []
        for frozen, c in zip(e['candidates'], selected):
            cid = c['candidate_id']
            candidates.append({'candidate': c, 'title_hash': frozen['title_sha256'],
                               'headline_hash': frozen['headline_sha256'], 'decision': decisions.get(cid),
                               'recommended': route.get('recommended_candidate_id') == cid})
        tasks.append({'task_id': tid, 'video_id': vid, 'status': e['status'],
                      'conclusion_origin': '本次重评' if e['status'] == 'completed' else '原系统历史结论；本次无新结论',
                      'error': e.get('error') if e['status'] == 'failed' else None,
                      'error_type': e.get('error_type') if e['status'] == 'failed' else None,
                      'original_status': original_status, 'video': original['video'],
                      'input_quality': state.get('input_quality'),
                      'source_evidence_usage': state.get('source_evidence_usage'),
                      'route': route, 'delivery_slots': state.get('delivery_slots') or {},
                      'candidates': candidates})
    # 防止验证时 manifest 更新；运行批次不能作为一致快照。
    if manifest_path.read_bytes() != manifest_bytes:
        raise ValueError('manifest 在读取期间变化')
    out.mkdir(parents=True, exist_ok=False)
    snapshot = {'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                'task_count': len(tasks), 'candidate_count': expected_candidates, 'tasks': tasks}
    (out / 'snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    result = build(Path(args.manifest), Path(args.out))
    print(json.dumps({'task_count': result['task_count'], 'candidate_count': result['candidate_count']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
