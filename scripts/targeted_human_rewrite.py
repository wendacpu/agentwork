#!/usr/bin/env python3
"""对快照中已否定的单条候选执行隔离重写；需要显式提供身份与模型配置。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_system.cli import make_pipeline
from agent_system.config import RunConfig
from agent_system.human_rewrite import load_feedback
from agent_system.pipeline import _normalize_boundary, _normalize_understanding
from agent_system.storage import Storage
from agent_system.types import Evidence, Highlight, VideoInput


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'feedback-db', 'source-db', 'task-id', 'candidate-id', 'title-hash', 'config', 'output'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--allow-paid', action='store_true')
    args = p.parse_args()
    task, feedback = load_feedback(args.snapshot, args.feedback_db, args.task_id, args.candidate_id, args.title_hash)
    source_db = Path(args.source_db).resolve(strict=True)
    with sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True) as c:
        row = c.execute('SELECT video_id,state_json FROM tasks WHERE task_id=?', (args.task_id,)).fetchone()
    if not row or row[0] != task['video_id']:
        raise ValueError('原任务不存在或视频身份不一致')
    state = json.loads(row[1])
    if state.get('video') != task.get('video'):
        raise ValueError('原始视频与快照不一致')
    entries = [e for e in task['candidates'] if e['candidate']['candidate_id'] == args.candidate_id]
    source = entries[0]['candidate']
    matches = [c for c in state.get('candidates', []) if c.get('candidate_id') == args.candidate_id]
    if len(matches) != 1 or matches[0] != source:
        raise ValueError('原候选与快照不一致')
    config = RunConfig.from_json(args.config)
    config.apply_env()
    if config.model_provider != 'mock' and not args.allow_paid:
        raise ValueError('付费模型需显式 --allow-paid')
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    config.output_dir = str(out)
    config.case_bank = config.cache_bank = ''
    config.case_bank_isolated = config.cache_bank_isolated = True
    config.memory_call_limit = 0
    storage = Storage(out / 'agent.db')
    tid = storage.create_task(task['video_id'], asdict(config))
    # 反馈中的原任务身份不伪装成新任务身份；附上来源，按目标任务校验。
    feedback['source_task_id'] = feedback['task_id']
    feedback['task_id'] = tid
    state['targeted_rewrite_feedback'] = feedback
    state['targeted_rewrite_source'] = {'task_id': args.task_id, 'snapshot': str(Path(args.snapshot).resolve()), 'candidate_id': args.candidate_id, 'title_hash': args.title_hash}
    storage.set_state(tid, 'rewriting', state)
    try:
        snapshot = state['video']
        video = VideoInput(**{**snapshot, 'evidence': [Evidence(**e) for e in snapshot['evidence']]})
        understanding = _normalize_understanding(state['understanding'])
        boundary = _normalize_boundary(state['boundary'])
        highlights = [Highlight(**h) for h in state.get('highlights', [])]
        pipeline = make_pipeline(config, None, storage)
        result = pipeline._generate_and_review(tid, state, video, understanding, boundary, highlights, 1, source, known_candidates=state.get('candidates', []))
        state.update(candidates=result['candidates'], reviews=result['reviews'], candidate_decisions=result['decisions'], route=result['route'])
        storage.set_state(tid, 'pending_human', state)
        storage.save_candidates(tid, result['candidates'])
        print(json.dumps({'task_id': tid, 'video_id': task['video_id'], 'candidate_count': len(result['candidates']), 'route': result['route']}, ensure_ascii=False))
    except Exception as exc:
        state['error'] = str(exc)
        storage.set_state(tid, 'technical_failed', state)
        raise
    finally:
        storage.conn.close()


if __name__ == '__main__':
    main()
