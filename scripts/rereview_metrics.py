#!/usr/bin/env python3
"""离线计算隔离重评与人工判定的候选级指标，不调用模型、不写数据库。

python scripts/rereview_metrics.py FEEDBACK_DB ISOLATED_DB_OR_DIR [...] [--loose]
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import sqlite3
import sys

try:
    from .compare_rereview_labels import compare
except ImportError:  # 直接运行脚本
    from compare_rereview_labels import compare

POSITIVE = {'ready_for_human_review'}
NEGATIVE = {'rewrite', 'reject'}
HUMAN = {'approved', 'marginal', 'rejected'}


def ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def score(report, loose=False):
    """仅计算精确配对且双方标签明确的二分类；待核及其他状态单列。"""
    cells = Counter()
    pending = Counter()
    unknown = []
    for pair in report['paired']:
        human = pair['feedback']['human_decision']
        status = pair['rereview']['system_status']
        if human not in HUMAN:
            unknown.append({'pair': pair, 'reason': '未知人工判定'})
            continue
        if status == 'needs_review':
            pending[human] += 1
            continue
        if status not in POSITIVE | NEGATIVE:
            unknown.append({'pair': pair, 'reason': '未知系统状态'})
            continue
        actual = human == 'approved' or (loose and human == 'marginal')
        predicted = status in POSITIVE
        cells[('tp' if actual else 'fp') if predicted else ('fn' if actual else 'tn')] += 1
    tp, fp, fn, tn = (cells[k] for k in ('tp', 'fp', 'fn', 'tn'))
    return {
        'positive_human': 'approved + marginal' if loose else 'approved',
        'confusion': {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn},
        'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn),
        'needs_review': {'total': sum(pending.values()), 'by_human': dict(sorted(pending.items()))},
        'unknown': unknown,
    }


def metrics(report, include_loose=False):
    result = {
        'method': {'population': '冻结口径人工反馈与隔离重评精确配对的候选',
                   'identity': report['method']['identity'],
                   'system_positive': sorted(POSITIVE), 'system_negative': sorted(NEGATIVE),
                   'needs_review': '单独统计，不进入精准率或召回率分母',
                   'missing_and_duplicates': '缺评、不匹配、歧义和无效数据单列，不按失败计入',
                   'read_only': True},
        'coverage': report['summary'],
        'strict': score(report),
        'excluded': {key: report[key] for key in ('missing_rereview', 'missing_feedback', 'ambiguous',
                                                   'incomplete', 'invalid_feedback', 'database_problems', 'diagnostic')},
    }
    if include_loose:
        result['loose'] = score(report, loose=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feedback_db')
    parser.add_argument('isolated', nargs='+')
    parser.add_argument('--loose', action='store_true', help='额外输出将 marginal 视为正例的宽松口径')
    args = parser.parse_args(argv)
    try:
        result = metrics(compare(args.feedback_db, args.isolated), args.loose)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
