#!/usr/bin/env python3
"""冻结源候选并逐任务隔离重评。默认仅预览；执行需显式 --execute。

示例：python scripts/batch_rereview.py --db runtime/agent.db --output runtime/batch-1 --limit 5
      python scripts/batch_rereview.py --db runtime/agent.db --output runtime/batch-1 --mock --execute
非 mock 模型执行还须显式传 --allow-paid；请先确认授权和费用。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.cli import make_pipeline
from agent_system.config import RunConfig
from agent_system.pipeline import _freeze_rereview_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='源任务库，只读')
    parser.add_argument('--output', required=True, help='全新批次目录，不可已存在')
    parser.add_argument('--config', help='与主 CLI 一致的 RunConfig JSON')
    parser.add_argument('--task-id', action='append', default=[], help='指定任务，可重复；缺省按 task_id 排序选全部')
    parser.add_argument('--limit', type=int, default=0, help='最多选取任务数；0 表示不限')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='实际重评；缺省仅预览')
    mode.add_argument('--dry-run', action='store_true', help='只冻结并输出预览，不创建目录、不调用模型')
    parser.add_argument('--mock', action='store_true', help='使用项目 MockAdapter')
    parser.add_argument('--allow-paid', action='store_true', help='允许非 mock 模型调用，可能产生费用')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit 不能为负数')
    if len(set(args.task_id)) != len(args.task_id):
        parser.error('任务 ID 不可重复')
    source = Path(args.db).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        parser.error(f'找不到源库：{source}')
    if output.exists() or output == source or output in source.parents:
        parser.error('输出必须是全新目录，且不得包含源库')
    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()
    if args.mock:
        config.model_provider = 'mock'
    if args.execute and config.model_provider != 'mock' and not args.allow_paid:
        parser.error('非 mock 执行可能产生费用，必须显式指定 --allow-paid')
    # 评审只写本批目录：禁止共享缓存、外部核查；output_dir 覆盖配置和环境。
    config.output_dir = str(output)
    config.cache_bank = ''
    config.cache_bank_isolated = True
    config.case_bank = ''
    config.case_bank_isolated = True
    config.verification_enabled = False

    conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # 读事务保证冻结与 Pipeline.rereview 看到同一数据库快照。
        conn.execute('BEGIN')
        rows = {row['task_id']: row for row in conn.execute('SELECT * FROM tasks ORDER BY task_id')}
        ids = args.task_id or list(rows)
        if args.limit:
            ids = ids[:args.limit]
        records = []
        for task_id in ids:
            item = {'task_id': task_id, 'status': 'planned', 'candidates': []}
            try:
                row = rows[task_id]
                item['video_id'] = row['video_id']
                state = json.loads(row['state_json'] or '{}')
                item['candidates'] = _freeze_rereview_candidates(state.get('candidates'))
            except Exception as exc:
                item.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            records.append(item)
        report = {'source_database_path': str(source), 'output_dir': str(output),
                  'dry_run': not args.execute, 'model_provider': config.model_provider,
                  'generation_model': config.generation_model, 'review_model': config.review_model,
                  'config_snapshot': {key: value for key, value in asdict(config).items()
                                      if key not in {'api_key_env', 'base_url', 'extra',
                                                     'verification_secret_id_env', 'verification_secret_key_env'}},
                  'config_snapshot_omitted': ['api_key_env', 'base_url', 'extra',
                                              'verification_secret_id_env', 'verification_secret_key_env'],
                  'tasks': records}
        if not args.execute:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if any(r['status'] == 'failed' for r in records) else 0
        output.mkdir(parents=True, exist_ok=False)
        report_path = output / 'manifest.json'

        def save():
            # 每个任务执行前后落盘；进程中断时 running 不会被误认为成功。
            temp = output / 'manifest.json.tmp'
            temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(report_path)

        save()
        def get_task(task_id):
            return rows[task_id]
        storage = SimpleNamespace(path=str(source), conn=conn, case_conn=conn, get_task=get_task)
        try:
            pipeline = make_pipeline(config, None, storage)
        except Exception as exc:
            for item in records:
                if item['status'] == 'planned':
                    item.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            save()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        for item in records:
            if item['status'] != 'planned':
                continue
            item['status'] = 'running'
            save()
            try:
                item['result_db'] = str(Path(pipeline.rereview(item['task_id'])).resolve())
                item['status'] = 'completed'
            except Exception as exc:
                item.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            save()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if any(r['status'] == 'failed' for r in records) else 0
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
