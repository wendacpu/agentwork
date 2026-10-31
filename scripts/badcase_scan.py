#!/usr/bin/env python3
"""Bad Case 自动回收扫描（渠道二）。

从运行库（runtime/**/agent.db）里自动提取「被防线拦住 / 技术失败 / 业务停止」的信号，
按错误码与节点聚合，输出 P2 趋势报告。对应 docs/bad-case-spec.md 第 6 节「渠道二：系统自动回收」。

用法：
    python3 scripts/badcase_scan.py            # 扫 runtime/**/agent.db
    python3 scripts/badcase_scan.py --root runtime/review_batch_001
    python3 scripts/badcase_scan.py --json     # 输出 JSON
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def find_dbs(root: str) -> list[str]:
    return sorted(glob.glob(str(Path(root) / "**" / "agent.db"), recursive=True))


def read_tasks(db: str):
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    try:
        rows = []
        for r in conn.execute("SELECT task_id, video_id, status, state_json FROM tasks"):
            try:
                state = json.loads(r["state_json"] or "{}")
            except Exception:
                state = {}
            rows.append((r["task_id"], r["video_id"], r["status"], state))
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def collect_signals(db: str, task_id: str, video_id: str, status: str, state: dict) -> list[dict]:
    """每个任务抽出可归类的 bad case 信号；kind 对应渠道二的四类痕迹。"""
    signals: list[dict] = []
    db_name = db.rsplit("/runtime/", 1)[-1]

    def add(kind: str, code: str, node: str, detail: str = "", severity_hint: str = "P2") -> None:
        signals.append({"db": db_name, "task_id": task_id, "video_id": video_id,
                        "kind": kind, "code": code, "node": node,
                        "detail": detail[:160], "severity_hint": severity_hint})

    # ① 校验 / 机械拦截
    for cand in (state.get("candidates") or []):
        for err in ((cand.get("mechanical_checks") or {}).get("errors") or []):
            add("G4_机械校验", str(err.get("code") or "G4_UNKNOWN"), "N10", str(err.get("reason") or ""))
        for flag in (cand.get("programmatic_g1_flags") or []):
            code = flag.get("code") if isinstance(flag, dict) else str(flag)
            add("G1_程序化事实标志", str(code or "G1_FLAG"), "N10", str(flag)[:120])
        for i, issue in enumerate(cand.get("generation_schema_errors") or []):
            add("结构_生成schema错误", str(issue or "SCHEMA"), "N09", str(issue)[:120])

    # ② 路由（拒/重写）
    for d in (state.get("candidate_decisions") or []):
        st = str(d.get("status") or "")
        if st in {"reject", "rewrite"}:
            add("路由_" + st, str(d.get("reason") or st), "Q01", "")

    # ③ 评审 issue（问题归因）
    for review in (state.get("reviews") or []):
        for issue in (review.get("issues") or []):
            add("评审_issue", str(issue.get("issue_code") or issue.get("primary_dimension") or "ISSUE"),
                str(issue.get("rollback_to") or "R01"), str(issue.get("reason") or ""))

    # ④ 可用性 / 业务停止 / 输入质量
    if status == "technical_failed":
        add("可用性_技术失败", "TECHNICAL_FAILED", "X01", "任务技术失败，可检查点续跑", "P3")
    if status == "stopped":
        add("可用性_业务停止", str(state.get("stop_reason") or "STOPPED"), "X02", "业务停止（重写耗尽/无可用陈述等）", "P3")
    iq = state.get("input_quality")
    if isinstance(iq, dict) and str(iq.get("level") or "").lower() == "low":
        add("输入_低质量", "INPUT_LOW_QUALITY", "N01", "素材不足，强制降级或被拦截", "P2")

    return signals


def scan(root: str) -> dict:
    dbs = find_dbs(root)
    all_signals: list[dict] = []
    status_counter: Counter = Counter()
    db_stats: dict[str, dict] = {}
    for db in dbs:
        tasks = read_tasks(db)
        db_signals: list[dict] = []
        for task_id, video_id, status, state in tasks:
            status_counter[status or "unknown"] += 1
            db_signals.extend(collect_signals(db, task_id, video_id, status, state))
        all_signals.extend(db_signals)
        db_stats[db] = {"tasks": len(tasks), "signals": len(db_signals)}
    return {"root": root, "dbs": len(dbs), "tasks": sum(status_counter.values()),
            "status": dict(status_counter), "db_stats": db_stats, "signals": all_signals}


def render(report: dict) -> str:
    signals = report["signals"]
    out = ["# Bad Case 自动回收扫描（渠道二）", ""]
    out.append(f"- 扫描根: `{report['root']}`")
    out.append(f"- 运行库: {report['dbs']} 个，任务: {report['tasks']} 条")
    out.append("")
    out.append("## 任务状态分布")
    for status, n in sorted(report["status"].items(), key=lambda kv: -kv[1]):
        out.append(f"- {status}: {n}")
    out.append("")
    out.append("## 拦截信号（按类型 × 错误码）")
    by_kind_code = Counter((s["kind"], s["code"]) for s in signals)
    if not by_kind_code:
        out.append("- （无）")
    for (kind, code), n in by_kind_code.most_common(30):
        out.append(f"- {kind} · `{code}` × {n}")
    out.append("")
    out.append("## 按节点")
    by_node = Counter(s["node"] for s in signals)
    for node, n in by_node.most_common():
        out.append(f"- {node}: {n}")
    out.append("")
    out.append("## 按运行库")
    for db, st in report["db_stats"].items():
        out.append(f"- {db.rsplit('/runtime/', 1)[-1]}: {st['tasks']} 任务 / {st['signals']} 信号")
    out.append("")
    out.append("## 逐条信号（前 50）")
    for s in signals[:50]:
        out.append(f"- [{s['kind']}] `{s['code']}` · {s['node']} · {s['video_id']} · {s['db']}")
    out.append("")
    out.append("> 说明：本报告是「被拦截的错误」的批量统计（P2 趋势），用于发现上游质量问题；"
               "不是流出事故。穿透到人工或发布后的案例另记 templates/bad_cases.csv。")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bad Case 自动回收扫描（渠道二）")
    parser.add_argument("--root", default="runtime", help="扫描根目录（默认 runtime）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = scan(args.root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
