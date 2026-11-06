"""低层取证接口：state dict → 结构化 records，无 JSON-dump 往返。

与 diagnostic.py 的关系：diagnose() 面向人展示（sections 是 indent=2 的 JSON 字符串），
本模块面向程序消费（原生 dict/list），两者共享 _load/_open 只读语义但不互相调用。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _open(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _objects(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ids(value: Any, *, strings_only: bool = False) -> list[str]:
    """只投影列表中的合法 ID，损坏的标量不能拆成字符或导致异常。"""
    if not isinstance(value, list):
        return []
    allowed = (str,) if strings_only else (str, int)
    return [str(x) for x in value if isinstance(x, allowed)]


def parse_state(state: dict[str, Any]) -> dict[str, Any]:
    """把任务 state_json 解析为工具层需要的结构化视图。

    返回键：video_meta, candidates, reviews_by_candidate, decisions_by_candidate,
    route, evidence_by_id, claims, boundary, thesis_contract, highlights,
    review_thesis_contract, verification_decision, input_quality, risk_signals。
    缺失字段返回空容器，不抛异常——对话 Agent 需要知道"没记录"而不是崩掉。
    """
    video = _dict(state.get('video'))
    understanding = _dict(state.get('understanding'))

    # 证据索引
    evidence_list = _objects(video.get('evidence'))
    evidence_by_id = {str(e['evidence_id']): e for e in evidence_list if e.get('evidence_id')}

    # 候选
    candidates = []
    for c in _objects(state.get('candidates')):
        cid = str(c.get('candidate_id') or '')
        if not cid:
            continue
        candidates.append({
            'candidate_id': cid,
            'title': str(c.get('rendered_title') or c.get('headline') or ''),
            'slot_id': str(c.get('slot_id') or ''),
            'highlight_id': str(c.get('highlight_id') or ''),
            'claim_ids': _ids(c.get('claim_ids')),
            'evidence_ids': _ids(c.get('evidence_ids')),
            'generation_round': c.get('generation_round', 0),
            'parent_candidate_id': c.get('parent_candidate_id'),
        })

    # 评审按候选分组
    reviews_by_candidate: dict[str, list[dict]] = {}
    for r in _objects(state.get('reviews')):
        cid = str(r.get('candidate_id') or '')
        if cid:
            reviews_by_candidate.setdefault(cid, []).append(_project_review(r))

    # 决策按候选分组
    decisions_by_candidate: dict[str, dict] = {}
    for d in _objects(state.get('candidate_decisions')):
        cid = str(d.get('candidate_id') or '')
        if cid:
            decisions_by_candidate[cid] = {
                'status': str(d.get('status') or ''),
                'reason': str(d.get('reason') or ''),
                'repairable': d.get('repairable'),
            }

    # 路由
    route = _dict(state.get('route'))

    # 主旨契约
    thesis_contract = _dict(state.get('thesis_contract'))
    review_thesis_contract = _dict(state.get('review_thesis_contract'))

    # 看点
    highlights = _objects(state.get('highlights'))

    # 断言
    claims = _objects(understanding.get('claims'))

    # 边界
    boundary = _dict(state.get('boundary'))

    # 核查决策
    verification_decision = _dict(state.get('verification_decision'))

    # 视频元数据（白名单字段）
    video_meta = {k: video[k] for k in ('video_id', 'title', 'author', 'url', 'format', 'domain', 'duration', 'published_at') if k in video}

    return {
        'video_meta': video_meta,
        'candidates': candidates,
        'reviews_by_candidate': reviews_by_candidate,
        'decisions_by_candidate': decisions_by_candidate,
        'route': route,
        'evidence_by_id': evidence_by_id,
        'claims': claims,
        'boundary': boundary,
        'thesis_contract': thesis_contract,
        'review_thesis_contract': review_thesis_contract,
        'highlights': highlights,
        'verification_decision': verification_decision,
        'input_quality': state.get('input_quality'),
        'risk_signals': _objects(state.get('risk_signals')),
    }


def _project_review(review: dict[str, Any]) -> dict[str, Any]:
    """投影评审记录：丢弃冗余字段，保留解释所需信息。实测省 ~60% 体积。"""
    gates = {}
    raw_gates = _dict(review.get('gates'))
    for name, gate in raw_gates.items():
        if isinstance(gate, dict):
            gates[name] = {
                'status': str(gate.get('status') or ''),
                'reason': str(gate.get('reason') or ''),
                'evidence_ids': _ids(gate.get('evidence_ids')),
                'rule_ids': _ids(gate.get('rule_ids'), strings_only=True),
            }
    issues = []
    for issue in _objects(review.get('issues')):
        issues.append({
            'issue_id': str(issue.get('issue_id') or ''),
            'issue_code': str(issue.get('issue_code') or ''),
            'severity': str(issue.get('severity') or ''),
            'reason': str(issue.get('reason') or ''),
            'action': str(issue.get('action') or ''),
            'problem_span': str(issue.get('problem_span') or ''),
            'evidence_ids': _ids(issue.get('evidence_ids')),
        })
    quality = {}
    for dim, val in _dict(review.get('quality')).items():
        if isinstance(val, dict):
            quality[dim] = {'score': val.get('score'), 'reason': str(val.get('reason') or '')}
    return {
        'candidate_id': str(review.get('candidate_id') or ''),
        'expected_takeaway': _dict(review.get('expected_takeaway')),
        'gates': gates,
        'issues': issues,
        'quality': quality,
        'rewrite_guidance': _dict(review.get('rewrite_guidance')),
    }


def load_task(db_path: str | Path, task_id: str) -> dict[str, Any] | None:
    """只读加载单个任务的解析结果；不存在返回 None。"""
    with closing(_open(db_path)) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    state = json.loads(row['state_json'] or '{}')
    if not isinstance(state, dict):
        return None
    parsed = parse_state(state)
    parsed['_task_row'] = {'task_id': row['task_id'], 'video_id': row['video_id'],
                           'status': row['status'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}
    return parsed


def list_tasks_from_db(db_path: str | Path) -> list[dict[str, Any]]:
    """列出指定库中所有任务的摘要（不含 state_json 原文）。"""
    with closing(_open(db_path)) as conn:
        rows = conn.execute('SELECT task_id, video_id, status, created_at, updated_at, state_json FROM tasks ORDER BY created_at DESC').fetchall()
    out = []
    for row in rows:
        state = json.loads(row['state_json'] or '{}')
        video = _dict(state.get('video')) if isinstance(state, dict) else {}
        title = str(video.get('title') or row['video_id'])
        candidates = _objects(state.get('candidates')) if isinstance(state, dict) else []
        route = _dict(state.get('route')) if isinstance(state, dict) else {}
        out.append({
            'task_id': row['task_id'], 'video_id': row['video_id'], 'status': row['status'],
            'title': title, 'candidate_count': len(candidates),
            'recommended': str(route.get('recommended_candidate_id') or ''),
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
        })
    return out
