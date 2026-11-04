"""修复提案与发布门禁的联合入口，不调用模型、不自动改源码。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3

from .repairs import RepairStore, backfill
from . import release_gate


def reviewed_ids(ledger: str, ids: list[str]):
    conn = sqlite3.connect(ledger)
    try:
        store = RepairStore(conn)
        sources = [r[0] for r in conn.execute('SELECT path FROM repair_sources')]
        for source in sources:
            backfill(store, Path(source))
        drain_result = store.drain()
        if drain_result['failed'] or conn.execute("SELECT 1 FROM repair_outbox WHERE status IN ('pending','failed') LIMIT 1").fetchone():
            raise ValueError('反馈处理尚有失败或待处理事件，不能使用旧提案发布')
        rows = {r['feedback_key'] + '@' + r['revision']: r for r in store.list()}
        for pid in ids:
            if pid not in rows or rows[pid]['status'] != 'reviewed':
                raise ValueError(f'修复提案未审核或反馈已改判：{pid}')
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    p.add_argument('--ledger', default='runtime/repair/repair.db')
    p.add_argument('--output-dir', default='runtime/repair/releases')
    sub = p.add_subparsers(dest='command', required=True)
    v = sub.add_parser('validate'); v.add_argument('--proposal', action='append', required=True)
    a = sub.add_parser('approve'); a.add_argument('--validation', required=True); a.add_argument('--reviewer', required=True); a.add_argument('--confirm', action='store_true')
    c = sub.add_parser('verify-cases'); c.add_argument('--validation', required=True); c.add_argument('--proposal', required=True); c.add_argument('--reviewer', required=True); c.add_argument('--checklist', required=True); c.add_argument('--confirm', action='store_true')
    sub.add_parser('status')
    revoke = sub.add_parser('revoke'); revoke.add_argument('--manifest', required=True); revoke.add_argument('--reviewer', required=True); revoke.add_argument('--reason', required=True); revoke.add_argument('--confirm', action='store_true')
    for name in ('close', 'resolve'):
        item = sub.add_parser(name)
        for key in ('proposal', 'manifest-id', 'reviewer', 'evidence'):
            item.add_argument('--' + key, required=True)
        item.add_argument('--confirm', action='store_true')
    sub.add_parser('observations')
    args = p.parse_args(argv)
    if args.command == 'validate':
        reviewed_ids(args.ledger, args.proposal)
        result = release_gate.validate_release(args.root, args.proposal, args.output_dir)
    elif args.command == 'verify-cases':
        reviewed_ids(args.ledger, [args.proposal])
        checklist = json.loads(Path(args.checklist).read_text(encoding='utf-8'))
        result = release_gate.record_proposal_verification(args.root, args.validation, args.proposal, args.reviewer, checklist, args.output_dir, confirmed=args.confirm)
    elif args.command == 'approve':
        # 模块会从可信SQLite复核验证，CLI此处再检查反馈修订仍有效。
        validation = json.loads(Path(args.validation).read_text(encoding='utf-8'))
        reviewed_ids(args.ledger, validation['proposal_ids'])
        result = release_gate.approve_release(args.root, args.validation, args.reviewer, args.output_dir, confirmed=args.confirm)
        from .repair_lifecycle import register
        with sqlite3.connect(args.ledger) as conn:
            register(conn, result)
    elif args.command in {'close', 'resolve', 'observations'}:
        # 查看观察状态同样同步已登记源库，不能遗漏发布后的新负反馈。
        with sqlite3.connect(args.ledger) as sync_conn:
            sync_store = RepairStore(sync_conn)
            for (path,) in sync_conn.execute('SELECT path FROM repair_sources').fetchall():
                backfill(sync_store, Path(path))
            outcome = sync_store.drain()
            if outcome['failed']:
                raise ValueError('反馈同步处理失败，观察状态不可作为最新结论')
        from . import repair_lifecycle
        with sqlite3.connect(args.ledger) as conn:
            repair_lifecycle.init(conn)
            if args.command == 'observations':
                result = [dict(proposal_id=r[0], manifest_id=r[1], status=r[2], details=json.loads(r[3])) for r in conn.execute('SELECT * FROM repair_deployments')]
            else:
                reviewed_ids(args.ledger, [args.proposal])
                gate = release_gate.check_active_release(args.root, args.output_dir)
                if not gate['allowed'] or gate.get('manifest_id') != args.manifest_id:
                    raise ValueError('观察或结案必须对应当前有效发布')
                getattr(repair_lifecycle, args.command)(conn, args.proposal, args.manifest_id, args.reviewer, args.evidence, confirmed=args.confirm)
                result = {'proposal': args.proposal, 'action': args.command}
    elif args.command == 'revoke':
        result = release_gate.revoke_release(args.root, args.manifest, args.reviewer, args.reason, args.output_dir, confirmed=args.confirm)
    else:
        result = release_gate.check_active_release(args.root, args.output_dir)
        if result.get('status') == 'active':
            try:
                reviewed_ids(args.ledger, result['proposal_ids'])
            except (ValueError, sqlite3.Error, OSError) as exc:
                result = {**result, 'allowed': False, 'status': 'invalid', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    main()
