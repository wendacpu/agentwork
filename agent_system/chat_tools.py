"""对话 Agent 的工具注册表：全部只读，纯函数可 pytest。

每个工具签名统一为 (params: dict, ctx: ToolContext) -> envelope dict。
envelope = {ok: bool, data: Any, refs_seen: [str], truncated: bool, notices: [str]}
refs_seen 登记本轮模型可引用的 ID；ctx 携带 db 白名单和 session focus。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evidence_index import load_task, list_tasks_from_db


@dataclass
class ToolContext:
    """服务端注入；工具无权接受路径参数，db 只能从白名单别名取。"""
    db_aliases: dict[str, str] = field(default_factory=dict)  # alias → resolved path
    default_alias: str = ''
    focus: dict[str, Any] = field(default_factory=dict)       # 当前焦点（task_id, candidate_ids）
    refs_seen: set[str] = field(default_factory=set)

    def resolve_db(self, batch: str | None = None) -> str:
        alias = batch or self.default_alias
        if not alias:
            raise ValueError('未指定批次且无默认库')
        path = self.db_aliases.get(alias)
        if not path:
            available = ', '.join(self.db_aliases.keys()) or '（空）'
            raise ValueError(f'未知批次 {alias!r}；可选：{available}')
        return path


def _envelope(data: Any, ctx: ToolContext, refs: list[str] | None = None,
              truncated: bool = False, notices: list[str] | None = None) -> dict:
    for r in (refs or []):
        ctx.refs_seen.add(r)
    return {'ok': True, 'data': data, 'refs_seen': refs or [], 'truncated': truncated, 'notices': notices or []}


def _err(msg: str) -> dict:
    return {'ok': False, 'error': msg, 'refs_seen': [], 'truncated': False, 'notices': []}


def _limit(value: Any, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('limit 必须是正整数')
    return min(value, maximum)


# ─── find_tasks ────────────────────────────────────────────────────────────────

def find_tasks(params: dict, ctx: ToolContext) -> dict:
    """跨批次搜索任务。返回摘要列表 + 多库命中计数。"""
    video_id = params.get('video_id', '')
    title_text = params.get('title_text', '')
    status = params.get('status', '')
    limit = _limit(params.get('limit', 10), 20)
    batch = params.get('batch')

    results = []
    multi_db_hits = 0

    # 统一处理：如果指定了 batch 就只搜那一个库，否则搜全部白名单库
    if batch:
        targets = [(batch, ctx.resolve_db(batch))]
    else:
        targets = [(alias, path) for alias, path in ctx.db_aliases.items()]

    for alias, path in targets:
        try:
            tasks = list_tasks_from_db(path)
        except Exception as e:
            results.append({'_error': f'{alias}: {type(e).__name__}', '_batch': alias})
            continue
        for t in tasks:
            if video_id and t['video_id'] != video_id:
                continue
            if title_text and title_text.lower() not in t['title'].lower():
                continue
            if status and t['status'] != status:
                continue
            t['batch'] = alias
            results.append(t)

    total = len(results)
    if total > 1:
        batches_hit = len({t.get('batch') for t in results if t.get('batch')})
        if batches_hit > 1:
            multi_db_hits = batches_hit

    refs = [f"task:{t['batch']}:{t['task_id']}" for t in results[:limit] if 'task_id' in t]
    notices = []
    if multi_db_hits > 1:
        notices.append(f'同一关键词在 {multi_db_hits} 个批次中命中——需要消歧时列出选项让用户选，不要猜最新批次。')
    return _envelope(results[:limit], ctx, refs=refs, truncated=total > limit, notices=notices)


# ─── get_task ──────────────────────────────────────────────────────────────────

def get_task(params: dict, ctx: ToolContext) -> dict:
    """单任务概览卡：状态、路由、候选列表、主旨摘要。~1K 字符，不含原始记录。"""
    task_id = params.get('task_id', '')
    batch = params.get('batch')
    if not task_id:
        return _err('缺少 task_id')
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    tc = parsed['thesis_contract']
    route = parsed['route']
    summary = {
        'task_id': task_id,
        'video': parsed['video_meta'],
        'status': parsed['_task_row']['status'],
        'route': {
            'action': str(route.get('action') or ''),
            'recommended_candidate_id': str(route.get('recommended_candidate_id') or ''),
            'eligible_candidate_ids': [str(x) for x in (route.get('eligible_candidate_ids') or [])],
            'reason': str(route.get('reason') or ''),
        },
        'candidates': [{'candidate_id': c['candidate_id'], 'title': c['title'],
                        'decision_status': parsed['decisions_by_candidate'].get(c['candidate_id'], {}).get('status', '')}
                       for c in parsed['candidates']],
        'thesis_topic': str(tc.get('topic') or ''),
        'thesis_core_promise': str(tc.get('core_promise') or ''),
        'claim_count': len(parsed['claims']),
        'highlight_count': len(parsed['highlights']),
        'input_quality': parsed['input_quality'],
    }
    refs = [f'task:{task_id}']
    # 更新焦点
    ctx.focus = {'task_id': task_id, 'batch': batch or ctx.default_alias,
                 'candidate_ids': [c['candidate_id'] for c in parsed['candidates']]}
    return _envelope(summary, ctx, refs=refs)


# ─── get_review ────────────────────────────────────────────────────────────────

def get_review(params: dict, ctx: ToolContext) -> dict:
    """候选级评审投影：gates/issues/quality + 决策 + 中文段落摘要。"""
    task_id = params.get('task_id', '') or ctx.focus.get('task_id', '')
    candidate_id = params.get('candidate_id', '')
    batch = params.get('batch') or (ctx.focus.get('batch') if task_id == ctx.focus.get('task_id') else None)
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    if not task_id:
        return _err('缺少 task_id 且无焦点')
    if not candidate_id:
        return _err('缺少 candidate_id')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    if not any(c['candidate_id'] == candidate_id for c in parsed['candidates']):
        return _err(f'候选不属于该任务：{candidate_id}')
    reviews = parsed['reviews_by_candidate'].get(candidate_id, [])
    decision = parsed['decisions_by_candidate'].get(candidate_id, {})
    if not reviews and not decision:
        return _err(f'该候选无评审记录：{candidate_id}')

    # 收集引用
    refs = []
    for r in reviews:
        for gate in r.get('gates', {}).values():
            for eid in gate.get('evidence_ids', []):
                if eid in parsed['evidence_by_id']:
                    refs.append(f'ev:{eid}')
        for issue in r.get('issues', []):
            if issue.get('issue_id'):
                refs.append(f'ri:{issue["issue_id"]}')
            for eid in issue.get('evidence_ids', []):
                if eid in parsed['evidence_by_id']:
                    refs.append(f'ev:{eid}')

    result = {'candidate_id': candidate_id, 'reviews': reviews, 'decision': decision}
    # 更新焦点
    if ctx.focus.get('task_id') == task_id and (ctx.focus.get('batch') or ctx.default_alias) == (batch or ctx.default_alias):
        ctx.focus['candidate_id'] = candidate_id
    return _envelope(result, ctx, refs=list(dict.fromkeys(refs)))


# ─── get_evidence ──────────────────────────────────────────────────────────────

_MAX_TEXT = 300

def get_evidence(params: dict, ctx: ToolContext) -> dict:
    """按 ID 或引用闭包取证据条目原文。"""
    task_id = params.get('task_id', '') or ctx.focus.get('task_id', '')
    batch = params.get('batch') or (ctx.focus.get('batch') if task_id == ctx.focus.get('task_id') else None)
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    evidence_ids = params.get('evidence_ids', [])
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 30 or any(not isinstance(x, str) for x in evidence_ids):
        return _err('evidence_ids 必须是最多 30 项的字符串列表')
    match_candidate = params.get('match_candidate', '')
    limit = _limit(params.get('limit', 12), 30)
    if not task_id:
        return _err('缺少 task_id 且无焦点')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    ev_map = parsed['evidence_by_id']

    # 确定要取哪些
    target_ids: list[str] = []
    if evidence_ids:
        target_ids = [str(x) for x in evidence_ids]
    elif match_candidate:
        cand = next((c for c in parsed['candidates'] if c['candidate_id'] == match_candidate), None)
        if not cand:
            return _err('候选不属于该任务')
        target_ids = cand.get('evidence_ids', [])
    else:
        # 默认取焦点候选的引用
        same_focus = task_id == ctx.focus.get('task_id') and (batch or ctx.default_alias) == (ctx.focus.get('batch') or ctx.default_alias)
        cid = ctx.focus.get('candidate_id', '') if same_focus else ''
        cand = next((c for c in parsed['candidates'] if c['candidate_id'] == cid), None) if cid else None
        target_ids = cand.get('evidence_ids', []) if cand else list(ev_map.keys())

    items = []
    truncated = len(target_ids) > limit
    for eid in target_ids[:limit]:
        e = ev_map.get(eid)
        if not e:
            items.append({'evidence_id': eid, 'missing': True})
            continue
        text = str(e.get('text') or '')
        cut = len(text) > _MAX_TEXT
        if cut:
            text = text[:_MAX_TEXT] + '…'
            truncated = True
        items.append({'evidence_id': eid, 'field': str(e.get('field') or ''),
                      'text': text, 'locator': str(e.get('locator') or ''),
                      'quality': str(e.get('quality') or ''), 'truncated_text': cut})

    refs = [f'ev:{i["evidence_id"]}' for i in items if not i.get('missing')]
    notices = []
    missing = [i['evidence_id'] for i in items if i.get('missing')]
    if missing:
        notices.append(f'以下证据 ID 在任务快照中不存在：{", ".join(missing[:5])}')
    return _envelope(items, ctx, refs=refs, truncated=truncated, notices=notices)


# ─── check_evidence_coverage ───────────────────────────────────────────────────

def check_evidence_coverage(params: dict, ctx: ToolContext) -> dict:
    """确定性判定：候选/评审的证据覆盖情况，不让模型自己数。"""
    task_id = params.get('task_id', '') or ctx.focus.get('task_id', '')
    same_focus = task_id == ctx.focus.get('task_id') and (not params.get('batch') or params.get('batch') == ctx.focus.get('batch'))
    candidate_id = params.get('candidate_id', '') or (ctx.focus.get('candidate_id', '') if same_focus else '')
    batch = params.get('batch') or (ctx.focus.get('batch') if task_id == ctx.focus.get('task_id') else None)
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    if not task_id or not candidate_id:
        return _err('需要 task_id 和 candidate_id')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    cand = next((c for c in parsed['candidates'] if c['candidate_id'] == candidate_id), None)
    if not cand:
        return _err(f'候选不存在：{candidate_id}')

    ev_map = parsed['evidence_by_id']
    reviews = parsed['reviews_by_candidate'].get(candidate_id, [])

    gate_refs = {}
    for r in reviews:
        for name, gate in r.get('gates', {}).items():
            gate_refs[name] = len(gate.get('evidence_ids', []))

    issue_refs = sum(len(i.get('evidence_ids', [])) for r in reviews for i in r.get('issues', []))
    candidate_refs = [eid for eid in cand.get('evidence_ids', []) if eid in ev_map]
    dangling = [eid for eid in cand.get('evidence_ids', []) if eid not in ev_map]
    review_refs = [eid for review in reviews for gate in review.get('gates', {}).values()
                   for eid in gate.get('evidence_ids', [])]
    review_refs.extend(eid for review in reviews for issue in review.get('issues', [])
                       for eid in issue.get('evidence_ids', []))
    dangling_review = list(dict.fromkeys(eid for eid in review_refs if eid not in ev_map))

    # 断言核查状态：缺失的关联断言不能当作已核查。
    claim_ids = set(cand.get('claim_ids', []))
    known_claim_ids = {str(c.get('claim_id')) for c in parsed['claims'] if c.get('claim_id')}
    missing_claims = sorted(claim_ids - known_claim_ids)
    claims_unverified = []
    for c in parsed['claims']:
        if c.get('claim_id') in claim_ids:
            v = c.get('verification')
            if not isinstance(v, dict) or not v.get('verdict') or v.get('verdict') == 'unverified':
                claims_unverified.append(str(c.get('claim_id')))

    # 引用存在只表示记录可检索，不证明支持力度或证据充分。
    verdict_hint = 'record_gap' if dangling or dangling_review or missing_claims else ('references_present' if candidate_refs else 'none')
    return _envelope({
        'gate_refs': gate_refs, 'issue_refs': issue_refs,
        'candidate_refs': len(candidate_refs), 'dangling_refs': dangling,
        'dangling_review_refs': dangling_review, 'missing_claims': missing_claims,
        'claims_unverified': claims_unverified, 'verdict_hint': verdict_hint,
    }, ctx, refs=[f'ev:{eid}' for eid in candidate_refs])


# ─── compare_candidates ────────────────────────────────────────────────────────

def compare_candidates(params: dict, ctx: ToolContext) -> dict:
    """各候选一行摘要：标题/决策/失败闸门/问题码/是否推荐。"""
    task_id = params.get('task_id', '') or ctx.focus.get('task_id', '')
    batch = params.get('batch') or (ctx.focus.get('batch') if task_id == ctx.focus.get('task_id') else None)
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    if not task_id:
        return _err('缺少 task_id 且无焦点')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    recommended = str(parsed['route'].get('recommended_candidate_id') or '')
    rows = []
    refs = []
    for c in parsed['candidates']:
        cid = c['candidate_id']
        decision = parsed['decisions_by_candidate'].get(cid, {})
        failed_gates = []
        issue_codes = []
        for r in parsed['reviews_by_candidate'].get(cid, []):
            for name, gate in r.get('gates', {}).items():
                if gate.get('status') not in ('pass', ''):
                    failed_gates.append(f'{name}:{gate["status"]}')
            for issue in r.get('issues', []):
                if issue.get('issue_code'):
                    issue_codes.append(issue['issue_code'])
                    if issue.get('issue_id'):
                        refs.append(f'ri:{issue["issue_id"]}')
        rows.append({
            'candidate_id': cid, 'title': c['title'],
            'decision': decision.get('status', ''), 'decision_reason': decision.get('reason', ''),
            'failed_gates': failed_gates, 'issue_codes': issue_codes,
            'is_recommended': cid == recommended,
        })
    return _envelope(rows, ctx, refs=refs)


# ─── explain_record ────────────────────────────────────────────────────────────

def explain_record(params: dict, ctx: ToolContext) -> dict:
    """单条记录全文（issue/gate/claim/evidence/highlight），上限 2K 字符。"""
    kind = params.get('kind', '')
    record_id = params.get('id', '')
    task_id = params.get('task_id', '') or ctx.focus.get('task_id', '')
    batch = params.get('batch') or (ctx.focus.get('batch') if task_id == ctx.focus.get('task_id') else None)
    if not batch and len(ctx.db_aliases) > 1:
        return _err('存在多个批次，请先明确指定 batch，不自动选择。')
    if not kind or not record_id or not task_id:
        return _err('需要 kind、id 和 task_id')
    db = ctx.resolve_db(batch)
    parsed = load_task(db, task_id)
    if parsed is None:
        return _err(f'任务不存在：{task_id}')

    record = None
    if kind == 'evidence':
        record = parsed['evidence_by_id'].get(record_id)
    elif kind == 'claim':
        record = next((c for c in parsed['claims'] if c.get('claim_id') == record_id), None)
    elif kind == 'highlight':
        record = next((h for h in parsed['highlights'] if h.get('highlight_id') == record_id), None)
    elif kind == 'issue':
        for reviews in parsed['reviews_by_candidate'].values():
            for r in reviews:
                record = next((i for i in r.get('issues', []) if i.get('issue_id') == record_id), None)
                if record:
                    break
            if record:
                break
    if not record:
        return _err(f'找不到 {kind}:{record_id}')

    text = json.dumps(record, ensure_ascii=False, indent=2)
    truncated = len(text) > 2000
    if truncated:
        text = text[:2000] + '\n…（已截断）'
    ref = f'{kind[0]}v:{record_id}' if kind == 'evidence' else f'{"ri" if kind == "issue" else "cl"}:{record_id}'
    return _envelope({'kind': kind, 'id': record_id, 'full_text': text}, ctx, refs=[ref], truncated=truncated)


# ─── list_batches ──────────────────────────────────────────────────────────────

def list_batches(params: dict, ctx: ToolContext) -> dict:
    """可见 db 清册：别名、任务数、最新更新时间。不暴露文件系统路径。"""
    out = []
    for alias in ctx.db_aliases:
        try:
            tasks = list_tasks_from_db(ctx.db_aliases[alias])
            latest = max((t['updated_at'] for t in tasks), default='')
            out.append({'batch': alias, 'task_count': len(tasks), 'latest_updated': latest})
        except Exception as e:
            out.append({'batch': alias, 'error': type(e).__name__})
    return _envelope(out, ctx)


# ─── resolve_reference ─────────────────────────────────────────────────────────

def resolve_reference(params: dict, ctx: ToolContext) -> dict:
    """把指代短语映射到焦点内实体；不确定时返回 needs_disambiguation。"""
    phrase = params.get('phrase', '')
    focus = ctx.focus
    if not focus.get('task_id'):
        return _envelope({'resolved': None, 'needs_disambiguation': True,
                          'reason': '当前无焦点任务，请先用 find_tasks 定位'}, ctx)
    candidates = focus.get('candidate_ids', [])
    current_cand = focus.get('candidate_id', '')
    if current_cand not in candidates:
        current_cand = ''
    # 简单规则：只有一个候选时直接解析；多个时要求消歧
    if len(candidates) == 1 or (current_cand and any(k in phrase for k in ('它', '这条', '这个', '该'))):
        return _envelope({'resolved': {'task_id': focus['task_id'], 'candidate_id': current_cand or candidates[0]},
                          'needs_disambiguation': False}, ctx)
    return _envelope({'resolved': None, 'needs_disambiguation': True,
                      'reason': f'焦点任务有 {len(candidates)} 个候选，请指定哪一个',
                      'options': candidates}, ctx)


# ─── 注册表与 schema ───────────────────────────────────────────────────────────

TOOLS = {
    'find_tasks': find_tasks,
    'get_task': get_task,
    'get_review': get_review,
    'get_evidence': get_evidence,
    'check_evidence_coverage': check_evidence_coverage,
    'compare_candidates': compare_candidates,
    'explain_record': explain_record,
    'list_batches': list_batches,
    'resolve_reference': resolve_reference,
}

TOOL_SCHEMAS = [
    {'type': 'function', 'function': {'name': 'find_tasks', 'description': '按视频ID/标题文本/状态搜索任务。跨批次命中会提示消歧。', 'parameters': {'type': 'object', 'properties': {'video_id': {'type': 'string'}, 'title_text': {'type': 'string'}, 'status': {'type': 'string'}, 'batch': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'get_task', 'description': '获取单个任务概览：状态、路由、候选列表、主旨摘要。', 'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'batch': {'type': 'string'}}, 'required': ['task_id']}}},
    {'type': 'function', 'function': {'name': 'get_review', 'description': '获取某候选的完整评审投影：gates/issues/quality/decision。', 'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'candidate_id': {'type': 'string'}, 'batch': {'type': 'string'}}, 'required': ['candidate_id']}}},
    {'type': 'function', 'function': {'name': 'get_evidence', 'description': '按ID或候选引用闭包取证据原文（≤300字/条）。', 'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'evidence_ids': {'type': 'array', 'items': {'type': 'string'}}, 'match_candidate': {'type': 'string'}, 'limit': {'type': 'integer'}, 'batch': {'type': 'string'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'check_evidence_coverage', 'description': '确定性判定候选的证据覆盖：gate引用数/悬空引用/未核查断言。', 'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'candidate_id': {'type': 'string'}, 'batch': {'type': 'string'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'compare_candidates', 'description': '同任务所有候选对比：决策/失败闸门/问题码/是否推荐。', 'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'batch': {'type': 'string'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'explain_record', 'description': '取单条记录全文（evidence/claim/issue/highlight），≤2K字符。', 'parameters': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['evidence', 'claim', 'issue', 'highlight']}, 'id': {'type': 'string'}, 'task_id': {'type': 'string'}, 'batch': {'type': 'string'}}, 'required': ['kind', 'id']}}},
    {'type': 'function', 'function': {'name': 'list_batches', 'description': '列出可查询的批次库及任务数。', 'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'resolve_reference', 'description': '把指代短语（那条/上一个/它）映射到焦点实体；不确定返回需消歧。', 'parameters': {'type': 'object', 'properties': {'phrase': {'type': 'string'}}, 'required': ['phrase']}}},
]
