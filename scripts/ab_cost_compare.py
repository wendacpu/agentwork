#!/usr/bin/env python3
"""A/B 对照：同一批 20 条视频在「规则进 system + 压缩小载荷」前后的成本与路由差异。

旧臂 = runtime/v_eval_fixed_20261104（2026-11-04，旧代码），
新臂 = runtime/cost_ab_20261105（本次，代码变了、配置/模型/视频集逐字一致）。
两边都用事件表的 usage 计费口径（model_reply + model_reply_invalid），只读、不再花钱。

输出三张表：①节点级 token/次数对比；②每条视频的路由与状态对照（语义回归检查：
路由翻转的样本要能在评审台里人工看一眼）；③压缩小载荷事件的新旧载荷字符数。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

BILLABLE = ("model_reply", "model_reply_invalid")


def node_cost(db: Path) -> dict:
    db = Path(db).resolve()
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        agg = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0, "cached": 0})
        for (pj,) in conn.execute("select payload_json from events where event_type in ('model_reply','model_reply_invalid')"):
            p = json.loads(pj)
            u = p.get("usage") or {}
            row = agg[p.get("task") or "?"]
            row["calls"] += 1
            row["prompt"] += int(u.get("prompt_tokens") or 0)
            row["completion"] += int(u.get("completion_tokens") or 0)
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            if cached is None:
                cached = u.get("prompt_cache_hit_tokens") or 0
            row["cached"] += int(cached)
        routes = {}
        for vid, status in conn.execute("select video_id, status from tasks"):
            routes[vid] = status
        final = {}
        for tid, pj in conn.execute("select task_id, payload_json from events where event_type='quality_route'"):
            p = json.loads(pj)
            final[tid] = (p.get("action"), p.get("reason"))
        tmap = {tid: vid for tid, vid in conn.execute("select task_id, video_id from tasks")}
        routes_final = {v: final.get(t) for t, v in tmap.items()}
        compress = [json.loads(pj) for (pj,) in conn.execute("select payload_json from events where event_type='compress_small_request'")]
    return {"nodes": dict(agg), "status": routes, "final_route": routes_final, "compress": compress}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="runtime/v_eval_fixed_20261104/agent.db")
    ap.add_argument("--new", default="runtime/cost_ab_20261105/agent.db")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    old, new = node_cost(Path(args.old)), node_cost(Path(args.new))

    def tot(agg: dict) -> int:
        return sum(r["prompt"] + r["completion"] for r in agg.values())

    lines = []
    lines.append(f"{'节点':14s} {'旧calls':>7s} {'旧prompt':>10s} {'旧cached%':>9s} | {'新calls':>7s} {'新prompt':>10s} {'新cached%':>9s} | {'Δprompt':>9s}")
    for name in ("understand", "boundary", "highlight", "generate", "rewrite", "review", "thesis_review", "thesis_consistency", "verify"):
        o, n = old["nodes"].get(name, {}), new["nodes"].get(name, {})
        if not o and not n:
            continue
        op, np_ = o.get("prompt", 0), n.get("prompt", 0)
        oc = 100 * o.get("cached", 0) / op if op else 0
        nc = 100 * n.get("cached", 0) / np_ if np_ else 0
        delta = f"{100 * (np_ - op) / op:+.1f}%" if op else "—"
        lines.append(f"{name:14s} {o.get('calls',0):7d} {op:10,d} {oc:8.1f}% | {n.get('calls',0):7d} {np_:10,d} {nc:8.1f}% | {delta:>9s}")
    lines.append("")
    lines.append(f"总 token：旧 {tot(old['nodes']):,} → 新 {tot(new['nodes']):,}（{100 * (tot(new['nodes']) - tot(old['nodes'])) / max(1, tot(old['nodes'])):+.1f}%）")
    lines.append("")
    lines.append("视频级路由对照（旧→新，仅列变化）：")
    flips = 0
    for vid in sorted(set(old["status"]) | set(new["status"])):
        o = (old["status"].get(vid), old["final_route"].get(vid))
        n = (new["status"].get(vid), new["final_route"].get(vid))
        if o != n:
            flips += 1
            lines.append(f"  {vid}: {o} -> {n}")
    if not flips:
        lines.append("  （无翻转）")
    lines.append("")
    lines.append(f"压缩小载荷事件 {len(new['compress'])} 次（旧臂没有该事件）：")
    for e in new["compress"]:
        saved = e["full_payload_chars"] - e["compress_payload_chars"]
        lines.append(f"  {e['parent']}: {e['full_payload_chars']:,} -> {e['compress_payload_chars']:,} 字符（省 {saved:,}）")
    out = {"old_total": tot(old["nodes"]), "new_total": tot(new["nodes"]),
           "old_nodes": old["nodes"], "new_nodes": new["nodes"],
           "route_flips": flips, "compress_events": new["compress"]}
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else "\n".join(lines))


if __name__ == "__main__":
    main()
