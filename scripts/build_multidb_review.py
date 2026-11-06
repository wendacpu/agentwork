#!/usr/bin/env python3
"""从多个 SQLite 任务库建立独立评审快照；不调用模型、不修改源库。

用法：python3 scripts/build_multidb_review.py --db 批次1/agent.db --db 批次2/agent.db --out 新目录
输出目录必须不存在。应等源任务停止写入后再执行；各库不是跨库原子快照。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def digest(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def build(databases: list[Path], out: Path, manifest: Path | None = None) -> dict:
    manifest = manifest.resolve(strict=True) if manifest else None
    entries = []
    if manifest:
        entries = json.loads(manifest.read_text(encoding='utf-8')).get('tasks', [])
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError('manifest.tasks 必须是对象数组')
    explicit = [p.resolve(strict=True) for p in databases]
    if len(set(explicit)) != len(explicit):
        raise ValueError('不得重复指定源数据库')

    def manifest_db(entry):
        raw = entry.get('result_db')
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute() and not path.exists():
            path = manifest.parent / path
        return path.resolve()

    sources = list(explicit)
    for entry in entries:
        path = manifest_db(entry)
        if path and path.is_file() and path not in sources:
            sources.append(path)
    if not sources and not entries:
        raise ValueError('必须指定源数据库或非空 manifest.tasks')
    out = out.resolve()
    if out.exists():
        raise ValueError('输出目录已存在，拒绝覆盖')
    for source in sources:
        if not source.is_file() or out == source.parent or out in source.parents:
            raise ValueError('源库无效或输出目录包含源库')
    # 所有读取与验证先完成；源库使用 SQLite 只读事务，包含 WAL 中已提交数据。
    rows, provenance, warnings = [], [], []
    for number, source in enumerate(sources, 1):
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            conn.row_factory = sqlite3.Row
            task_rows = conn.execute('SELECT task_id, video_id, status, config_json, state_json, created_at, updated_at FROM tasks ORDER BY created_at, task_id').fetchall()
            has_pool = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates_by_task'").fetchone()
            for row in task_rows:
                data = dict(row)
                state = json.loads(data['state_json'] or '{}')
                if not isinstance(state, dict):
                    raise ValueError(f'任务状态不是对象: {source} / {data["task_id"]}')
                pool = []
                for field in ('delivery_candidates', 'candidates', 'pending_review_candidates'):
                    pool.extend(state.get(field) or [])
                extra = []
                if has_pool:
                    extra = [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM candidates_by_task WHERE task_id=? ORDER BY candidate_id', (data['task_id'],))]
                # 现有评审器冻结匹配只读状态数组，因此复制任务专属表候选到快照状态。
                state['candidates'] = list(state.get('candidates') or []) + extra
                pool.extend(extra)
                candidates, seen = [], set()
                decisions = {str(d.get('candidate_id') or ''): d for d in state.get('candidate_decisions') or [] if isinstance(d, dict)}
                route = state.get('route') or {}
                for c in pool:
                    if not isinstance(c, dict):
                        warnings.append(f'{source.name}/{data["task_id"]}: 非对象候选')
                        continue
                    cid = str(c.get('candidate_id') or '')
                    title = str(c.get('rendered_title') or c.get('headline') or '')
                    if not cid or not title:
                        warnings.append(f'{source.name}/{data["task_id"]}: 候选缺 id 或标题，保留在状态但无法冻结')
                        continue
                    key = (cid, digest(title))
                    if key in seen:
                        continue
                    seen.add(key)
                    status = 'recommended' if route.get('recommended_candidate_id') == cid else str((decisions.get(cid) or {}).get('status') or 'unknown')
                    candidates.append({'candidate_id': cid, 'title_sha256': key[1], 'status': status, 'slot_id': str(c.get('slot_id') or '')})
                original_id = data['task_id']
                new_id = f's{number:03d}:{original_id}'
                data['task_id'] = new_id
                data['state_json'] = json.dumps(state, ensure_ascii=False)
                rows.append((data, candidates))
                provenance.append({'task_id': new_id, 'source_db': str(source), 'source_task_id': original_id})
    matched = {(p['source_db'], str(p['source_task_id'])): p['task_id'] for p in provenance}
    by_id = {r['task_id']: r for r, _ in rows}
    for i, entry in enumerate(entries, 1):
        original = str(entry.get('task_id') or '')
        video = str(entry.get('video_id') or '')
        if not original or not video:
            raise ValueError(f'manifest 第 {i} 条缺少真实 task_id 或 video_id')
        source = manifest_db(entry)
        key = (str(source), original) if source else None
        existing = matched.get(key)
        if existing:
            if str(by_id[existing]['video_id']) != video:
                raise ValueError(f'manifest 与数据库 video_id 不一致: {original}')
            continue
        new_id = f'm{i:06d}:{original}'
        status = str(entry.get('status') or 'unknown')
        state = {'video': {'video_id': video}, 'candidates': [], 'route': {},
                 'error': 'manifest 记录的任务在结果库中不存在或结果库不可用',
                 'manifest_entry': entry}
        row = {'task_id': new_id, 'video_id': video, 'status': status, 'config_json': '{}',
               'state_json': json.dumps(state, ensure_ascii=False), 'created_at': '', 'updated_at': ''}
        rows.append((row, []))
        provenance.append({'task_id': new_id, 'source_db': str(source) if source else None,
                           'source_task_id': original, 'placeholder': True, 'manifest_status': status})
        warnings.append(f'manifest 任务 {original}: 无可用结果记录，保留空候选占位')
    out.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(out / 'agent.db')
    try:
        db.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)')
        db.executemany('INSERT INTO tasks VALUES (:task_id,:video_id,:status,:config_json,:state_json,:created_at,:updated_at)', (r for r, _ in rows))
        db.commit()
    finally:
        db.close()
    mapping = []
    for i, (row, candidates) in enumerate(rows, 1):
        state = json.loads(row['state_json'])
        video = state.get('video') or {}
        mapping.append({'blind_id': f'R{i:03d}', 'index': i, 'task_id': row['task_id'], 'video_id': row['video_id'], 'category': '', 'source_title': video.get('title') or '', 'task_status': row['status'], 'route': state.get('route') or {}, 'candidates': candidates})
    (out / 'review_mapping.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'SOURCE.json').write_text(json.dumps({'sources': [str(p) for p in sources], 'tasks': provenance, 'warnings': warnings}, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'out': str(out), 'tasks': len(rows), 'candidates': sum(len(c) for _, c in rows), 'warnings': warnings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', default=[], help='源任务库，可重复')
    parser.add_argument('--manifest', help='批次清单，读取 tasks 的 result_db、status、task_id、video_id')
    parser.add_argument('--out', required=True, help='不存在的新评审目录')
    args = parser.parse_args()
    print(json.dumps(build([Path(p) for p in args.db], Path(args.out), Path(args.manifest) if args.manifest else None), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
