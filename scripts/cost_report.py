#!/usr/bin/env python3
"""成本归因：从既有批次的事件表反推「每个节点花了多少、多少次、贵在哪」。

为什么需要它：系统会记录每次模型调用的 usage，但从不聚合。没有这张表，
任何降本改动都无法被证明——只能凭感觉说「好像省了」。本脚本只读事件表，
不发起任何模型调用，因此可以反复跑在历史批次上，把已花的钱重新摊开看。

口径说明（三条都要写清，否则数字会被误读）：
1. 计费口径按 `model_reply` + `model_reply_invalid` 两类事件聚合。前者是成功调用，
   后者是拿到了回复但结构不合格——它同样消耗了真实 token，不能当没发生。
2. `model_error`（超时/网络/鉴权）通常没有 usage，本脚本按 0 计 token，
   但计入「失败次数」并不计入「可计费失败 token」——避免把未知当已知。
3. 占比分母是本批「全部可计费 prompt tokens」，不是「有效调用的 prompt tokens」。

用法：
    python scripts/cost_report.py --batch runtime/v_eval_fixed_20261104
    python scripts/cost_report.py --db runtime/v_eval_fixed_20261104/agent.db --json
    python scripts/cost_report.py --all runtime  # 扫所有含 agent.db 的批次
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BILLABLE_EVENTS = ("model_reply", "model_reply_invalid")
FAILED_EVENTS = ("model_reply_invalid", "model_error", "node_failed")

# 节点顺序按「生成链路」而不是字母序，方便对照流程图读。
NODE_ORDER = ("understand", "boundary", "highlight", "generate", "rewrite", "review", "thesis_review", "thesis_consistency", "verify")
NODE_LABEL = {
    "understand": "理解",
    "boundary": "边界",
    "highlight": "看点",
    "generate": "生成",
    "rewrite": "重写",
    "review": "评审",
    "thesis_review": "主旨复核",
    "thesis_consistency": "主旨比对",
    "verify": "外部核查",
}


@dataclass
class NodeCost:
    task: str
    calls: int = 0
    ok_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failures: int = 0
    failed_prompt_tokens: int = 0
    cache_hits: int = 0

    @property
    def avg_prompt(self) -> int:
        return round(self.prompt_tokens / self.calls) if self.calls else 0

    @property
    def ok_avg_prompt(self) -> int:
        return round(self.prompt_tokens / self.ok_calls) if self.ok_calls else 0


@dataclass
class BatchCost:
    path: Path
    videos: int = 0
    nodes: dict[str, NodeCost] = field(default_factory=dict)
    task_calls: dict[str, int] = field(default_factory=dict)   # task_id -> 调用次数
    total_events: int = 0

    @property
    def total_prompt(self) -> int:
        return sum(n.prompt_tokens for n in self.nodes.values())

    @property
    def total_completion(self) -> int:
        return sum(n.completion_tokens for n in self.nodes.values())

    @property
    def total_calls(self) -> int:
        return sum(n.calls for n in self.nodes.values())

    @property
    def total_failures(self) -> int:
        return sum(n.failures for n in self.nodes.values())

    @property
    def wasted_prompt(self) -> int:
        return sum(n.failed_prompt_tokens for n in self.nodes.values())

    def per_video(self) -> int:
        return round(self.total_prompt / self.videos) if self.videos else 0


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _usage(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    # usage 缺失与 usage=0 是两件事，但在这个口径下都按 0 计；不要用估算值冒充真实消耗。
    return (int(prompt) if isinstance(prompt, (int, float)) else 0,
            int(completion) if isinstance(completion, (int, float)) else 0)


def load_batch(db: Path) -> BatchCost:
    batch = BatchCost(path=db.parent)
    nodes: dict[str, NodeCost] = {}
    with closing(_ro(db)) as conn:
        batch.videos = int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0])
        rows = conn.execute(
            "SELECT task_id, node, event_type, payload_json FROM events "
            "WHERE event_type IN (?,?,?,?)",
            ("model_reply", "model_reply_invalid", "model_error", "model_cache_hit"),
        )
        for row in rows:
            batch.total_events += 1
            task_id = str(row["task_id"] or "")
            event_type = str(row["event_type"])
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            # 事件表里的 node 是 N03/R01 这类编号，真正可读的分类在 payload.task。
            name = str(payload.get("task") or row["node"] or "unknown")
            node = nodes.setdefault(name, NodeCost(task=name))
            if event_type == "model_cache_hit":
                node.cache_hits += 1
                continue
            if event_type == "model_error":
                node.failures += 1
                continue
            prompt, completion = _usage(payload)
            node.calls += 1
            node.prompt_tokens += prompt
            node.completion_tokens += completion
            batch.task_calls[task_id] = batch.task_calls.get(task_id, 0) + 1
            if event_type == "model_reply":
                node.ok_calls += 1
            else:
                node.failures += 1
                node.failed_prompt_tokens += prompt
    batch.nodes = nodes
    return batch


def _order(name: str) -> tuple[int, str]:
    try:
        return NODE_ORDER.index(name), name
    except ValueError:
        return len(NODE_ORDER), name


def render(batch: BatchCost) -> str:
    total_prompt = batch.total_prompt or 1
    lines = [
        f"# 成本归因 · {batch.path.name}",
        "",
        f"- 视频/任务数：**{batch.videos}**",
        f"- 模型调用：**{batch.total_calls}** 次（失败 {batch.total_failures} 次，"
        f"其中可计费失败 {sum(1 for n in batch.nodes.values() if n.failed_prompt_tokens)} 个节点）",
        f"- prompt tokens：**{batch.total_prompt:,}**（每条视频 {batch.per_video():,}）",
        f"- completion tokens：**{batch.total_completion:,}**",
    ]
    if batch.total_completion:
        ratio = batch.total_prompt / batch.total_completion
        lines.append(f"- 输入/输出比：**{ratio:.1f} : 1**（输入占成本 {total_prompt / (total_prompt + batch.total_completion):.1%}）")
    if batch.wasted_prompt:
        lines.append(f"- 结构失败浪费 prompt：**{batch.wasted_prompt:,}**（占 {batch.wasted_prompt / total_prompt:.1%}）")
    lines.append("")

    header = ["| 节点 | 调用 | prompt | 占比 | 成功均价 | completion | 失败 | 浪费 prompt |",
              "|---|---|---|---|---|---|---|---|"]
    body = []
    for name in sorted(batch.nodes, key=_order):
        node = batch.nodes[name]
        body.append(
            f"| {NODE_LABEL.get(name, name)} `{name}` | {node.calls} | {node.prompt_tokens:,} | "
            f"{node.prompt_tokens / total_prompt:.1%} | {node.ok_avg_prompt:,} | "
            f"{node.completion_tokens:,} | {node.failures} | {node.failed_prompt_tokens:,} |"
        )
    lines += header + body
    lines.append("")

    if batch.task_calls:
        counts = sorted(batch.task_calls.values())
        median = counts[len(counts) // 2]
        lines += [
            "## 每任务的调用次数分布",
            "",
            f"- 最少 {counts[0]} / 中位 {median} / 最多 {counts[-1]} 次",
            f"- 调用数最多的 3 个任务：{', '.join(f'{k[-8:]}={v}' for k, v in sorted(batch.task_calls.items(), key=lambda x: -x[1])[:3])}",
            "",
        ]
    return "\n".join(lines)


def to_json(batch: BatchCost) -> dict[str, Any]:
    total = batch.total_prompt or 1
    return {
        "batch": batch.path.name,
        "videos": batch.videos,
        "calls": batch.total_calls,
        "failures": batch.total_failures,
        "prompt_tokens": batch.total_prompt,
        "completion_tokens": batch.total_completion,
        "prompt_per_video": batch.per_video(),
        "wasted_prompt_tokens": batch.wasted_prompt,
        "nodes": {
            name: {
                "calls": node.calls,
                "ok_calls": node.ok_calls,
                "prompt_tokens": node.prompt_tokens,
                "share": round(node.prompt_tokens / total, 4),
                "avg_prompt_ok": node.ok_avg_prompt,
                "completion_tokens": node.completion_tokens,
                "failures": node.failures,
                "failed_prompt_tokens": node.failed_prompt_tokens,
                "cache_hits": node.cache_hits,
            }
            for name, node in batch.nodes.items()
        },
    }


def find_batches(root: Path) -> Iterable[Path]:
    for db in sorted(root.glob("*/agent.db")):
        yield db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", help="批次目录（含 agent.db）")
    parser.add_argument("--db", help="直接指定 agent.db")
    parser.add_argument("--all", metavar="ROOT", help="扫描 root 下所有含 agent.db 的批次")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", default="", help="把结果写入文件（默认打印）")
    args = parser.parse_args()

    targets: list[Path] = []
    if args.db:
        targets.append(Path(args.db).resolve())
    if args.batch:
        targets.append(Path(args.batch).resolve() / "agent.db")
    if args.all:
        targets.extend(p.resolve() for p in find_batches(Path(args.all).resolve()))
    if not targets:
        parser.error("至少给一个 --db / --batch / --all")
    missing = [p for p in targets if not p.is_file()]
    if missing:
        print("找不到数据库：" + "、".join(str(p) for p in missing))
        return 2

    batches = [load_batch(p) for p in targets]
    if args.json:
        payload: Any = [to_json(b) for b in batches] if len(batches) > 1 else to_json(batches[0])
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = "\n\n---\n\n".join(render(b) for b in batches)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
