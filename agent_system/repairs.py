"""反馈修复账本：幂等事件、可撤回缺陷、证据归因与待验证提案。无需模型。"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


class RepairStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # 不使用 executescript，避免隐式提交人工判定的外层事务。
        for sql in (
            'CREATE TABLE IF NOT EXISTS repair_outbox(event_id TEXT PRIMARY KEY, feedback_key TEXT, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT, created_at TEXT)',
            'CREATE TABLE IF NOT EXISTS repair_defects(feedback_key TEXT PRIMARY KEY, revision TEXT, status TEXT, payload TEXT, diagnosis TEXT, proposal TEXT, updated_at TEXT)',
            'CREATE TABLE IF NOT EXISTS repair_history(id INTEGER PRIMARY KEY, feedback_key TEXT, revision TEXT, action TEXT, payload TEXT, created_at TEXT)',
            'CREATE TABLE IF NOT EXISTS repair_sources(path TEXT PRIMARY KEY)',
        ):
            conn.execute(sql)

    def enqueue(self, task_id: str, state: dict, decision: dict, candidate: dict, *, withdrawn=False, source='live') -> str:
        key = f"{task_id}:{candidate.get('candidate_id', decision.get('candidate_id', ''))}"
        # 模拟信号必须显式隔离反馈键，不能覆盖或伪装成同候选的人工三档判定。
        if source == 'simulated_signal':
            key = f"{key}:simulated_signal"
        snapshot = {k: state.get(k) for k in ('video', 'understanding', 'boundary', 'rules', 'prompt_hashes', 'risk_signals')}
        snapshot['reviews'] = [r for r in state.get('reviews', []) if r.get('candidate_id') == candidate.get('candidate_id')]
        payload = {'task_id': task_id, 'candidate': candidate, 'decision': decision, 'snapshot': snapshot, 'withdrawn': withdrawn, 'source': source}
        revision = hashlib.sha256(_json(payload).encode()).hexdigest()
        previous = self.conn.execute('SELECT event_id,payload FROM repair_outbox WHERE feedback_key=? ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
        if previous and previous[1] == _json(payload):
            return previous[0]
        event_id = hashlib.sha256((key + revision + (previous[0] if previous else '')).encode()).hexdigest()
        # 相同当前事件不重置状态；撤回后再次作出同样判断是一条新修订。
        self.conn.execute('INSERT OR IGNORE INTO repair_outbox(event_id,feedback_key,payload,status,created_at) VALUES(?,?,?,?,?)', (event_id, key, _json(payload), 'pending', _now()))
        return event_id

    def drain(self) -> dict:
        counts = {'processed': 0, 'failed': 0}
        rows = self.conn.execute("SELECT event_id,feedback_key,payload FROM repair_outbox WHERE status IN ('pending','failed') ORDER BY rowid").fetchall()
        for event_id, key, raw in rows:
            try:
                self.conn.execute('BEGIN IMMEDIATE')
                current_status = self.conn.execute('SELECT status FROM repair_outbox WHERE event_id=?', (event_id,)).fetchone()
                if not current_status or current_status[0] not in {'pending', 'failed'}:
                    self.conn.commit()
                    continue
                newer = self.conn.execute("SELECT 1 FROM repair_outbox WHERE feedback_key=? AND status='delivered' AND rowid>(SELECT rowid FROM repair_outbox WHERE event_id=?)", (key, event_id)).fetchone()
                if newer:
                    self.conn.execute("UPDATE repair_outbox SET status='superseded' WHERE event_id=?", (event_id,))
                    self.conn.commit()
                    continue
                payload = json.loads(raw)
                candidate, decision = payload['candidate'], payload['decision']
                original = candidate.get('rendered_title') or candidate.get('headline') or ''
                final = decision.get('final_headline') or ''
                changed = bool(final and final not in {original, candidate.get('headline', '')})
                simulated_negative = (payload.get('source') == 'simulated_signal'
                                      and (decision.get('signal') or {}).get('event') == 'negative_feedback')
                negative = (decision.get('decision') in {'rejected', 'marginal'}
                            or (decision.get('decision') == 'approved' and changed)
                            or simulated_negative)
                # 模拟负反馈只创建待核查的修复假设，绝不写入人工判定/金标。
                status = 'withdrawn' if payload['withdrawn'] else 'triage' if negative else 'no_defect'
                diagnosis, proposal = diagnose(payload) if status == 'triage' else ({}, {})
                prior = self.conn.execute('SELECT revision FROM repair_defects WHERE feedback_key=?', (key,)).fetchone()
                self.conn.execute('INSERT INTO repair_defects VALUES(?,?,?,?,?,?,?) ON CONFLICT(feedback_key) DO UPDATE SET revision=excluded.revision,status=excluded.status,payload=excluded.payload,diagnosis=excluded.diagnosis,proposal=excluded.proposal,updated_at=excluded.updated_at', (key, event_id, status, raw, _json(diagnosis), _json(proposal), _now()))
                if status == 'triage':
                    from .repair_lifecycle import observe_feedback
                    observe_feedback(self.conn, key, event_id, payload, diagnosis)
                from .repair_lifecycle import init
                init(self.conn)
                self.conn.execute("UPDATE repair_deployments SET status='invalidated' WHERE proposal_id LIKE ? AND proposal_id != ?", (key + '@%', key + '@' + event_id))
                self.conn.execute('INSERT INTO repair_history(feedback_key,revision,action,payload,created_at) VALUES(?,?,?,?,?)', (key, event_id, 'superseded' if prior else 'created', _json({'previous': prior[0] if prior else None, 'status': status}), _now()))
                self.conn.execute("UPDATE repair_outbox SET status='delivered',attempts=attempts+1,error=NULL WHERE event_id=?", (event_id,))
                self.conn.commit()
                counts['processed'] += 1
            except Exception as exc:
                self.conn.rollback()
                self.conn.execute("UPDATE repair_outbox SET status='failed',attempts=attempts+1,error=? WHERE event_id=?", (str(exc)[:500], event_id))
                self.conn.commit()
                counts['failed'] += 1
        return counts

    def list(self):
        return [{'feedback_key': r[0], 'revision': r[1], 'status': r[2], 'payload': json.loads(r[3]), 'diagnosis': json.loads(r[4]), 'proposal': json.loads(r[5])} for r in self.conn.execute('SELECT feedback_key,revision,status,payload,diagnosis,proposal FROM repair_defects ORDER BY feedback_key')]

    def review_proposal(self, key, revision, reviewer, *, root_cause, change, regression_tests, counterexamples, acceptance):
        row = self.conn.execute('SELECT revision,status FROM repair_defects WHERE feedback_key=?', (key,)).fetchone()
        if not row or row[0] != revision or row[1] != 'triage':
            raise ValueError('提案已审核冻结、撤回、过期或不存在；修改方案需创建新反馈修订')
        if not all(isinstance(x, str) and x.strip() for x in (reviewer, root_cause, change, regression_tests, counterexamples, acceptance)):
            raise ValueError('必须审核根因、修复、原案例回归、反例和验收条件')
        review = dict(reviewer=reviewer, root_cause=root_cause, change=change, regression_tests=regression_tests, counterexamples=counterexamples, acceptance=acceptance, reviewed_at=_now())
        self.conn.execute("UPDATE repair_defects SET status='reviewed',proposal=?,updated_at=? WHERE feedback_key=?", (_json(review), _now(), key))
        self.conn.execute('INSERT INTO repair_history(feedback_key,revision,action,payload,created_at) VALUES(?,?,?,?,?)', (key, revision, 'proposal_reviewed', _json(review), _now()))
        self.conn.commit()


def diagnose(payload: dict) -> tuple[dict, dict]:
    """规则给出有依据的调查方向，不冒充已确认因果。"""
    reason = str(payload['decision'].get('reason') or '')
    raw_type = payload['decision'].get('reason_type') or ''
    snapshot = payload['snapshot']
    candidate = payload['candidate']
    codes = sorted({i.get('issue_code') for r in snapshot.get('reviews') or [] for i in r.get('issues', []) if i.get('issue_code')})
    groups = [
        ('content_nature', ('AI', '虚构', '演绎', '虚拟'), ('extraction', 'constraint', 'validation'), '原始声明→信号→必填前缀→最终渲染'),
        ('numeric', ('数字', '金额', '价格', '872', '800'), ('extraction', 'validation'), '数值、单位、指标、引述角色与证据定位'),
        ('selection', ('看点', '边角', '核心', '重点'), ('selection',), '主旨与候选看点的证据映射'),
        ('constraint', ('背景', '限定', '主体', '地点'), ('extraction', 'constraint'), '实体角色、必要锚点与限定传递'),
        ('expression', ('句子', '读不懂', '不完整', '用词'), ('expression',), '语义自足与表达自然度'),
    ]
    match = next((g for g in groups if any(t.lower() in reason.lower() for t in g[1])), None)
    by_type = {'fact': 'numeric' if any(c.startswith('G4_NUMERIC') for c in codes) else 'constraint', 'context': 'constraint', 'point': 'selection', 'grammar': 'expression', 'unclear': 'expression'}
    if match is None and raw_type in by_type:
        match = next(g for g in groups if g[0] == by_type[raw_type])
    family, layers, inspection = (match[0], list(match[2]), match[3]) if match else ('unresolved', ['unknown'], '先核对人工理由与原始证据')
    evidence_ids = candidate.get('evidence_ids', [])
    video = snapshot.get('video') or {}
    evidence = [e for e in video.get('evidence', []) if e.get('evidence_id') in evidence_ids]
    observations = []
    if video.get('evidence'):
        from types import SimpleNamespace
        from .signals import extract_risk_signals, unsupported_numeric_facts
        from .labels import required_prefixes
        report = extract_risk_signals(SimpleNamespace(evidence=[SimpleNamespace(**({'field': 'unknown', 'quality': 'unknown', **e})) for e in video['evidence']]))
        missing = sorted(set(report['required_prefixes']) - set(candidate.get('prefix_labels', [])))
        if missing:
            observations.append({'kind': 'confirmed_declaration_missing_prefix', 'missing': missing, 'signals': [s for s in report['signals'] if s['status'] == 'confirmed_declaration'], 'suspected_failure': 'constraint_or_rendering', 'scope': 'current_guard_replay_not_historical_execution'})
        texts = [e['text'] for e in evidence if e.get('quality') not in {'model_derived_uncalibrated', 'unverified_source_metadata'} and e.get('field') != 'frame']
        unmatched = unsupported_numeric_facts(candidate.get('headline', ''), texts)
        if unmatched:
            observations.append({'kind': 'numeric_support_unresolved', 'facts': unmatched, 'scope': 'candidate_evidence_refs_only_requires_claim_mapping_review'})
        if report['pending_signal_ids']:
            observations.append({'kind': 'content_nature_needs_verification', 'signals': report['pending_signal_ids']})
    diagnosis = {'status': 'hypothesis', 'family': family, 'suspected_layers': layers, 'human_reason': reason, 'issue_codes': codes, 'evidence': evidence, 'inspection_path': inspection, 'first_failure': 'unconfirmed', 'escaped_defense': 'unconfirmed', 'observations': observations, 'missing_snapshot': not bool(snapshot.get('video'))}
    proposal = {'status': 'draft', 'target_layers': layers, 'investigate': inspection, 'suggested_change': '检查该路径最早失效节点，修复原约束而非追加全局禁词；根因未确认时不得发布', 'regression_seed': {'original': candidate.get('headline'), 'expected': payload['decision'].get('final_headline'), 'human_verdict': payload['decision'].get('decision'), 'risk_truth': 'requires_review'}, 'required_tests': ['原案例复现', '同风险改写变体', '否定/无关语境反例', '历史保留集与人工负担比较'], 'release_eligible': False}
    from .repair_playbooks import playbook, build_work_items
    proposal['playbook'] = playbook(family)
    proposal['work_items'] = build_work_items(family, payload)
    return diagnosis, proposal


def group_defects(store: RepairStore) -> dict:
    """把逐条提案按根因族聚成少数几组，供一次决策一组。

    88 条独立提案没人会逐条审；族内根因相同，修一处就能覆盖一批。
    这里只做聚合，不改变任何提案状态，也不构成修复结论。
    """
    groups: dict[str, dict] = {}
    for row in store.list():
        if row["status"] == "no_defect":
            continue
        diag = row["diagnosis"] or {}
        family = str(diag.get("family") or "unresolved")
        g = groups.setdefault(family, {
            "family": family, "count": 0, "statuses": {}, "issue_codes": {},
            "layers": [], "videos": set(), "examples": [],
        })
        g["count"] += 1
        g["statuses"][row["status"]] = g["statuses"].get(row["status"], 0) + 1
        for code in diag.get("issue_codes") or []:
            g["issue_codes"][code] = g["issue_codes"].get(code, 0) + 1
        for layer in diag.get("suspected_layers") or []:
            if layer not in g["layers"]:
                g["layers"].append(layer)
        video_id = str((row["payload"].get("snapshot", {}).get("video") or {}).get("video_id") or "")
        if video_id:
            g["videos"].add(video_id)
        if len(g["examples"]) < 3:
            g["examples"].append({
                "proposal_id": f"{row['feedback_key']}@{row['revision']}",
                "headline": row["payload"]["candidate"].get("headline", ""),
                "reason": row["payload"]["decision"].get("reason", ""),
            })
    out = []
    for g in groups.values():
        g["distinct_videos"] = len(g.pop("videos"))
        out.append(g)
    # 影响面大的族先修：覆盖视频多、条目多，修一处的收益最大。
    return {"groups": sorted(out, key=lambda x: (-x["distinct_videos"], -x["count"])),
            "total_defects": sum(g["count"] for g in out),
            "note": "族内为同一根因假设；review 后可只对代表案例出修复与反例，其余随版本回归。"}


def backfill(store: RepairStore, db_path: Path):
    source = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    store.conn.execute('INSERT OR IGNORE INTO repair_sources(path) VALUES(?)', (str(db_path.resolve()),))
    n = 0
    try:
        for task_id, state_raw, config_raw in source.execute('SELECT task_id,state_json,config_json FROM tasks'):
            if json.loads(config_raw).get('model_provider') == 'mock':
                continue
            state = json.loads(state_raw)
            current = {}
            for (raw,) in source.execute('SELECT payload_json FROM human_decisions WHERE task_id=? ORDER BY id', (task_id,)):
                d = json.loads(raw)
                current[d.get('candidate_id')] = d
            for candidate in state.get('candidates', []):
                cid = candidate.get('candidate_id')
                if cid in current:
                    store.enqueue(task_id, state, current[cid], candidate, source=str(db_path))
                    n += 1
                elif store.conn.execute('SELECT 1 FROM repair_defects WHERE feedback_key=?', (f'{task_id}:{cid}',)).fetchone():
                    store.enqueue(task_id, state, {'candidate_id': cid}, candidate, withdrawn=True, source=str(db_path))
        store.conn.commit()
    finally:
        source.close()
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', default='runtime/repair/repair.db')
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('backfill'); imp.add_argument('--db', action='append', required=True)
    sub.add_parser('drain'); sub.add_parser('list'); sub.add_parser('analyse'); sub.add_parser('sync'); sub.add_parser('group')
    review = sub.add_parser('review')
    for arg in ('key', 'revision', 'reviewer', 'root-cause', 'change', 'regression-tests', 'counterexamples', 'acceptance'):
        review.add_argument('--' + arg, required=True)
    args = p.parse_args(argv)
    target = Path(args.ledger); target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30); store = RepairStore(conn); conn.commit()
    try:
        if args.command == 'backfill':
            result = {'imported_decisions': sum(backfill(store, Path(db)) for db in args.db), **store.drain()}
        elif args.command == 'sync':
            sources = [r[0] for r in conn.execute('SELECT path FROM repair_sources')]
            result = {'imported_decisions': sum(backfill(store, Path(db)) for db in sources), **store.drain()}
        elif args.command == 'drain': result = store.drain()
        elif args.command == 'analyse':
            total = 0
            for row in store.list():
                if row['status'] != 'triage':
                    continue
                diagnosis, proposal = diagnose(row['payload'])
                conn.execute('UPDATE repair_defects SET diagnosis=?,proposal=?,updated_at=? WHERE feedback_key=?', (_json(diagnosis), _json(proposal), _now(), row['feedback_key']))
                total += 1
            conn.commit()
            result = {'analysed': total, 'status': 'hypotheses_only'}
        elif args.command == 'group':
            result = group_defects(store)
        elif args.command == 'review':
            store.review_proposal(args.key, args.revision, args.reviewer, root_cause=args.root_cause, change=args.change, regression_tests=args.regression_tests, counterexamples=args.counterexamples, acceptance=args.acceptance)
            result = {'reviewed': args.key}
        else: result = store.list()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally: conn.close()


if __name__ == '__main__':
    main()
