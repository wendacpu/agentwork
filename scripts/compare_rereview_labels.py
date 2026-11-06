#!/usr/bin/env python3
"""离线比较人工反馈与隔离重评；不调用模型，也不写入任何数据库。

用法：python scripts/compare_rereview_labels.py FEEDBACK_DB ISOLATED_DB_OR_DIR [...]
目录递归发现 SQLite 数据库；输出 JSON 到标准输出。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def identity(task, candidate, title_hash):
    return (str(task or ''), str(candidate or ''), str(title_hash or ''))


def key_dict(key):
    return dict(zip(('task_id', 'candidate_id', 'title_sha256'), key))


def valid_key(key):
    return bool(key[0] and key[1] and len(key[2]) == 64 and all(c in '0123456789abcdef' for c in key[2]))


def feedback_rows(path):
    rows = defaultdict(list)
    diagnostics = []
    invalid = []
    with closing(connect(path)) as conn:
        for batch, task, candidate, title_hash, raw in conn.execute(
                'SELECT batch_id, task_id, candidate_id, title_hash, payload_json FROM candidate_decisions_v2'):
            key = identity(task, candidate, title_hash)
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('payload 不是对象')
                if not valid_key(key):
                    raise ValueError('身份字段或标题哈希无效')
                for field, expected in [('task_id', task), ('candidate_id', candidate), ('title_hash', title_hash)]:
                    if field in payload and payload[field] != expected:
                        raise ValueError(f'payload 的 {field} 与行不一致')
                if 'original_headline' in payload and (not isinstance(payload['original_headline'], str)
                                                       or digest(payload['original_headline']) != title_hash):
                    raise ValueError('原始标题与哈希不一致')
            except (ValueError, TypeError, KeyError) as exc:
                invalid.append({**key_dict(key), 'batch_id': batch, 'reason': str(exc)})
                continue
            item = {**key_dict(key), 'batch_id': batch, 'human_decision': payload.get('decision'),
                    'scope': payload.get('scope'), 'reason': payload.get('reason')}
            if payload.get('scope') == 'diagnostic':
                diagnostics.append(item)
            elif payload.get('scope') in ('frozen', None, ''):
                rows[key].append(item)
            else:
                invalid.append({**item, 'reason': '未知反馈口径'})
    return rows, diagnostics, invalid


def discover(paths):
    found = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            found.add(path.resolve())
        elif path.is_dir():
            for p in path.rglob('*.db'):
                if p.is_file():
                    found.add(p.resolve())
        else:
            raise ValueError(f'路径不存在: {path}')
    return sorted(found)


def isolated_rows(paths):
    rows = defaultdict(list)
    problems = []
    skipped = []
    db_count = 0
    explicit = {Path(p).resolve() for p in paths if Path(p).is_file()}
    for path in discover(paths):
        try:
            with closing(connect(path)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'rereview_snapshot', 'tasks'} <= tables:
                    if path in explicit:
                        problems.append({'database': str(path), 'reason': '不是隔离重评库：缺少快照或任务表'})
                    else:
                        skipped.append(str(path))
                    continue
                db_count += 1
                for task, raw in conn.execute('SELECT task_id, candidates_json FROM rereview_snapshot'):
                    try:
                        frozen = json.loads(raw)
                        if not isinstance(frozen, list):
                            raise ValueError('快照不是数组')
                        task_row = conn.execute('SELECT state_json, status FROM tasks WHERE task_id=?', (task,)).fetchone()
                        if not task_row:
                            raise ValueError('快照对应任务不存在')
                        state = json.loads(task_row[0])
                        if not isinstance(state, dict):
                            raise ValueError('任务状态不是对象')
                    except (ValueError, TypeError) as exc:
                        problems.append({'database': str(path), 'task_id': task, 'reason': str(exc)})
                        continue
                    for entry in frozen:
                        if not isinstance(entry, dict):
                            problems.append({'database': str(path), 'task_id': task, 'reason': '快照候选不是对象'})
                            continue
                        key = identity(task, entry.get('candidate_id'), entry.get('title_sha256'))
                        if not valid_key(key):
                            problems.append({'database': str(path), **key_dict(key), 'reason': '快照身份无效或缺少标题哈希'})
                            continue
                        candidates = [c for c in (state.get('candidates') or []) if isinstance(c, dict)
                                      and str(c.get('candidate_id') or '') == key[1]
                                      and isinstance(c.get('rendered_title') or c.get('headline'), str)
                                      and digest(c.get('rendered_title') or c.get('headline')) == key[2]]
                        decisions = [d for d in (state.get('candidate_decisions') or []) if isinstance(d, dict)
                                     and str(d.get('candidate_id') or '') == key[1]]
                        reason = None
                        if len(candidates) != 1:
                            reason = '实际候选与冻结标题哈希不唯一或不匹配'
                        elif len(decisions) != 1:
                            reason = '重评候选判定缺失或不唯一'
                        elif not decisions[0].get('status'):
                            reason = '重评候选状态缺失'
                        item = {**key_dict(key), 'database': str(path), 'task_status': task_row[1],
                                'system_status': decisions[0].get('status') if len(decisions) == 1 else None,
                                'system_reason': decisions[0].get('reason') if len(decisions) == 1 else None,
                                'route': state.get('route'), 'problem': reason}
                        rows[key].append(item)
        except (sqlite3.Error, OSError) as exc:
            problems.append({'database': str(path), 'reason': str(exc)})
    return rows, problems, skipped, db_count


def compare(feedback, paths):
    human, diagnostic, invalid = feedback_rows(feedback)
    machine, problems, skipped, count = isolated_rows(paths)
    paired, missing_rereview, missing_feedback, ambiguous, incomplete = [], [], [], [], []
    for key in sorted(set(human) | set(machine)):
        hs, ms = human.get(key, []), machine.get(key, [])
        if len(hs) > 1 or len(ms) > 1:
            ambiguous.append({**key_dict(key), 'feedback': hs, 'rereviews': ms})
        elif not ms:
            missing_rereview.append(hs[0])
        elif not hs:
            missing_feedback.append(ms[0])
        elif ms[0]['problem'] or not hs[0]['human_decision']:
            incomplete.append({'feedback': hs[0], 'rereview': ms[0],
                               'reason': ms[0]['problem'] or '人工判定缺失'})
        else:
            paired.append({'feedback': hs[0], 'rereview': ms[0]})
    matrix = Counter((p['feedback']['human_decision'], p['rereview']['system_status']) for p in paired)
    return {
        'method': {'identity': 'task_id + candidate_id + 完整展示标题 UTF-8 SHA-256；快照和实际标题均须验证',
                   'population': '仅 frozen 人工判定（旧数据无 scope 视为 frozen）；diagnostic 单列，不进比较',
                   'matrix': '人工 decision × 重评 candidate_decisions.status；不是准确率，不将不同标签强行映射',
                   'duplicates': '任一侧同身份多条即歧义，不选择批次或最新记录',
                   'read_only': True},
        'summary': {'isolated_databases': count, 'paired': len(paired), 'missing_rereview': len(missing_rereview),
                    'missing_feedback': len(missing_feedback), 'ambiguous': len(ambiguous),
                    'incomplete': len(incomplete), 'diagnostic': len(diagnostic),
                    'invalid_feedback': len(invalid), 'database_problems': len(problems)},
        'matrix': [{'human_decision': h, 'system_status': s, 'count': n} for (h, s), n in sorted(matrix.items())],
        'paired': paired, 'missing_rereview': missing_rereview, 'missing_feedback': missing_feedback,
        'ambiguous': ambiguous, 'incomplete': incomplete, 'diagnostic': diagnostic,
        'invalid_feedback': invalid, 'database_problems': problems, 'skipped_non_isolated': skipped,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feedback_db', help='源 human_feedback_v2.db')
    parser.add_argument('isolated', nargs='+', help='隔离数据库或递归搜索目录')
    args = parser.parse_args(argv)
    try:
        report = compare(args.feedback_db, args.isolated)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
