"""只读回放历史候选；不调用模型，不把旧人工判定当作逐风险真值。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_system.signals import extract_risk_signals, check_signal_constraints, signal_review_reasons, unsupported_numeric_facts
from agent_system.labels import required_prefixes


def replay_database(path: Path) -> list[dict]:
    output = []
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    try:
        for row in conn.execute('SELECT task_id,video_id,config_json,state_json,created_at FROM tasks'):
            task_id, video_id, config_raw, state_raw, created = row
            config, state = json.loads(config_raw), json.loads(state_raw)
            if config.get('model_provider') == 'mock':
                continue
            video = state.get('video') or {}
            if not video.get('evidence'):
                continue
            evidence = [SimpleNamespace(**e) for e in video['evidence']]
            report = extract_risk_signals(SimpleNamespace(evidence=evidence))
            claims = [SimpleNamespace(claim_id=c['claim_id'], text=c.get('text', ''), evidence_refs=c.get('evidence_refs', [])) for c in state.get('understanding', {}).get('claims', [])]
            by_id = {c.claim_id: c for c in claims}
            humans = {h.get('candidate_id'): h for h in state.get('human_decisions', [])}
            for candidate in state.get('candidates', []):
                title = candidate.get('headline', '')
                cited_ids = candidate.get('claim_ids', [])
                refs = {r for cid in cited_ids if cid in by_id for r in by_id[cid].evidence_refs}
                texts = [e.text for e in evidence if e.evidence_id in refs and e.field != 'frame' and e.quality not in {'model_derived_uncalibrated', 'unverified_source_metadata'}]
                errors = check_signal_constraints(title, report, claims, cited_ids)
                numbers = unsupported_numeric_facts(title, texts)
                if numbers:
                    errors.append({'code': 'G4_NUMERIC_CONFLICT', 'facts': numbers})
                expected = set(report['required_prefixes']) | set(required_prefixes(format=video.get('format', ''), attributes=state.get('understanding', {}).get('attributes', [])))
                missing = sorted(expected - set(candidate.get('prefix_labels', [])))
                if missing:
                    errors.append({'code': 'G4_PREFIX_MISSING', 'labels': missing})
                review = signal_review_reasons(title, report, refs)
                if report['pending_signal_ids']:
                    review.append({'code': 'CONTENT_NATURE_SIGNAL_UNRESOLVED', 'signal_ids': report['pending_signal_ids']})
                human = humans.get(candidate.get('candidate_id'), {})
                output.append({'database': str(path), 'task_id': task_id, 'video_id': video_id, 'created_at': created,
                    'candidate_id': candidate.get('candidate_id'), 'headline': title, 'old_route': state.get('route', {}).get('action'),
                    'human_decision': human.get('decision'), 'human_reason': human.get('reason'),
                    'action': 'rewrite' if errors else 'needs_review' if review else 'unchanged',
                    'errors': errors, 'review_reasons': review, 'cited_evidence_ids': sorted(refs),
                    'signals': report['signals'], 'evidence': [{'evidence_id': e.evidence_id, 'field': e.field, 'text': e.text, 'quality': e.quality} for e in evidence if e.evidence_id in refs or any(s['evidence_id'] == e.evidence_id for s in report['signals'])],
                    'unknown_claim_ids': [cid for cid in cited_ids if cid not in by_id],
                    'risk_ground_truth': 'unadjudicated'})
    finally:
        conn.close()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='append', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    rows = [r for p in args.db for r in replay_database(Path(p))]
    # 同一视频每次实验均保留；另给最新任务去重视角，避免重复候选放大负担。
    latest = {}
    for row in rows:
        key = row['video_id']
        if key not in latest or (row['created_at'], row['task_id']) > latest[key]:
            latest[key] = (row['created_at'], row['task_id'])
    unique = [r for r in rows if (r['created_at'], r['task_id']) == latest[r['video_id']]]
    def summarize(items):
        return {'candidates': len(items), 'videos': len({r['video_id'] for r in items}),
            'actions': dict(Counter(r['action'] for r in items)),
            'triggered_videos': len({r['video_id'] for r in items if r['action'] != 'unchanged'}),
            'human_overlap': dict(Counter((r['human_decision'] or 'unlabelled') + ':' + r['action'] for r in items)),
            'codes': dict(Counter(x['code'] for r in items for x in r['errors'] + r['review_reasons']))}
    result = {'scope': '历史候选影子回放，非新模型运行；动作只覆盖信号守卫，不代表完整流水线最终路由',
        'limitations': ['人工通过/驳回不是逐风险真值，不可据此计算准确率或召回率', '旧引用不足可能导致新增拦截，需逐条核验', '最新任务按视频去重，但不代表随机抽样'],
        'source_sha256': hashlib.sha256(Path(__file__).resolve().parents[1].joinpath('agent_system/signals.py').read_bytes()).hexdigest(),
        'all_runs': summarize(rows), 'latest_per_video': summarize(unique), 'rows': rows}
    target = Path(args.output)
    if target.exists():
        raise SystemExit('输出已存在，拒绝覆盖，请使用新路径')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
