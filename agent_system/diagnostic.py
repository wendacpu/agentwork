"""只读任务诊断：不启动流水线、不使用会初始化数据库的 Storage。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _open(db_path: str | Path) -> sqlite3.Connection:
    # URI 必须转义路径中的 ?、# 等字符；不使用 immutable，避免漏掉 WAL 中的最新状态。
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _state(row: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(row['state_json'] or '{}')
    if not isinstance(value, dict):
        raise ValueError('任务 state_json 不是对象')
    return value


def _load(db_path: str | Path, task_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    with closing(_open(db_path)) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(f'任务不存在: {task_id}')
    return row, _state(row)


def _objects(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _title(candidate: dict[str, Any]) -> str:
    return str(candidate.get('rendered_title') or candidate.get('headline') or '')


def _candidates(state: dict[str, Any]) -> list[dict[str, str]]:
    return [{'candidate_id': str(c['candidate_id']), 'title': _title(c)}
            for c in _objects(state.get('candidates')) if c.get('candidate_id')]


def list_tasks(db_path: str | Path) -> list[dict[str, Any]]:
    """列出指定库中的任务；结果携带规范化数据库路径以避免跨库混淆。"""
    path = str(Path(db_path).resolve())
    with closing(_open(db_path)) as conn:
        rows = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC').fetchall()
    return [{'db_path': path, 'task_id': row['task_id'], 'video_id': row['video_id'],
             'status': row['status'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
             'title': _dict(_state(row).get('video')).get('title') or row['video_id']}
            for row in rows]


def get_task_info(db_path: str | Path, task_id: str) -> dict[str, Any]:
    row, state = _load(db_path, task_id)
    return {'db_path': str(Path(db_path).resolve()), 'task_id': task_id,
            'video_id': row['video_id'], 'title': _dict(state.get('video')).get('title') or row['video_id'],
            'candidates': _candidates(state)}


def _dump(value: Any) -> str:
    """保留原字段，使用缩进便于人工阅读；不把记录转换为事实结论。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def diagnose(db_path: str | Path, task_id: str, scenario: str,
             candidate_id: str | None = None, question: str = '') -> dict[str, Any]:
    """按任务快照原样呈现证据和记录；question 仅作区分大小写的字面检索。"""
    if scenario not in {'understanding', 'review', 'evidence'}:
        raise ValueError('scenario 必须为 understanding、review 或 evidence')
    row, state = _load(db_path, task_id)
    candidates = _candidates(state)
    by_id = {c['candidate_id']: c for c in candidates}
    if candidate_id is not None and candidate_id not in by_id:
        raise ValueError('候选不属于指定数据库中的指定任务')
    if candidate_id is None and len(candidates) == 1:
        candidate_id = candidates[0]['candidate_id']
    video = _dict(state.get('video'))
    understanding = _dict(state.get('understanding'))
    result: dict[str, Any] = {
        'db_path': str(Path(db_path).resolve()), 'task_id': task_id, 'video_id': row['video_id'],
        'scenario': scenario, 'candidate_id': candidate_id,
        'title': by_id[candidate_id]['title'] if candidate_id else (video.get('title') or row['video_id']),
        'sections': [], 'notices': [], 'candidates': candidates,
    }
    notices = result['notices']
    sections = result['sections']
    if candidate_id is None and len(candidates) > 1 and scenario != 'understanding':
        if question:
            notices.append('请先选择候选；question 尚未检索，不进行自然语言理解。')
        notices.append('存在多个候选，请指定 candidate_id；不推断选择或采用推荐候选。')
        sections.append({'heading': '候选清单', 'items': [_dump(c) for c in candidates]})
        return result
    if not candidates:
        notices.append('任务快照没有候选。')
    selected = next((c for c in _objects(state.get('candidates')) if c.get('candidate_id') == candidate_id), None)
    evidence = _objects(video.get('evidence'))
    claims = _objects(understanding.get('claims'))
    sources = _objects(understanding.get('extracted_sources'))
    evidence_by_id = {e.get('evidence_id'): e for e in evidence if e.get('evidence_id')}
    all_refs: list[tuple[str, Any]] = []
    for obj, label, fields in [(selected or {}, '候选', ('evidence_ids',)),
                               *[(c, '断言 ' + str(c.get('claim_id', '')), ('evidence_refs',)) for c in claims],
                               *[(s, '来源 ' + str(s.get('source_id', '')), ('evidence_refs',)) for s in sources]]:
        for field in fields:
            refs = obj.get(field)
            if isinstance(refs, list):
                all_refs.extend((label, ref) for ref in refs)
    for label, ref in all_refs:
        if ref not in evidence_by_id:
            notices.append(f'{label}的证据引用悬空：{ref}')

    def section(heading: str, records: list[Any], missing: str) -> None:
        sections.append({'heading': heading, 'items': [_dump(x) for x in records] if records else [missing]})

    if scenario == 'review':
        reviews = [r for r in _objects(state.get('reviews')) if r.get('candidate_id') == candidate_id] if candidate_id else []
        decisions = [r for r in _objects(state.get('candidate_decisions')) if r.get('candidate_id') == candidate_id] if candidate_id else []
        section('候选引用', [selected] if selected else [], '未选择候选或没有候选。')
        section('候选级评审', reviews, '未记录该候选评审。')
        section('候选级决策', decisions, '未记录该候选决策。')
        review_refs = []
        for review in reviews:
            for gate in _dict(review.get('gates')).values():
                if isinstance(gate, dict) and isinstance(gate.get('evidence_ids'), list):
                    review_refs.extend(gate['evidence_ids'])
            for issue in _objects(review.get('issues')):
                if isinstance(issue.get('evidence_ids'), list):
                    review_refs.extend(issue['evidence_ids'])
        review_evidence = [e for e in evidence if e.get('evidence_id') in review_refs]
        section('评审关联证据', review_evidence, '未记录或无法匹配评审关联证据。')
        section('任务级路由（不等于候选级评审）', [state['route']] if isinstance(state.get('route'), dict) and state['route'] else [], '未记录路由。')
    elif scenario == 'understanding':
        video_meta = {k: video[k] for k in ('title', 'author', 'url', 'description', 'caption', 'format', 'domain', 'duration', 'published_at') if k in video}
        section('视频素材信息', [video_meta] if video_meta else [], '缺少视频素材信息。')
        section('主旨契约', [state['thesis_contract']] if state.get('thesis_contract') else [], '未记录主旨契约。')
        section('评审主旨契约', [state['review_thesis_contract']] if state.get('review_thesis_contract') else [], '未记录评审主旨契约。')
        highlights = state.get('highlights')
        section('看点与引用', highlights if isinstance(highlights, list) else ([highlights] if highlights else []), '未记录看点。')
        section('理解断言', claims, '未记录理解断言。')
        section('证据原文、定位与质量', evidence, '未记录证据。')
        section('引用来源', sources, '未记录引用来源。')
        section('语义单元、事件与转折', [{k: understanding[k] for k in ('semantic_units', 'events', 'turns', 'conflicts') if understanding.get(k)}] if any(understanding.get(k) for k in ('semantic_units', 'events', 'turns', 'conflicts')) else [], '未记录语义单元或事件。')
        section('事实边界', [state['boundary']] if state.get('boundary') else [], '未记录事实边界。')
    else:
        section('候选引用', [selected] if selected else [], '未选择候选或没有候选。')
        candidate_claim_ids = (selected or {}).get('claim_ids') or []
        if not isinstance(candidate_claim_ids, list):
            candidate_claim_ids = []
        referenced_claims = [c for c in claims if c.get('claim_id') in candidate_claim_ids]
        section('候选关联断言（模型抽取，非事实裁决）', referenced_claims, '未记录或无法匹配候选关联断言。')
        candidate_refs = (selected or {}).get('evidence_ids') or []
        if not isinstance(candidate_refs, list):
            candidate_refs = []
        cited_evidence = [e for e in evidence if e.get('evidence_id') in candidate_refs]
        section('候选关联证据原文、定位与质量', cited_evidence, '未记录或无法匹配候选关联证据。')
        section('全部素材证据（不一定被候选引用）', evidence, '未记录证据。')
        section('全部断言与历史核查', claims, '未记录断言，核查状态未知。')
        section('引用来源', sources, '未记录引用来源。')
    if claims:
        for claim in claims:
            verification = claim.get('verification')
            if not isinstance(verification, dict) or not verification.get('verdict') or verification.get('verdict') == 'unverified':
                notices.append(f"断言 {claim.get('claim_id', '未知')}：未核查（无有效核查结果）。")
    else:
        notices.append('未记录断言，无法判断是否执行过核查。')
    notices.append('证据、引用及既有评审只表示记录内容，不构成事实已证实；核查结论亦为原始记录。')
    if question:
        matches = []
        for heading, records in [('证据', evidence), ('断言', claims), ('来源', sources),
                                 ('候选', [selected] if selected else []),
                                 ('评审', [r for r in _objects(state.get('reviews')) if r.get('candidate_id') == candidate_id])]:
            for record in records:
                if question in _dump(record):
                    matches.append(f'{heading}: {_dump(record)}')
        sections.append({'heading': '问题字面检索', 'items': matches or ['未在检索范围内找到完全匹配。']})
        notices.append('question 只做区分大小写的完整字面匹配，范围为证据、断言、来源、当前候选和其评审；不理解或回答自然语言问题。')
    return result
