"""已批准修复的观察与结案：复发线索不等同自动证明根因。"""
from __future__ import annotations
import json
from .repairs import RepairStore, _json, _now


def init(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS repair_deployments(proposal_id TEXT, manifest_id TEXT, status TEXT, payload TEXT, PRIMARY KEY(proposal_id,manifest_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS repair_observations(observation_id TEXT PRIMARY KEY, proposal_id TEXT, manifest_id TEXT, payload TEXT)')


def register(conn, manifest):
    init(conn)
    store = RepairStore(conn)
    current = {r['feedback_key'] + '@' + r['revision']: r for r in store.list()}
    for pid in manifest['proposal_ids']:
        if pid not in current or current[pid]['status'] != 'reviewed':
            raise ValueError('提案已过期或未审核')
        row = current[pid]
        payload = {'registered_at': _now(), 'family': row['diagnosis'].get('family', 'unresolved'), 'source_video_id': (row['payload']['snapshot'].get('video') or {}).get('video_id'), 'manifest_path': manifest.get('manifest_path'), 'reviewer': manifest.get('reviewer'), 'baseline_revisions': [r['revision'] for r in current.values()]}
        conn.execute('INSERT OR IGNORE INTO repair_deployments VALUES(?,?,?,?)', (pid, manifest.get('manifest_id') or manifest['id'], 'observing', _json(payload)))
    conn.commit()


def observe_feedback(conn, feedback_key, revision, payload, diagnosis):
    """只对已部署、不同视频的同类负反馈发复发疑似，保持需审核。"""
    init(conn)
    video_id = (payload['snapshot'].get('video') or {}).get('video_id')
    if not video_id or diagnosis.get('family') == 'unresolved':
        return
    for pid, mid, raw in conn.execute("SELECT proposal_id,manifest_id,payload FROM repair_deployments WHERE status IN ('observing','closed','recurrence_suspected')").fetchall():
        dep = json.loads(raw)
        # 登记前已有的历史判定不构成“发布后复发”；导入时间不能冒充发生时间。
        baseline = dep.get('baseline_revisions', [])
        if revision in baseline or dep['family'] != diagnosis.get('family') or dep.get('source_video_id') == video_id:
            continue
        observation_id = f'{pid}:{mid}:{revision}'
        record = {'feedback_key': feedback_key, 'revision': revision, 'video_id': video_id, 'kind': 'recurrence_suspected', 'reason': '发布后不同视频同类负反馈，需确认是否同根因', 'created_at': _now()}
        conn.execute('INSERT OR IGNORE INTO repair_observations VALUES(?,?,?,?)', (observation_id, pid, mid, _json(record)))
        conn.execute("UPDATE repair_deployments SET status='recurrence_suspected' WHERE proposal_id=? AND manifest_id=?", (pid, mid))


def close(conn, proposal_id, manifest_id, reviewer, evidence, confirmed=False):
    init(conn)
    row = conn.execute('SELECT status,payload FROM repair_deployments WHERE proposal_id=? AND manifest_id=?', (proposal_id, manifest_id)).fetchone()
    if not row or row[0] != 'observing' or not confirmed or not reviewer.strip() or not evidence.strip():
        raise ValueError('只能对观察中的修复以明确审核及观察证据结案；疑似复发须先调查')
    data = json.loads(row[1]); data['closure'] = {'reviewer': reviewer, 'evidence': evidence, 'closed_at': _now()}
    conn.execute("UPDATE repair_deployments SET status='closed',payload=? WHERE proposal_id=? AND manifest_id=?", (_json(data), proposal_id, manifest_id))
    conn.commit()


def resolve(conn, proposal_id, manifest_id, reviewer, evidence, confirmed=False):
    init(conn)
    row = conn.execute('SELECT status,payload FROM repair_deployments WHERE proposal_id=? AND manifest_id=?', (proposal_id, manifest_id)).fetchone()
    if not row or row[0] != 'recurrence_suspected' or not confirmed or not reviewer.strip() or not evidence.strip():
        raise ValueError('必须明确复核疑似复发并留下证据')
    data = json.loads(row[1]); data.setdefault('resolutions', []).append({'reviewer': reviewer, 'evidence': evidence, 'at': _now()})
    conn.execute("UPDATE repair_deployments SET status='observing',payload=? WHERE proposal_id=? AND manifest_id=?", (_json(data), proposal_id, manifest_id))
    conn.commit()
