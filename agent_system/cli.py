from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .config import RunConfig
from .data import Dataset
from .model import MockAdapter, OpenAICompatibleAdapter
from .pipeline import Pipeline
from .report import render_task
from .storage import Storage
from .types import CaseRecord, HumanDecision
from .memory import MemoryStore
from .debugger import serve as serve_debugger
from .goldsheet import record as record_gold, remove as remove_gold
from .verification.search import create_search_provider


ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "data" / "source" / "video_dataset.json"


def build(args: argparse.Namespace):
    config = RunConfig.from_json(args.config) if args.config else RunConfig()
    config.apply_env()
    dataset = Dataset.from_paths(TABLE, config.asset_root, config.ocr_mode)
    storage = Storage(Path(config.output_dir) / "agent.db")
    return config, dataset, storage, make_pipeline(config, dataset, storage)


PROVIDER_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "kimi": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "juflow": ("https://juflowai.com/v1", "JUFLOW_API_KEY"),
}


def make_pipeline(config: RunConfig, dataset: Dataset, storage: Storage) -> Pipeline:
    if config.model_provider == "mock":
        search_provider = create_search_provider(config.verification_provider, secret_id_env=config.verification_secret_id_env, secret_key_env=config.verification_secret_key_env) if config.verification_enabled else None
        return Pipeline(dataset, storage, MockAdapter(), config, MockAdapter(), search_provider)
    if config.model_provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"unknown provider: {config.model_provider}")
    default_base, default_key = PROVIDER_DEFAULTS[config.model_provider]
    base, key_env = config.base_url or default_base, config.api_key_env or default_key
    adapter = OpenAICompatibleAdapter(base, key_env, config.generation_model, reasoning_effort=config.reasoning_effort)
    review_adapter = OpenAICompatibleAdapter(base, key_env, config.review_model, reasoning_effort=config.reasoning_effort)
    search_provider = create_search_provider(config.verification_provider, secret_id_env=config.verification_secret_id_env, secret_key_env=config.verification_secret_key_env) if config.verification_enabled else None
    return Pipeline(dataset, storage, adapter, config, review_adapter, search_provider)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shortvideo-agent")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit")
    sub.add_parser("ingest")
    r = sub.add_parser("run"); r.add_argument("video_id")
    b = sub.add_parser("batch"); b.add_argument("video_ids", nargs="*"); b.add_argument("--limit", type=int, default=0)
    rr = sub.add_parser("report"); rr.add_argument("task_id"); rr.add_argument("--output", default="runtime/report.html")
    rv = sub.add_parser("rereview"); rv.add_argument("task_id")
    h = sub.add_parser("human"); h.add_argument("task_id"); h.add_argument("candidate_id"); h.add_argument("decision", choices=["approved", "marginal", "rejected"]); h.add_argument("--headline"); h.add_argument("--reason", default=""); h.add_argument("--criteria", default="", help="可选问题归类 ID；不确定根因时留空"); h.add_argument("--reason-type", default="", help="可选根因归类；不确定时留空"); h.add_argument("--reviewer")
    c = sub.add_parser("case"); c.add_argument("task_id"); c.add_argument("case_id"); c.add_argument("case_type", choices=["rule", "fact", "expression"]); c.add_argument("problem_type"); c.add_argument("bad_example"); c.add_argument("good_example"); c.add_argument("applicability"); c.add_argument("--confirm", action="store_true"); c.add_argument("--scope", default=""); c.add_argument("--source-evidence", nargs="*", default=[]); c.add_argument("--case-version", default="v1")
    cl = sub.add_parser("case-list"); cl.add_argument("--all", action="store_true", help="含未确认草稿")
    cs = sub.add_parser("case-show"); cs.add_argument("case_id")
    cc = sub.add_parser("case-confirm"); cc.add_argument("case_id")
    cf = sub.add_parser("case-from"); cf.add_argument("task_id")
    ex = sub.add_parser("export-labels"); ex.add_argument("--output", default="runtime/labels.json")
    il = sub.add_parser("import-labels"); il.add_argument("path")
    dbg = sub.add_parser("debug"); dbg.add_argument("--host", default="127.0.0.1"); dbg.add_argument("--port", type=int, default=8765); dbg.add_argument("--db-path", action="append", help="可多次；缺省扫 <output_dir>/*/agent.db（含新任务默认库）")
    args = p.parse_args(argv)
    config, dataset, storage, pipeline = build(args)
    if args.cmd == "audit": print(json.dumps(dataset.audit(), ensure_ascii=False, indent=2)); return 0
    if args.cmd == "ingest":
        manifest = {"schema_version": "0.1", "asset_root": config.asset_root, "videos": []}
        for row in dataset.rows:
            video = dataset.get(row["videoID"])
            manifest["videos"].append({"video_id": video.video_id, "title": video.title, "asset_paths": video.asset_paths, "provenance": video.provenance})
        out = Path(config.output_dir) / "manifest.json"; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(out); return 0
    if args.cmd == "run": print(pipeline.run(args.video_id, "cli.run")); return 0
    if args.cmd == "batch":
        ids = args.video_ids or [r["videoID"] for r in dataset.rows][:args.limit or None]
        results = []
        for vid in ids:
            task_id = pipeline.run(vid, "cli.batch")
            results.append({"video_id": vid, "task_id": task_id})
            row = storage.get_task(task_id)
            if row["status"] == "technical_failed":
                state = json.loads(row["state_json"] or "{}")
                if not state.get("failure_retryable", False):
                    results[-1]["batch_stopped"] = "non-retryable technical failure"
                    break
        print(json.dumps(results, ensure_ascii=False, indent=2)); return 0
    if args.cmd == "report": print(render_task(storage, args.task_id, args.output)); return 0
    if args.cmd == "rereview": print(pipeline.rereview(args.task_id)); return 0
    if args.cmd == "human":
        payload = {
            "candidate_id": args.candidate_id, "decision": args.decision,
            "final_headline": args.headline, "reason": args.reason, "reviewer": args.reviewer,
            "criteria_id": args.criteria or None, "reason_type": args.reason_type or None,
        }
        storage.record_human_decision(args.task_id, payload)
        # 判定必须同时进 gold.csv，否则 eval 读不到（CLI 与调试台走同一条口径）
        row = storage.get_task(args.task_id)
        state = json.loads(row["state_json"] or "{}")
        candidate = next((c for c in (state.get("candidates") or []) if c.get("candidate_id") == args.candidate_id), {})
        route = state.get("route") or {}
        path = record_gold(
            video_id=row["video_id"], task_id=args.task_id, candidate_id=args.candidate_id,
            headline=str(candidate.get("rendered_title") or candidate.get("headline") or ""),
            verdict={"approved": "可用", "marginal": "勉强通过"}.get(args.decision, "不可用"),
            final_headline=args.headline, criteria_id=args.criteria, fallback_node="",
            reason=args.reason, reviewer=args.reviewer or "unnamed", route=str(route.get("action") or ""),
            recommended="是" if route.get("recommended_candidate_id") == args.candidate_id else "",
        )
        print(json.dumps({"task_id": args.task_id, "gold": str(path)}, ensure_ascii=False)); return 0
    if args.cmd == "case":
        case = CaseRecord(args.case_id, args.case_type, args.problem_type, args.bad_example, args.good_example, args.applicability, args.task_id, config.rule_version, args.confirm, [config.rule_version], "confirmed" if args.confirm else "draft", args.scope, args.source_evidence, args.case_version); MemoryStore(storage.conn).create_draft(case); print(args.case_id); return 0
    if args.cmd == "case-list":
        rows = [dict(r) | {"payload": json.loads(r["payload"])} for r in storage.conn.execute("SELECT case_id, payload, confirmed, rule_version FROM cases ORDER BY confirmed DESC, case_id")]
        if not args.all:
            rows = [r for r in rows if r["confirmed"]]
        if not rows:
            print("案例库是空的：先判一条（调试台或 `human` 命令），采纳/驳回会自动沉淀。")
            return 0
        print(f"{'状态':6s} {'极性':8s} {'问题类型':28s} {'标题':38s} 已采纳标题")
        print("-" * 120)
        for r in rows:
            d = r["payload"]
            bad = str(d.get("bad_example") or "")[:34]
            good = str(d.get("good_example") or "")[:34]
            state = "已确认" if r["confirmed"] else "草稿"
            print(f"{state:6s} {str(d.get('polarity') or '-'):8s} {str(d.get('problem_type') or '')[:26]:28s} {bad:38s} {good}")
        print(f"\n共 {len(rows)} 条（{storage.path}）")
        return 0
    if args.cmd == "case-show":
        row = storage.conn.execute("SELECT * FROM cases WHERE case_id=?", (args.case_id,)).fetchone()
        if not row:
            print(f"没有这条案例：{args.case_id}"); return 1
        print(json.dumps(json.loads(row["payload"]), ensure_ascii=False, indent=2)); return 0
    if args.cmd == "case-confirm":
        ok = MemoryStore(storage.conn).confirm(args.case_id)
        print(json.dumps({"case_id": args.case_id, "confirmed": ok}, ensure_ascii=False)); return 0 if ok else 2
    if args.cmd == "case-from":
        row = storage.get_task(args.task_id)
        state = json.loads(row["state_json"] or "{}")
        candidates = {c.get("candidate_id"): c for c in (state.get("candidates") or [])}
        made = []
        for decision in state.get("human_decisions") or []:
            candidate = candidates.get(decision.get("candidate_id"))
            if candidate is None:
                continue
            case_id = storage._auto_case(args.task_id, state, decision, candidate)
            if case_id:
                made.append(case_id)
        print(json.dumps({"task_id": args.task_id, "cases": made}, ensure_ascii=False)); return 0
    if args.cmd == "export-labels":
        rows = [dict(r) | {"payload": json.loads(r["payload_json"])} for r in storage.conn.execute("SELECT * FROM labels ORDER BY id")]
        out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"); print(out); return 0
    if args.cmd == "import-labels":
        labels = json.loads(Path(args.path).read_text(encoding="utf-8"))
        for label in labels:
            task_id = label.get("task_id"); payload = label.get("payload", label)
            if not task_id: raise ValueError("each label needs task_id")
            storage.add_label(task_id, payload)
        print(f"imported {len(labels)} labels"); return 0
    if args.cmd == "debug":
        out_dir = Path(config.output_dir)
        out_dir = out_dir if out_dir.is_absolute() else ROOT / out_dir
        # 新任务写 out_dir/agent.db；已有任务散在 out_dir/*/agent.db，全部纳入评审台
        paths = [out_dir / "agent.db"] + sorted(out_dir.glob("*/agent.db")) if not args.db_path else [Path(x) for x in args.db_path]
        storage.conn.close()
        serve_debugger(config, dataset, [str(p) for p in paths], lambda db: make_pipeline(config, dataset, db), args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

