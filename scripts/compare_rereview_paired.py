#!/usr/bin/env python3
"""结束后按冻结身份对照旧版与重评；仅输出 JSON，不写库、不扫描目录。

用法：python scripts/compare_rereview_paired.py --manifest 已结束批次manifest.json
可通过参数指定历史库和补评清单；必须确保重评进程已经结束。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

try:
    from .compare_rereview_labels import connect, digest, feedback_rows, identity, isolated_rows, key_dict, valid_key
    from .rereview_metrics import HUMAN, POSITIVE, NEGATIVE, ratio
except ImportError:
    from compare_rereview_labels import connect, digest, feedback_rows, identity, isolated_rows, key_dict, valid_key
    from rereview_metrics import HUMAN, POSITIVE, NEGATIVE, ratio

ROOT = Path(__file__).resolve().parents[1]


def supplement(db, manifest):
    """清单定义展示全集；反馈键与标题哈希均须精确匹配。"""
    tasks = json.loads(Path(manifest).read_text(encoding='utf-8'))
    if not isinstance(tasks, list):
        raise ValueError('补评清单不是数组')
    expected = defaultdict(list)
    for task in tasks:
        for candidate in task.get('candidates', []):
            title = candidate.get('title')
            key = identity(task.get('task_id'), candidate.get('candidate_id'), candidate.get('hash'))
            if not valid_key(key) or not isinstance(title, str) or digest(title) != key[2]:
                raise ValueError(f'补评清单身份或标题哈希无效：{key}')
            expected[key].append({'scope': 'supplement', 'human_decision': None})
    with closing(connect(db)) as conn:
        for raw, decision, reason in conn.execute('SELECT key, decision, reason FROM decisions'):
            parts = raw.split('|') if isinstance(raw, str) else []
            key = tuple(parts)
            if len(key) != 3 or not valid_key(key):
                raise ValueError(f'补评键无效：{raw!r}')
            if key not in expected:
                expected[key].append({'scope': 'supplement', 'human_decision': decision,
                                      'reason': reason, 'problem': '反馈不在补评清单'})
            else:
                for item in expected[key]:
                    item.update(human_decision=decision, reason=reason)
    return expected


def old_rows(path):
    rows = defaultdict(list)
    with closing(connect(path)) as conn:
        for task, raw in conn.execute('SELECT task_id, state_json FROM tasks'):
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError(f'旧任务状态无效：{task}')
            decisions = defaultdict(list)
            for d in state.get('candidate_decisions') or []:
                if isinstance(d, dict):
                    decisions[str(d.get('candidate_id') or '')].append(d)
            for c in state.get('candidates') or []:
                if not isinstance(c, dict):
                    continue
                title = c.get('rendered_title') or c.get('headline')
                if not isinstance(title, str):
                    continue
                key = identity(task, c.get('candidate_id'), digest(title))
                if not valid_key(key):
                    continue
                ds = decisions[key[1]]
                rows[key].append({'system_status': ds[0].get('status') if len(ds) == 1 else None,
                                  'problem': None if len(ds) == 1 else '旧候选决策缺失或不唯一'})
    return rows


def finished_manifest(path):
    path = Path(path).resolve()
    report = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(report, dict) or report.get('dry_run') is not False or not isinstance(report.get('tasks'), list):
        raise ValueError('需要实际执行的重评 manifest')
    tasks = report['tasks']
    if any(not isinstance(t, dict) or t.get('status') not in ('completed', 'failed') for t in tasks):
        raise ValueError('manifest 存在未结束任务，禁止读取重评库')
    if len({t.get('task_id') for t in tasks}) != len(tasks) or any(not t.get('task_id') for t in tasks):
        raise ValueError('manifest 任务身份缺失或重复')
    expected, paths, failed = {}, [], []
    for t in tasks:
        if t['status'] == 'failed':
            failed.append(t)
            continue
        if not t.get('result_db') or not isinstance(t.get('candidates'), list):
            raise ValueError('完成任务缺少数据库或候选快照')
        p = Path(t['result_db'])
        p = (path.parent / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_file() or p in paths:
            raise ValueError(f'重评数据库不存在或重复：{p}')
        paths.append(p)
        keys = []
        for c in t['candidates']:
            key = identity(t['task_id'], c.get('candidate_id'), c.get('title_sha256'))
            if not valid_key(key):
                raise ValueError('manifest 候选身份无效')
            keys.append(key)
        if len(set(keys)) != len(keys):
            raise ValueError('manifest 候选重复')
        expected[str(p)] = set(keys)
    return paths, expected, failed


def matrix(pairs):
    pending = Counter()
    eligible = []
    excluded = []
    for pair in pairs:
        old, new, human = pair['old']['system_status'], pair['new']['system_status'], pair['human']['human_decision']
        if human not in HUMAN:
            excluded.append({**key_dict(pair['key']), 'reason': '人工判定缺失或未知'})
        elif old == 'needs_review' or new == 'needs_review':
            pending[(old == 'needs_review', new == 'needs_review')] += 1
        elif old not in POSITIVE | NEGATIVE or new not in POSITIVE | NEGATIVE:
            excluded.append({**key_dict(pair['key']), 'reason': '旧版或新版状态未知'})
        else:
            eligible.append(pair)
    def score(which):
        cells = Counter()
        for pair in eligible:
            actual = pair['human']['human_decision'] == 'approved'
            predicted = pair[which]['system_status'] in POSITIVE
            cells[('tp' if actual else 'fp') if predicted else ('fn' if actual else 'tn')] += 1
        tp, fp, fn, tn = (cells[k] for k in ('tp', 'fp', 'fn', 'tn'))
        return {'confusion': dict(tp=tp, fp=fp, fn=fn, tn=tn),
                'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn)}
    return {'same_intersection': len(eligible), 'old': score('old'), 'new': score('new'),
            'pending': {'old_only': pending[(True, False)], 'new_only': pending[(False, True)],
                        'both': pending[(True, True)]}, 'excluded': excluded,
            'pairs': [{**key_dict(p['key']), 'human': p['human']['human_decision'],
                       'old_status': p['old']['system_status'], 'new_status': p['new']['system_status']}
                      for p in eligible]}


def compare_paired(feedback, supplement_db, supplement_manifest, old_db, manifest):
    paths, expected, failed = finished_manifest(manifest)
    frozen, diagnostic, invalid = feedback_rows(feedback)
    supp = supplement(supplement_db, supplement_manifest)
    old = old_rows(old_db)
    new, problems, _, count = isolated_rows(paths)
    actual = defaultdict(set)
    for key, items in new.items():
        for item in items:
            actual[item['database']].add(key)
    for path, keys in expected.items():
        if actual[path] != keys:
            problems.append({'database': path, 'reason': '重评快照与 manifest 候选不一致',
                             'missing': [key_dict(k) for k in sorted(keys - actual[path])],
                             'unexpected': [key_dict(k) for k in sorted(actual[path] - keys)]})
    bad_dbs = {p['database'] for p in problems}
    groups = {'frozen': [], 'supplement': []}
    exclusions = []
    for key in sorted(set(frozen) | set(supp)):
        hs = frozen.get(key, []) + supp.get(key, [])
        os, ns = old.get(key, []), new.get(key, [])
        reasons = []
        if len(hs) != 1:
            reasons.append('人工反馈重复或跨来源冲突')
        if not os:
            reasons.append('缺旧候选或标题哈希不匹配')
        elif len(os) != 1 or os[0]['problem']:
            reasons.append('旧候选或决策不唯一/缺失')
        if not ns:
            reasons.append('缺重评或标题哈希不匹配')
        elif len(ns) != 1 or ns[0]['problem'] or ns[0]['database'] in bad_dbs:
            reasons.append('重评不唯一、无效或数据库与清单不一致')
        if any(not h.get('human_decision') or h.get('problem') for h in hs):
            reasons.append('缺人工判定或不在补评清单')
        if reasons:
            exclusions.append({**key_dict(key), 'reasons': reasons, 'human_records': hs})
            continue
        group = 'frozen' if key in frozen else 'supplement'
        groups[group].append({'key': key, 'human': hs[0], 'old': os[0], 'new': ns[0]})
    return {'method': {'identity': '任务ID + 候选ID + 展示标题UTF-8 SHA256；验证快照及实际展示标题',
                       'population': 'frozen主指标；补评为扩展诊断，合计有选择偏差',
                       'human_positive': 'approved；marginal与rejected为负例',
                       'system_positive': sorted(POSITIVE), 'system_negative': sorted(NEGATIVE),
                       'intersection': '两版同一人工已判断、双方均非待核且状态明确的候选',
                       'read_only': True, 'limitation': '不证明因果或总体效果；结果依赖人工口径和选择样本'},
            'coverage': {'frozen_identities': len(frozen), 'supplement_identities': len(supp),
                         'paired_frozen': len(groups['frozen']), 'paired_supplement': len(groups['supplement']),
                         'unmatched_or_conflicting': len(exclusions), 'isolated_databases': count,
                         'failed_tasks': failed},
            'frozen': matrix(groups['frozen']), 'supplement_diagnostic': matrix(groups['supplement']),
            'combined_selected_biased': matrix(groups['frozen'] + groups['supplement']),
            'exclusions': exclusions,
            'new_without_human': [key_dict(k) for k in sorted(set(new) - set(frozen) - set(supp))],
            'invalid_feedback': invalid, 'historical_diagnostic': diagnostic,
            'database_problems': problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, help='已结束的重评 manifest；不扫描目录')
    parser.add_argument('--feedback-db', default=str(ROOT / 'runtime/cost_ab_20261105/human_feedback_v2.db'))
    parser.add_argument('--supplement-db', default=str(ROOT / 'runtime/cost_ab_supplement_review/supplement_feedback.db'))
    parser.add_argument('--supplement-manifest', default=str(ROOT / 'runtime/cost_ab_supplement_review/supplement_manifest.json'))
    parser.add_argument('--old-db', default=str(ROOT / 'runtime/cost_ab_20261105/agent.db'))
    args = parser.parse_args(argv)
    try:
        result = compare_paired(args.feedback_db, args.supplement_db, args.supplement_manifest, args.old_db, args.manifest)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
