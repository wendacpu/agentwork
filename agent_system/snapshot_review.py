"""对已构建的重评快照提供只读展示与独立反馈，不运行模型。

用法：python -m agent_system.snapshot_review --snapshot 路径/snapshot.json
快照须为完整、已结束的静态文件；不能指向运行中批次。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .batch_review import BatchReviewStore, make_server, title_hash
from .batch_review_page import PAGE


class SnapshotReviewStore(BatchReviewStore):
    """以快照替换原任务数据库，沿用现有评审、校验与反馈账本。"""

    def __init__(self, snapshot_path: str | Path, feedback_db: str | Path | None = None):
        self.snapshot_path = Path(snapshot_path).resolve(strict=True)
        snapshot_bytes = self.snapshot_path.read_bytes()
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot = json.loads(snapshot_bytes)
        tasks = snapshot.get('tasks')
        if not isinstance(tasks, list) or snapshot.get('task_count') != len(tasks):
            raise ValueError('快照任务清单或数量不一致')
        self.batch_dir = self.snapshot_path.parent
        self.source_db = self.snapshot_path
        self.legacy_db = (self.batch_dir / 'human_feedback.db').resolve()
        self.isolate_legacy = True
        self.feedback_db = (Path(feedback_db) if feedback_db else self.batch_dir / 'snapshot_feedback.db').resolve()
        # 不允许通过硬链接或路径别名覆盖任何已有快照内容或旧反馈库。
        if self.feedback_db in {self.snapshot_path, self.legacy_db, (self.batch_dir / 'agent.db').resolve(),
                                (self.batch_dir / 'human_feedback_v2.db').resolve()}:
            raise ValueError('反馈库必须独立于快照、原任务及旧反馈库')
        if self.feedback_db.exists() and self.feedback_db.samefile(self.snapshot_path):
            raise ValueError('反馈库不得覆盖快照')
        self.mapping, self._states, self._snapshot_tasks = [], {}, {}
        candidate_count = 0
        for n, task in enumerate(tasks, 1):
            if not isinstance(task, dict) or task.get('status') not in {'completed', 'failed'}:
                raise ValueError('快照包含未结束或无效任务')
            task_id = task.get('task_id')
            if not isinstance(task_id, str) or not task_id or task_id in self._states:
                raise ValueError('快照任务标识缺失或重复')
            entries = task.get('candidates')
            if not isinstance(entries, list):
                raise ValueError('快照候选清单无效')
            candidate_count += len(entries)
            candidates, decisions, frozen = [], [], []
            seen = set()
            for entry in entries:
                c = entry.get('candidate') if isinstance(entry, dict) else None
                if not isinstance(c, dict):
                    raise ValueError('快照候选无效')
                cid = str(c.get('candidate_id') or '')
                title = str(c.get('rendered_title') or c.get('headline') or '')
                digest = title_hash(title)
                if entry.get('title_hash') != digest or entry.get('headline_hash') != title_hash(str(c.get('headline') or '')):
                    raise ValueError('快照候选标题哈希不一致')
                if not cid or not title or (cid, digest) in seen:
                    raise ValueError('快照候选身份缺失或重复')
                seen.add((cid, digest))
                candidates.append(c)
                decision = entry.get('decision')
                if decision is not None:
                    if not isinstance(decision, dict):
                        raise ValueError('快照系统判定无效')
                    decisions.append({**decision, 'candidate_id': cid})
                frozen.append({'candidate_id': cid, 'title_sha256': digest})
            route = dict(task.get('route') or {})
            recommended = [str(e['candidate']['candidate_id']) for e in entries if e.get('recommended')]
            if len(recommended) > 1:
                raise ValueError('快照含多个推荐候选')
            # 推荐标记以快照候选显式记录为准，不能沿用过期 route。
            route['recommended_candidate_id'] = recommended[0] if recommended else None
            state = {'video': task.get('video') or {}, 'candidates': candidates,
                     'candidate_decisions': decisions, 'route': route,
                     'delivery_slots': task.get('delivery_slots') or {},
                     'error': task.get('error')}
            item = {'task_id': task_id, 'video_id': str(task.get('video_id') or ''),
                    'blind_id': str(task.get('blind_id') or f'R{n:02d}'),
                    'category': str(task.get('category') or '重评快照'), 'candidates': frozen}
            self.mapping.append(item)
            self._states[task_id] = state
            self._snapshot_tasks[task_id] = task
        if snapshot.get('candidate_count') != candidate_count:
            raise ValueError('快照候选数量不一致')
        self.by_task = {item['task_id']: item for item in self.mapping}
        self.feedback_db.parent.mkdir(parents=True, exist_ok=True)
        if self.feedback_db.exists():
            try:
                with sqlite3.connect(self.feedback_db.as_uri() + '?mode=ro', uri=True) as conn:
                    identity = conn.execute('SELECT digest FROM snapshot_identity').fetchall()
            except sqlite3.Error as exc:
                raise ValueError('已有文件不是本快照的反馈库') from exc
            if len(identity) != 1 or identity[0][0] != snapshot_digest:
                raise ValueError('反馈库属于另一份快照')
        with self._feedback_conn() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS snapshot_identity (digest TEXT NOT NULL)')
            identity = conn.execute('SELECT digest FROM snapshot_identity').fetchall()
            if identity and (len(identity) != 1 or identity[0][0] != snapshot_digest):
                raise ValueError('反馈库属于另一份快照')
            if not identity:
                conn.execute('INSERT INTO snapshot_identity VALUES (?)', (snapshot_digest,))
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS candidate_decisions_v2 (
                    batch_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, title_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (batch_id, task_id, candidate_id, title_hash)
                );
                CREATE TABLE IF NOT EXISTS decision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')

    def _source(self, task_id):
        if task_id not in self._states:
            raise ValueError('任务不属于当前批次')
        # 上层会暂存 _review_candidate_pool；返回副本，避免改变快照。
        state = dict(self._states[task_id])
        return {'status': self._snapshot_tasks[task_id].get('original_status') or ''}, state

    def _task_candidate_pool(self, item, state):
        return list(state.get('candidates') or [])

    def list_tasks(self):
        tasks = super().list_tasks()
        for task in tasks:
            meta = self._snapshot_tasks[task['task_id']]
            failed = meta['status'] == 'failed'
            task['source_status'] = '失败：无新结果，显示旧状态' if failed else '成功：显示新状态'
            if failed:
                task['review_status'] = 'no_candidates'
        return tasks

    def get_task(self, task_id):
        task = super().get_task(task_id)
        meta = self._snapshot_tasks[task_id]
        task['snapshot_status'] = meta['status']
        task['snapshot_error'] = str(meta.get('error') or '')
        task['snapshot_error_type'] = str(meta.get('error_type') or '')
        task['conclusion_origin'] = str(meta.get('conclusion_origin') or '')
        return task

    def record_decision(self, body):
        task = self._snapshot_tasks.get(str(body.get('task_id') or ''))
        if task and task['status'] != 'completed':
            raise ValueError('失败任务没有新结果，旧状态仅供查看')
        return super().record_decision(body)


# 原页面保留所有既有媒体、候选和人工判定交互；只补快照来源提示。
SNAPSHOT_PAGE = PAGE.replace('function render(task){', 'function renderBase(task){', 1).replace(
    'async function openTask(id){', '''function render(task){
  renderBase(task);
  const failed=task.snapshot_status==='failed';
  const label=failed?'本次失败：无新结果，以下仅为旧状态（只读）':'本次成功：以下为新状态';
  const detail=failed?`<p>失败类型：${esc(task.snapshot_error_type||'未提供')}<br>失败详情：${esc(task.snapshot_error||'未提供')}</p>`:'';
  $('content').insertAdjacentHTML('afterbegin', `<section class="card ${failed?'disputed':''}" role="status"><h2>${esc(label)}</h2>${detail}<p>结论来源：${esc(task.conclusion_origin||'未提供')}</p></section>`);
  if(failed) $('content').querySelectorAll('[data-verdict],[data-dverdict],[data-reopen],[data-dreopen]').forEach(b=>{b.disabled=true;});
}
async function openTask(id){''', 1).replace('</style>', '.headline,.reason,pre{overflow-wrap:anywhere;white-space:pre-wrap}</style>', 1)


def make_snapshot_server(store, port=0):
    server = make_server(store, port=port)
    base = server.RequestHandlerClass

    class Handler(base):
        def do_GET(self):
            if urlparse(self.path).path == '/':
                self.send_bytes(SNAPSHOT_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
            else:
                super().do_GET()

    server.RequestHandlerClass = Handler
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--feedback-db')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    store = SnapshotReviewStore(args.snapshot, args.feedback_db)
    server = make_snapshot_server(store, args.port)
    print(f'快照评审台：http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
