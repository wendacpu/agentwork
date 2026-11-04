"""导出本地反馈修复待办，不执行提案。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_system.repairs import RepairStore


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', default='runtime/repair/repair.db')
    p.add_argument('--output', required=True)
    args = p.parse_args()
    target = Path(args.output)
    if target.exists(): raise ValueError('拒绝覆盖已有报告')
    conn = sqlite3.connect(Path(args.ledger).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        # 不通过建表型构造器打开只读库。
        rows = [{'key': r[0], 'revision': r[1], 'status': r[2], 'payload': json.loads(r[3]), 'diagnosis': json.loads(r[4]), 'proposal': json.loads(r[5])} for r in conn.execute('SELECT feedback_key,revision,status,payload,diagnosis,proposal FROM repair_defects')]
    finally: conn.close()
    lines = ['# 反馈修复待办快照', '', '此报告只列调查假设，不代表已修复或批准发布。', '', f'总记录 {len(rows)}；状态 {dict(Counter(r["status"] for r in rows))}', '', '## 缺陷任务', '']
    for row in rows:
        if row['status'] == 'no_defect': continue
        d, proposal = row['diagnosis'], row['proposal']
        lines.extend([f'### {row["key"]}', '', f'- 状态：{row["status"]}；调查族：{d.get("family", "unknown")}', f'- 原标题：{row["payload"]["candidate"].get("headline", "")}', f'- 人工理由：{row["payload"]["decision"].get("reason", "")}', f'- 提案 ID：`{row["key"]}@{row["revision"]}`', f'- 证据观测：{json.dumps(d.get("observations", []), ensure_ascii=False)}', f'- 工作项：{json.dumps(proposal.get("work_items", []), ensure_ascii=False)}', ''])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'records': len(rows), 'statuses': dict(Counter(r['status'] for r in rows)), 'report': str(target)}, ensure_ascii=False))


if __name__ == '__main__': main()
