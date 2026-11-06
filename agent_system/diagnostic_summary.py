"""将只读诊断记录整理为可追溯摘要；不执行模型、检索或事实核查。"""
from __future__ import annotations

import json
from typing import Any


def _sections(result: dict[str, Any]) -> dict[str, list[Any]]:
    parsed: dict[str, list[Any]] = {}
    for section in result.get('sections', []):
        if not isinstance(section, dict) or not isinstance(section.get('heading'), str):
            continue
        items = section.get('items')
        if not isinstance(items, list):
            continue
        values = parsed.setdefault(section['heading'], [])
        for item in items:
            if not isinstance(item, str):
                continue
            try:
                values.append(json.loads(item))
            except (ValueError, TypeError):
                values.append(item)
    return parsed


def _records(sections: dict[str, list[Any]], heading: str) -> list[dict[str, Any]]:
    return [item for item in sections.get(heading, []) if isinstance(item, dict)]


def _text(value: Any) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def _refs(value: Any) -> list[str]:
    return [str(v) for v in value if isinstance(v, (str, int)) and not isinstance(v, bool)] if isinstance(value, list) else []


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """返回 paragraphs（中文段落）、evidence（引用定位）和 notices（原提示）。

    evidence 的 section 指向诊断原分节，evidence_id 为原始引用；未匹配的引用保留，
    不将记录中的核查或评审结果当作本函数的事实裁决。
    """
    scenario = result.get('scenario')
    if scenario not in {'understanding', 'review', 'evidence'}:
        raise ValueError('scenario 必须为 understanding、review 或 evidence')
    sections = _sections(result)
    paragraphs: list[str] = []
    references: list[dict[str, Any]] = []
    evidence_index: dict[str, tuple[str, dict[str, Any]]] = {}
    for heading, items in sections.items():
        for item in items:
            if isinstance(item, dict) and item.get('evidence_id'):
                evidence_index.setdefault(str(item['evidence_id']), (heading, item))

    def cite(ids: Any, source: str, reason: str = '') -> None:
        for eid in _refs(ids):
            match = evidence_index.get(eid)
            entry: dict[str, Any] = {'evidence_id': eid, 'source_section': source, 'reason': reason,
                                     'matched': match is not None}
            if match:
                entry['section'] = match[0]
                for key in ('text', 'locator', 'field', 'quality'):
                    if key in match[1]:
                        entry[key] = match[1][key]
            references.append(entry)

    candidates = result.get('candidates') if isinstance(result.get('candidates'), list) else []
    if scenario != 'understanding' and not result.get('candidate_id') and len(candidates) > 1:
        paragraphs.append('存在多个候选但尚未选择，请指定 candidate_id；不代选，也不推断任务级推荐。')
    elif scenario == 'understanding':
        for heading, label in [('主旨契约', '生成侧历史模型主旨'), ('评审主旨契约', '评审侧历史模型主旨')]:
            records = _records(sections, heading)
            if not records:
                paragraphs.append(f'{label}未记录。')
                continue
            for record in records:
                parts = [f'{name}：{_text(record.get(key))}' for key, name in
                         [('topic', '主题'), ('delivery_type', '交付类型'), ('core_promise', '核心承诺')]
                         if _text(record.get(key))]
                for key, name in [('required_semantics', '必保语义'), ('qualifiers', '限定'), ('pending_questions', '待核问题')]:
                    vals = _refs(record.get(key))
                    if vals:
                        parts.append(f'{name}：{"、".join(vals)}')
                paragraphs.append(f'{label}记录：' + ('；'.join(parts) if parts else '缺少可摘要字段') + '。这只是历史理解，不是视频事实结论。')
                cite(record.get('evidence_ids'), heading, '主旨契约所列依据')
        claims = _records(sections, '理解断言')
        if not claims:
            paragraphs.append('未记录理解断言，核查状态未知。')
        else:
            for claim in claims:
                verification = claim.get('verification') if isinstance(claim.get('verification'), dict) else {}
                verdict = _text(verification.get('verdict')) or '未记录'
                paragraphs.append(f"模型断言 {_text(claim.get('claim_id')) or '未标识'}：{_text(claim.get('text')) or '缺少内容'}；历史核查结果：{verdict}。")
                cite(claim.get('evidence_refs'), '理解断言', '断言所列依据')
    elif scenario == 'review':
        reviews = _records(sections, '候选级评审')
        if not reviews:
            paragraphs.append('未记录该候选评审。')
        for review in reviews:
            details = []
            if _text(review.get('decision')):
                details.append('评审决策：' + _text(review['decision']))
            gates = review.get('gates') if isinstance(review.get('gates'), dict) else {}
            for name, gate in gates.items():
                if isinstance(gate, dict):
                    details.append(f"{name}：{_text(gate.get('status')) or '状态未记录'}，原因：{_text(gate.get('reason')) or '未记录'}")
                    cite(gate.get('evidence_ids'), '候选级评审', f'{name}：{_text(gate.get("reason")) or "原因未记录"}')
            issues = review.get('issues') if isinstance(review.get('issues'), list) else []
            for issue in issues:
                if isinstance(issue, dict):
                    reason = _text(issue.get('reason')) or '原因未记录'
                    details.append(f"问题 {_text(issue.get('issue_code')) or _text(issue.get('issue_id')) or '未标识'}：{reason}")
                    cite(issue.get('evidence_ids'), '候选级评审', reason)
            paragraphs.append('候选级评审记录：' + ('；'.join(details) if details else '缺少状态和原因') + '。')
        decisions = _records(sections, '候选级决策')
        if not decisions:
            paragraphs.append('未记录该候选决策。')
        for decision in decisions:
            parts = [f'{label}：{_text(decision.get(key))}' for key, label in [('status', '状态'), ('decision', '决策'), ('reason', '原因')] if _text(decision.get(key))]
            paragraphs.append('候选级决策记录：' + ('；'.join(parts) if parts else '缺少状态和原因') + '。')
        routes = _records(sections, '任务级路由（不等于候选级评审）')
        if not routes:
            paragraphs.append('未记录任务级路由。')
        for route in routes:
            paragraphs.append(f"任务级路由记录：动作 {_text(route.get('action')) or '未记录'}；推荐候选 {_text(route.get('recommended_candidate_id')) or '未记录'}；原因 {_text(route.get('reason')) or '未记录'}。任务级推荐不等于当前候选的评审结果。")
    else:
        selected = _records(sections, '候选引用')
        if not selected:
            paragraphs.append('未选择候选或没有候选，无法列出候选引用。')
        for candidate in selected:
            ids = _refs(candidate.get('evidence_ids'))
            paragraphs.append(f"候选 {_text(candidate.get('candidate_id')) or '未标识'} 所列证据：{ '、'.join(ids) if ids else '未记录' }。")
            cite(ids, '候选引用', '候选所列依据')
        claims = _records(sections, '候选关联断言（模型抽取，非事实裁决）')
        if not claims:
            paragraphs.append('未记录或无法匹配候选关联断言。')
        for claim in claims:
            verification = claim.get('verification') if isinstance(claim.get('verification'), dict) else {}
            paragraphs.append(f"候选关联模型断言 {_text(claim.get('claim_id')) or '未标识'}：{_text(claim.get('text')) or '缺少内容'}；历史核查结果：{_text(verification.get('verdict')) or '未记录'}。")
            cite(claim.get('evidence_refs'), '候选关联断言（模型抽取，非事实裁决）', '断言所列依据')
        if not _records(sections, '候选关联证据原文、定位与质量'):
            paragraphs.append('未记录或无法匹配候选关联证据原文。')

    if '问题字面检索' in sections:
        matches = sections['问题字面检索']
        if matches == ['未在检索范围内找到完全匹配。'] or not matches:
            paragraphs.append('问题的完整字面检索未在既定范围内找到匹配；这不代表内容不存在。')
        else:
            paragraphs.append(f'问题的完整字面检索返回 {len(matches)} 条记录；请查看原诊断的「问题字面检索」。')
        paragraphs.append('问题仅作区分大小写的完整字面匹配，不进行自然语言理解或回答。')
    paragraphs.append('素材引用、历史评审和历史核查均为已有记录，引用不等于事实确证；需结合原文独立核验。')
    notices = [n for n in result.get('notices', []) if isinstance(n, str)] if isinstance(result.get('notices'), list) else []
    return {'scenario': scenario, 'task_id': result.get('task_id'), 'candidate_id': result.get('candidate_id'),
            'paragraphs': paragraphs, 'evidence': references, 'notices': notices}
