"""冻结批次人工评审：原任务只读，意见按候选独立持久保存，不触发模型或案例学习。

三候选模式：每个视频从 state.delivery_candidates（三槽：source_rewrite / core_summary / core_hook）
读取最多三条，每条独立三档判定、独立改稿与撤回。旧单候选批次仍可读，不迁移、不覆盖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .batch_review_page import PAGE


def title_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# 判定口径：frozen 是本批冻结映射里的候选（计入冻结评审进度与冻结指标），
# diagnostic 是状态里或任务专属表里留存、但未冻结的候选（只进诊断口径账本）。
FROZEN_SCOPE = 'frozen'
DIAGNOSTIC_SCOPE = 'diagnostic'
DECISION_SCOPES = {FROZEN_SCOPE, DIAGNOSTIC_SCOPE}


def _decision_scope(payload) -> str:
    """取判定口径。旧记录没有 scope 字段——当时只有冻结候选可评，缺失即冻结。"""
    scope = str((payload or {}).get('scope') or '')
    return scope if scope in DECISION_SCOPES else FROZEN_SCOPE


REASON_TEXT = {
    "G4_LENGTH_EXCEEDED": "标题超过字数上限",
    "G4_ENDING_QUESTION_MARK": "标题不能以问号结尾",
    "G4_PREFIX_MISSING": "缺少平台标注前缀",
    "G4_PREFIX_BUDGET_EXHAUSTED": "前缀占满字数预算",
    "G4_EMPTY_TITLE": "标题为空",
    "G4_SCHEMA_INVALID": "模型输出结构不合法",
    "G4_REQUIRED_FIELD_MISSING": "缺少必填字段",
    "G4_INVALID_REFERENCE": "引用了任务范围外的对象",
    "G4_META_PHRASE": "标题在描述视频怎么说，而非陈述内容",
    "G4_VERIFICATION_CONCLUSION": "标题写了审核结论",
    "G4_INCOMPLETE_ATTRIBUTION": "来源前缀缺少归属动词",
    "G4_NUMERIC_CONFLICT": "数字没有引用证据支持",
    "G1_MISSING_LIMIT": "引用了需限定的陈述，但标题没表达限定",
}


def _blocking_reasons(candidate):
    """把机械检查错误翻成人话，评审台据此说明「这条为什么没交付」。"""
    checks = candidate.get("mechanical_checks") or {}
    out = []
    for err in checks.get("errors") or []:
        code = str(err.get("code") or "")
        out.append({"code": code, "text": REASON_TEXT.get(code, str(err.get("reason") or code))})
    # 待核不是判否，但同样是「没交付」，评审人得知道该查哪一条限定、为什么机械层判不了。
    # 不显示原因，评审台就只剩一个「待核」标签，等于让人重读整个状态。
    for item in checks.get("limit_pending") or []:
        limit = str(item.get("limit") or "")
        reason = str(item.get("reason") or "限定需要人工确认")
        out.append({"code": "G1_LIMIT_PENDING", "text": f"待核：{reason}｜要求：{limit}"})
    return out


def _length_excluded(item, state, candidate):
    """只排除明确标记超长的同身份候选，不推断其他机械错误。"""
    cid = str(candidate.get('candidate_id') or '')
    title = str(candidate.get('rendered_title') or candidate.get('headline') or '')
    pool = ((state.get('delivery_candidates') or []) + (state.get('candidates') or [])
            + (state.get('pending_review_candidates') or []) + (state.get('_review_candidate_pool') or []) + [candidate])
    for c in pool:
        if (str(c.get('candidate_id') or '') == cid
                and str(c.get('rendered_title') or c.get('headline') or '') == title
                and any(e.get('code') == 'G4_LENGTH_EXCEEDED' for e in (c.get('mechanical_checks') or {}).get('errors') or [])):
            return True
    return any(str(d.get('candidate_id') or '') == cid and str(d.get('title') or '') == title
               and 'G4_LENGTH_EXCEEDED' in (d.get('codes') or [])
               for d in item.get('mechanical_dropped') or [])


SOURCE_ONLY_QUALITIES = {"source", "model_derived_uncalibrated"}


def _claim_points(state, candidate):
    """这条标题用到的陈述里，哪些是「说不清楚真假」或「必须带限定」的。

    评审人看不懂 issue_code，需要的是人话：哪句话不可信、为什么不可信、标题该怎么处理。
    """
    claims = {str(c.get("claim_id")): c for c in ((state.get("understanding") or {}).get("claims") or [])}
    decisions = {str(d.get("claim_id")): d for d in ((state.get("boundary") or {}).get("decisions") or [])}
    evidence = {str(e.get("evidence_id")): e for e in ((state.get("video") or {}).get("evidence") or [])}
    points = []
    for raw_id in candidate.get("claim_ids") or []:
        claim_id = str(raw_id)
        claim = claims.get(claim_id) or {}
        decision = decisions.get(claim_id) or {}
        reasons = []
        if decision.get("verification_required"):
            reasons.append("材料里没有能核实的来源：这条只能写成转述，或者干脆不写。")
        if decision.get("attribution_required"):
            phrase = str(decision.get("required_source_phrase") or "").strip()
            reasons.append(f"必须交代是谁说的（{phrase or '需要写明信源'}），标题里看不出来。")
        for limit in decision.get("required_limits") or []:
            reasons.append(f"必须带上限定条件：{limit}")
        for forbidden in decision.get("forbidden_inferences") or []:
            reasons.append(f"不能这样推断：{forbidden}")
        refs = [str(x) for x in (claim.get("evidence_refs") or [])]
        if refs and all(str((evidence.get(ref) or {}).get("quality") or "") in SOURCE_ONLY_QUALITIES for ref in refs):
            reasons.append("这条只有原题或模型描述撑着，没有 ASR／字幕原文。")
        if reasons:
            points.append({"claim_id": claim_id, "claim": str(claim.get("text") or ""), "points": list(dict.fromkeys(reasons))})
    return points


def _review_issues(state, candidate_id):
    for review in state.get("reviews") or []:
        if str(review.get("candidate_id") or "") != str(candidate_id):
            continue
        out = []
        for issue in review.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("issue_code") or "")
            # 只展示内容层意见（G1/G2/G3/Q*）。机械层的 G4 已经单独用中文说明过，
            # 再列一遍只会把"参数"塞回评审人眼前。
            if not code.startswith(("G1_", "G2_", "G3_", "Q1_", "Q2_", "Q3_")):
                continue
            out.append({"code": code, "text": str(issue.get("reason") or ""), "action": str(issue.get("action") or "")})
        return out
    return []


def _rewrite_chain(state, candidate):
    """这条候选是从哪条改写来的：给出上一轮的标题，评审人才能判断重写有没有改进。"""
    pool = {str(c.get("candidate_id")): c for c in (state.get("candidates") or [])}
    parent_id = str(candidate.get("parent_candidate_id") or "")
    parent = pool.get(parent_id)
    # generation_round 记的是「已经被评审打回重写了几次」，不是第几次生成。
    # 标成"第 3 轮"会被读成重写了三次，实际是重写两次之后的第 3 次生成。
    rewrites = max(0, int(candidate.get("generation_round") or 0))
    label = "一次性生成（没被打回）" if rewrites == 0 else f"重写 {rewrites} 次后生成"
    parent_issues = _review_issues(state, parent_id) if parent_id else []
    if not parent_issues and parent:
        # 内容层没有留下意见时，退回机械检查结论：至少说明"为什么被打回"，
        # 而不是给一个空白的打回原因。
        parent_issues = _blocking_reasons(parent)
    return {
        "rewrites": rewrites,
        "generation": rewrites + 1,
        "label": label,
        "parent_id": parent_id,
        "parent_title": str((parent or {}).get("rendered_title") or (parent or {}).get("headline") or ""),
        "parent_issues": [item["text"] for item in parent_issues if item.get("text")],
        "change_reason": str(candidate.get("change_reason") or ""),
    }


class BatchReviewStore:
    def __init__(self, batch_dir: str | Path, feedback_db: str | Path | None = None):
        self.batch_dir = Path(batch_dir).resolve()
        self.source_db = self.batch_dir / 'agent.db'
        self.mapping = json.loads((self.batch_dir / 'review_mapping.json').read_text(encoding='utf-8'))
        self.by_task = {item['task_id']: item for item in self.mapping}
        if len(self.by_task) != len(self.mapping):
            raise ValueError('批次任务标识重复')
        self.legacy_db = (self.batch_dir / 'human_feedback.db').resolve()
        self.isolate_legacy = self.batch_dir.name == 'pro_juflow_20_20261102'
        self.feedback_db = (Path(feedback_db) if feedback_db else self.batch_dir / 'human_feedback_v2.db').resolve()
        if self.feedback_db in {self.source_db, self.legacy_db}:
            raise ValueError('反馈库不得覆盖原任务库或旧反馈库')
        self.feedback_db.parent.mkdir(parents=True, exist_ok=True)
        with self._feedback_conn() as conn:
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

    @contextmanager
    def _feedback_conn(self):
        conn = sqlite3.connect(self.feedback_db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _source(self, task_id):
        if task_id not in self.by_task:
            raise ValueError('任务不属于当前批次')
        uri = self.source_db.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('原任务不存在')
        return dict(row), json.loads(row['state_json'])

    def _candidates(self, item, state):
        """返回该视频的三槽候选（最多3条），每条含 candidate_id/rendered_title/slot_id。"""
        # 只能展示映射冻结的候选；状态中的其他候选不能替代冻结样本。
        if 'candidates' in item:
            out, seen = [], set()
            pool = (state.get('delivery_candidates') or []) + (state.get('candidates') or []) + (state.get('pending_review_candidates') or [])
            for frozen in item.get('candidates') or []:
                cid = str(frozen.get('candidate_id') or '')
                expected = str(frozen.get('title_sha256') or frozen.get('title_hash') or '')
                matches = [c for c in pool if str(c.get('candidate_id') or '') == cid and title_hash(str(c.get('rendered_title') or c.get('headline') or '')) == expected]
                if not cid or not expected or not matches or (cid, expected) in seen:
                    raise ValueError('冻结候选身份或标题哈希不一致')
                c = matches[0]
                rendered = str(c.get('rendered_title') or c.get('headline') or '')
                seen.add((cid, expected))
                decision = next((d for d in state.get('candidate_decisions', []) if d.get('candidate_id') == cid), {})
                route = state.get('route') or {}
                # 系统判定缺失（R01 崩溃）时回落到冻结时记下的状态，
                # 否则界面只能说「未知」，看不出这批候选到底经过评审没有。
                status = ('recommended' if route.get('recommended_candidate_id') == cid
                          else (decision.get('status') or frozen.get('status') or 'unknown'))
                reasons = _blocking_reasons(c)
                out.append({'candidate_id': cid, 'rendered_title': rendered, 'title_hash': expected,
                            'headline': str(c.get('headline') or rendered), 'status': status,
                            'slot_id': str(c.get('slot_id') or ''), 'thesis_id': str(c.get('thesis_id') or ''),
                            'prefix_labels': list(c.get('prefix_labels') or []),
                            'title_length': len(rendered), 'blocking_reasons': reasons,
                            'generation': _rewrite_chain(state, c),
                            'claim_points': _claim_points(state, c),
                            'review_issues': _review_issues(state, cid),
                            'deliverable': not reasons})
            return out
        # 回退：单候选 mapping
        cid = item.get('candidate_id')
        if not cid:
            return []
        pool = (state.get('candidates') or []) + (state.get('pending_review_candidates') or [])
        for candidate in reversed(pool):
            rendered = str(candidate.get('rendered_title') or candidate.get('headline') or '')
            if candidate.get('candidate_id') == cid and rendered == item.get('headline') and (not item.get('title_sha256') or title_hash(rendered) == item['title_sha256']):
                return [{'candidate_id': cid, 'rendered_title': rendered,
                         'headline': str(candidate.get('headline') or rendered),
                         'slot_id': str(candidate.get('slot_id') or ''), 'thesis_id': '',
                         'prefix_labels': list(candidate.get('prefix_labels') or [])}]
        return []

    def _legacy_decisions(self):
        """旧 decisions 表只读补齐；隔离批次与缺表批次返回空。"""
        if self.isolate_legacy or not self.legacy_db.exists():
            return []
        with closing(sqlite3.connect(self.legacy_db.as_uri() + '?mode=ro', uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():
                return []
            return [json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM decisions ORDER BY task_id')]

    def feedback(self, scope=None):
        """人工反馈账本。

        decisions / reviewed 默认仍是冻结口径（诊断判定不得计入冻结进度）；
        scope='diagnostic' 时按诊断口径过滤，同时始终另给 frozen_decisions /
        diagnostic_decisions 两个清单，便于调用方自行分辨。
        """
        if scope not in (None, FROZEN_SCOPE, DIAGNOSTIC_SCOPE):
            raise ValueError('无效判定口径')
        with self._feedback_conn() as conn:
            values = [json.loads(row[0]) for row in conn.execute(
                'SELECT payload_json FROM candidate_decisions_v2 WHERE batch_id=? ORDER BY task_id, candidate_id',
                (self.batch_dir.name,))]
        for value in values:
            value.setdefault('scope', _decision_scope(value))
        frozen = [v for v in values if _decision_scope(v) == FROZEN_SCOPE]
        diagnostic = [v for v in values if _decision_scope(v) == DIAGNOSTIC_SCOPE]
        excluded = set()
        for item in self.mapping:
            _, state = self._source(item['task_id'])
            state['_review_candidate_pool'] = self._task_candidate_pool(item, state)
            for c in state['_review_candidate_pool']:
                if _length_excluded(item, state, c):
                    title = str(c.get('rendered_title') or c.get('headline') or '')
                    excluded.add((item['task_id'], str(c.get('candidate_id') or ''), title_hash(title)))
        def eligible_count(rows):
            return sum((d.get('task_id'), d.get('candidate_id'), d.get('title_hash')) not in excluded for d in rows)
        chosen = diagnostic if scope == DIAGNOSTIC_SCOPE else frozen
        return {'batch': self.batch_dir.name, 'total_videos': len(self.mapping),
                'reviewable': 0, 'reviewed': eligible_count(chosen), 'decisions': chosen,
                'scope': scope or FROZEN_SCOPE,
                'frozen_decisions': frozen, 'diagnostic_decisions': diagnostic,
                'reviewed_frozen': eligible_count(frozen), 'reviewed_diagnostic': eligible_count(diagnostic),
                'legacy_decisions_read_only': self._legacy_decisions(),
                'feedback_database': str(self.feedback_db)}

    def _decisions_for_display(self, scope=FROZEN_SCOPE) -> dict:
        """展示用判定：新库为主，旧 decisions 表只读补齐（只补冻结口径）。

        旧批次的人评还没迁到 v2 时，评审台必须仍显示"已评"，否则同一批判定会被
        当成未评再判一遍。旧记录只用于显示，不写回、不参与提交。"""
        fb = self.feedback(scope)
        merged = {(d['task_id'], d.get('candidate_id'), d.get('title_hash')): d for d in fb['decisions']}
        if scope != DIAGNOSTIC_SCOPE:
            for d in fb.get('legacy_decisions_read_only', []):
                key = (d.get('task_id'), d.get('candidate_id'), d.get('title_hash'))
                merged.setdefault(key, {**d, 'legacy': True})
        return merged

    def _task_candidate_pool(self, item, state):
        """该任务范围内的候选池：状态三处候选 + 任务专属候选表（只读，按 task_id）。

        不能使用可能跨任务碰撞的全局 candidates 表。"""
        pool = ((state.get('delivery_candidates') or []) + (state.get('candidates') or [])
                + (state.get('pending_review_candidates') or []))
        with closing(sqlite3.connect(self.source_db.as_uri() + '?mode=ro', uri=True)) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates_by_task'").fetchone()
            if exists:
                for (payload,) in conn.execute('SELECT payload_json FROM candidates_by_task WHERE task_id=? ORDER BY candidate_id', (item['task_id'],)):
                    candidate = json.loads(payload)
                    if isinstance(candidate, dict):
                        pool.append(candidate)
        return pool

    def _diagnostic_candidates(self, item, state, frozen):
        """展示原状态及任务专属候选表中的非冻结候选，不改变评审口径。"""
        seen = {(c['candidate_id'], title_hash(c['rendered_title'])) for c in frozen}
        dropped = {(str(c.get('candidate_id') or ''), str(c.get('title') or '')): c
                   for c in item.get('mechanical_dropped') or []}
        decisions = {str(d.get('candidate_id') or ''): d for d in state.get('candidate_decisions') or []}
        result = []
        for c in self._task_candidate_pool(item, state):
            cid = str(c.get('candidate_id') or '')
            title = str(c.get('rendered_title') or c.get('headline') or '')
            key = (cid, title_hash(title))
            if not cid or not title or key in seen:
                continue
            seen.add(key)
            drop = dropped.get((cid, title)) or {}
            reasons = _blocking_reasons(c)
            codes = list(drop.get('codes') or [])
            status = str((decisions.get(cid) or {}).get('status') or ('blocked' if codes or reasons else 'unknown'))
            result.append({'candidate_id': cid, 'rendered_title': title, 'title_hash': key[1],
                           'slot_id': str(c.get('slot_id') or ''),
                           'prefix_labels': list(c.get('prefix_labels') or []),
                           'generation_round': c.get('generation_round'), 'status': status,
                           'blocking_reasons': reasons, 'codes': codes})
        return result

    def _diagnostic_candidate(self, item, state, frozen, candidate_id):
        """冻结匹配失败时的回落：在诊断候选池里按 candidate_id 找唯一一条。

        身份一律由服务端真实标题的哈希确定；客户端传来的哈希不参与识别。同 id 出现
        多个标题版本时说明身份不唯一，直接判为找不到而不是猜一条。"""
        if not candidate_id:
            return None
        frozen_keys = {(c['candidate_id'], c.get('title_hash') or title_hash(c['rendered_title'])) for c in frozen}
        matches, seen = [], set()
        for c in self._task_candidate_pool(item, state):
            cid = str(c.get('candidate_id') or '')
            if cid != candidate_id:
                continue
            rendered = str(c.get('rendered_title') or c.get('headline') or '')
            digest = title_hash(rendered)
            key = (cid, digest)
            if not rendered or key in frozen_keys or key in seen:
                continue
            seen.add(key)
            matches.append({'candidate_id': cid, 'rendered_title': rendered, 'title_hash': digest,
                            'headline': str(c.get('headline') or rendered),
                            'slot_id': str(c.get('slot_id') or ''),
                            'prefix_labels': list(c.get('prefix_labels') or [])})
        return matches[0] if len(matches) == 1 else None

    def list_tasks(self):
        decisions = self._decisions_for_display()
        diagnostic_decisions = self._decisions_for_display(DIAGNOSTIC_SCOPE)
        tasks = []
        for item in self.mapping:
            _, state = self._source(item['task_id'])
            state['_review_candidate_pool'] = self._task_candidate_pool(item, state)
            frozen = self._candidates(item, state)
            diagnostic = self._diagnostic_candidates(item, state, frozen)
            candidates = [c for c in frozen if not _length_excluded(item, state, c)]
            excluded = [c for c in frozen + diagnostic if _length_excluded(item, state, c)]
            diagnostic = [c for c in diagnostic if not _length_excluded(item, state, c)]
            matched = [decisions.get((item['task_id'], c['candidate_id'], title_hash(c['rendered_title']))) for c in candidates]
            done = sum(d is not None for d in matched)
            # 诊断口径单独计数；冻结进度 review_status 只看冻结候选。
            diagnostic_done = sum(
                diagnostic_decisions.get((item['task_id'], c['candidate_id'], c['title_hash'])) is not None
                for c in diagnostic)
            legacy = matched[0] if matched else None
            tasks.append({'task_id': item['task_id'], 'video_id': item['video_id'],
                          'blind_id': item['blind_id'], 'category': item['category'],
                          'title': candidates[0]['rendered_title'] if candidates else ('（已产出但超长拦截）' if excluded else '（未产出标题）'),
                          'candidate_count': len(candidates), 'diagnostic_count': len(diagnostic),
                          'length_excluded_count': len(excluded),
                          'diagnostic_reviewed': diagnostic_done,
                          'source_status': item.get('task_status') or '',
                          'blocked_count': sum(1 for c in candidates if not c.get('deliverable', True)),
                          'mechanical_dropped': list(item.get('mechanical_dropped') or []),
                          'contract_disagreement': state.get('contract_disagreement') or None,
                          'review_status': 'done' if candidates and done == len(candidates) else ('unreviewed' if candidates else 'no_candidates'),
                          'decision': legacy})
        return tasks

    def get_task(self, task_id):
        item = self.by_task.get(task_id)
        if not item:
            raise ValueError('任务不属于当前批次')
        row, state = self._source(task_id)
        state['_review_candidate_pool'] = self._task_candidate_pool(item, state)
        candidates = self._candidates(item, state)
        decisions = list(self._decisions_for_display().values())
        for c in candidates:
            c['title_hash'] = title_hash(c['rendered_title'])
            c['decision'] = next((d for d in decisions if d['task_id'] == task_id and d.get('candidate_id') == c['candidate_id'] and d.get('title_hash') == c['title_hash']), None)
        diagnostic = self._diagnostic_candidates(item, state, candidates)
        diagnostic_decisions = self._decisions_for_display(DIAGNOSTIC_SCOPE)
        for c in diagnostic:
            # 诊断候选也走冻结同一条链路：哈希由服务端标题算，判定按口径分别取。
            c['decision'] = diagnostic_decisions.get((task_id, c['candidate_id'], c['title_hash']))
        excluded = [c for c in candidates + diagnostic if _length_excluded(item, state, c)]
        candidates = [c for c in candidates if not _length_excluded(item, state, c)]
        diagnostic = [c for c in diagnostic if not _length_excluded(item, state, c)]
        video = state.get('video') or {}
        # 兼容旧的「每任务一条选题」调用方：顶层给出当前选中候选与它的判定。
        primary = candidates[0] if candidates else None
        return {'task_id': task_id, 'video_id': item['video_id'], 'blind_id': item['blind_id'],
                'category': item['category'], 'source_status': row['status'],
                'title': primary['rendered_title'] if primary else ('（已产出但超长拦截）' if excluded else '（未产出标题）'),
                'candidate_id': primary['candidate_id'] if primary else None,
                'decision': primary['decision'] if primary else None,
                'mechanical_dropped': list(item.get('mechanical_dropped') or []),
                'system_route': state.get('route') or {}, 'error': state.get('error'),
                'failure_checkpoint': state.get('failure_checkpoint'),
                'diagnostic_candidates': diagnostic, 'length_excluded_candidates': excluded,
                'contract_disagreement': state.get('contract_disagreement') or None,
                'state': {'video': video, 'candidates': candidates, 'delivery_slots': state.get('delivery_slots', {}),
                          'contract_disagreement': state.get('contract_disagreement') or None}}

    def record_decision(self, body):
        task_id = str(body.get('task_id') or '')
        item = self.by_task.get(task_id)
        if not item:
            raise ValueError('任务不属于当前批次')
        _, state = self._source(task_id)
        candidates = self._candidates(item, state)
        candidate_id = str(body.get('candidate_id') or '')
        thash = str(body.get('title_hash') or '')
        requested_scope = str(body.get('scope') or '')
        if requested_scope and requested_scope not in DECISION_SCOPES:
            raise ValueError('无效判定口径')
        matches = [c for c in candidates if c['candidate_id'] == candidate_id and (not thash or title_hash(c['rendered_title']) == thash)]
        # 客户端明确提供的哈希不匹配时，绝不能回退到同名候选。
        if thash and any(c['candidate_id'] == candidate_id for c in candidates) and not matches:
            raise ValueError('标题哈希不一致，请刷新页面')
        candidate = matches[0] if len(matches) == 1 else None
        scope = FROZEN_SCOPE
        if candidate is None:
            # 冻结候选匹配失败才回落到诊断候选池；口径由服务端数据决定，
            # 客户端传的 scope 不能把冻结候选降级成诊断候选。
            candidate = self._diagnostic_candidate(item, state, candidates, candidate_id)
            if candidate is None:
                raise ValueError('候选不属于本批选定标题，或本视频未产出标题')
            if thash and title_hash(candidate['rendered_title']) != thash:
                raise ValueError('标题哈希不一致，请刷新页面')
            scope = DIAGNOSTIC_SCOPE
        if _length_excluded(item, state, candidate) or any(
                _length_excluded(item, state, c) for c in self._task_candidate_pool(item, state)
                if str(c.get('candidate_id') or '') == candidate_id
                and str(c.get('rendered_title') or c.get('headline') or '') == candidate['rendered_title']):
            raise ValueError('超长候选仅供只读诊断，不接受人工评审或撤回')
        action = body.get('decision')
        if action not in {'approved', 'marginal', 'rejected', 'reopen'}:
            raise ValueError('无效人工判定')
        # 「精彩」是「可用」之上的附加标记，不是第四档：只用来说明这条标题可以当范本。
        # 因此它不参与 verdict 取值域，只在 approved 上成立；其余判定一律回落为 False，
        # 避免出现「不可用但精彩」这种自相矛盾的入库来源。诊断候选不是合格交付，
        # 永远不能当范本进案例库，所以也不接受这一标记。
        highlight = bool(body.get('highlight')) and action == 'approved' and scope == FROZEN_SCOPE
        reason = str(body.get('reason') or '').strip()
        if action in {'marginal', 'rejected'} and not reason:
            raise ValueError('勉强通过或不可用必须填写一句话意见')
        original = candidate['rendered_title']
        edited = str(body.get('final_headline') or '').strip()
        final = (edited or original) if action in {'approved', 'marginal'} else None
        if final and len(final) > 20:
            raise ValueError('修改后的完整标题不能超过20字（含标注前缀）')
        prefixes = candidate.get('prefix_labels') or []
        if final and any(f'【{prefix}】' not in final for prefix in prefixes):
            raise ValueError('修改后的标题不能丢弃平台标注前缀')
        now = datetime.now(timezone.utc).isoformat()
        payload = {'batch': self.batch_dir.name, 'blind_id': item['blind_id'], 'task_id': task_id,
                   'video_id': item['video_id'], 'candidate_id': candidate_id,
                   'slot_id': candidate.get('slot_id') or '',
                   # 标题身份以服务端真实标题为准：客户端传来的 title_hash 只能当提示，
                   # 不能决定身份，也不能把判定写到别的候选上。
                   'original_headline': original, 'title_hash': title_hash(original),
                   'decision': action, 'reason': reason, 'final_headline': final,
                   'highlight': highlight, 'scope': scope,
                   'criteria_id': str(body.get('criteria_id') or ''),
                   'reason_type': str(body.get('reason_type') or ''),
                   'reviewer': str(body.get('reviewer') or 'tiffany'), 'updated_at': now,
                   'original_system_route': state.get('route'), 'source_status': self._source(task_id)[0]['status']}
        with self._feedback_conn() as conn:
            if action == 'reopen':
                conn.execute('DELETE FROM candidate_decisions_v2 WHERE batch_id=? AND task_id=? AND candidate_id=? AND title_hash=?',
                             (self.batch_dir.name, task_id, candidate_id, payload['title_hash']))
            else:
                conn.execute('INSERT OR REPLACE INTO candidate_decisions_v2 VALUES(?,?,?,?,?)',
                             (self.batch_dir.name, task_id, candidate_id, payload['title_hash'], json.dumps(payload, ensure_ascii=False)))
            conn.execute('INSERT INTO decision_events(task_id,payload_json,created_at) VALUES(?,?,?)',
                         (task_id, json.dumps(payload, ensure_ascii=False), now))
        return {'status': 'reopened' if action == 'reopen' else 'saved', 'decision': payload}


def make_server(store, host='127.0.0.1', port=0):
    if host != '127.0.0.1':
        raise ValueError('人工评审仅监听本机')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, value, status=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', status)

        def send_bytes(self, raw, mime, status=200):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == '/':
                    self.send_bytes(PAGE.encode(), 'text/html; charset=utf-8')
                elif parsed.path == '/api/tasks':
                    self.send_json({'tasks': store.list_tasks(), 'batch_review': True})
                elif parsed.path == '/api/task':
                    self.send_json({'task': store.get_task(query.get('task_id', [''])[0])})
                elif parsed.path == '/api/feedback':
                    self.send_json(store.feedback(scope=query.get('scope', [None])[0] or None))
                elif parsed.path == '/api/media':
                    self.send_media(query)
                else:
                    self.send_json({'error': 'not found'}, 404)
            except (ValueError, KeyError) as exc:
                self.send_json({'error': str(exc)}, 400)

        def send_media(self, query):
            task = store.get_task(query.get('task_id', [''])[0])
            assets = task['state']['video'].get('asset_paths') or {}
            kind = query.get('kind', ['video'])[0]
            if kind == 'video':
                path = Path(assets.get('video') or '')
            elif kind == 'frame':
                frames = assets.get('frames') or []
                index = int(query.get('index', ['0'])[0])
                if index < 0 or index >= len(frames):
                    raise ValueError('无效抽帧')
                path = Path(frames[index])
            else:
                raise ValueError('无效素材类型')
            if not path.is_file():
                self.send_json({'error': '本地素材不可用，请打开原视频链接'}, 404)
                return
            total = path.stat().st_size
            start, end, status = 0, total - 1, 200
            requested = self.headers.get('Range')
            if requested:
                import re
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
                if not match or int(match[1]) >= total:
                    self.send_json({'error': '无效媒体范围'}, 416)
                    return
                start = int(match[1]); end = min(int(match[2]) if match[2] else total - 1, total - 1)
                if end < start:
                    self.send_json({'error': '无效媒体范围'}, 416)
                    return
                status = 206
            self.send_response(status)
            self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            self.end_headers()
            try:
                with path.open('rb') as source:
                    source.seek(start)
                    left = end - start + 1
                    while left:
                        chunk = source.read(min(left, 65536))
                        if not chunk: break
                        self.wfile.write(chunk); left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            origin = self.headers.get('Origin')
            expected = f'http://127.0.0.1:{self.server.server_port}'
            if (origin and origin != expected) or self.headers.get('X-Batch-Review') != '1':
                self.send_json({'error': '拒绝跨站判定请求'}, 403)
                return
            if self.path != '/api/decision':
                self.send_json({'error': '冻结批次不允许生成或续跑'}, 404)
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length < 1 or length > 65536:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求必须是对象')
                self.send_json(store.record_decision(body))
            except (ValueError, KeyError) as exc:
                self.send_json({'error': str(exc)}, 400)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir', required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--feedback', action='store_true', help='读取已保存人工反馈，不启动服务')
    args = parser.parse_args()
    store = BatchReviewStore(args.batch_dir)
    if args.feedback:
        print(json.dumps(store.feedback(), ensure_ascii=False, indent=2))
        return
    server = make_server(store, port=args.port)
    url = f'http://127.0.0.1:{server.server_port}'
    (store.batch_dir / 'review_server.json').write_text(json.dumps({'url': url, 'feedback_db': str(store.feedback_db)}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'本批人工评审台：{url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
