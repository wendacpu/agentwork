"""显式将快照人工否定意见用于单候选定向重写；不修改原任务或反馈库。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def load_feedback(snapshot_path: str, feedback_db: str, task_id: str, candidate_id: str, title_hash: str) -> tuple[dict, dict]:
    """校验快照、当前有效反馈与完整标题身份；未提供或不匹配时拒绝。"""
    snapshot_path = Path(snapshot_path).resolve(strict=True)
    feedback_path = Path(feedback_db).resolve(strict=True)
    if snapshot_path.samefile(feedback_path):
        raise ValueError('反馈库不能是快照文件')
    raw = snapshot_path.read_bytes()
    snapshot = json.loads(raw)
    tasks = [t for t in snapshot.get('tasks', []) if t.get('task_id') == task_id]
    if len(tasks) != 1 or tasks[0].get('status') != 'completed':
        raise ValueError('快照任务不存在、不唯一或未完成')
    task = tasks[0]
    entries = [e for e in task.get('candidates', []) if (e.get('candidate') or {}).get('candidate_id') == candidate_id]
    if len(entries) != 1:
        raise ValueError('快照候选不存在或不唯一')
    entry = entries[0]
    candidate = entry['candidate']
    title = str(candidate.get('rendered_title') or candidate.get('headline') or '')
    digest = hashlib.sha256(title.encode('utf-8')).hexdigest()
    if not title or title_hash != digest or entry.get('title_hash') != digest:
        raise ValueError('候选标题哈希不匹配')
    with sqlite3.connect(feedback_path.as_uri() + '?mode=ro', uri=True) as conn:
        identity = conn.execute('SELECT digest FROM snapshot_identity').fetchall()
        if identity != [(hashlib.sha256(raw).hexdigest(),)]:
            raise ValueError('反馈库与快照身份不一致')
        rows = conn.execute('SELECT payload_json FROM candidate_decisions_v2 WHERE batch_id=? AND task_id=? AND candidate_id=? AND title_hash=?',
                            (snapshot_path.parent.name, task_id, candidate_id, digest)).fetchall()
    if len(rows) != 1:
        raise ValueError('当前有效人工反馈不存在或不唯一')
    feedback = json.loads(rows[0][0])
    if (feedback.get('task_id') != task_id or feedback.get('candidate_id') != candidate_id
            or feedback.get('title_hash') != digest or feedback.get('video_id') != task.get('video_id')
            or feedback.get('decision') not in {'rejected', 'marginal'} or feedback.get('scope') != 'frozen'
            or not str(feedback.get('reason') or '').strip()):
        raise ValueError('反馈身份、状态、范围或理由无效')
    return task, {'task_id': task_id, 'candidate_id': candidate_id, 'title_hash': digest,
                  'decision': feedback['decision'], 'reason': str(feedback['reason'])[:500],
                  'reviewer': str(feedback.get('reviewer') or ''), 'updated_at': str(feedback.get('updated_at') or ''),
                  'snapshot_sha256': hashlib.sha256(raw).hexdigest()}
