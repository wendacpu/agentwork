"""服务化 MVP P2 收尾演练（零成本，mock provider）。

对应 specs/serviceization-mvp/plan.md 的 P2 出口判据：
  A. 进程级「拔网线」：worker 在途时 kill -9，重启后自动续跑，丢任务 = 0；
  A2. 模型断供 = 熔断：backlog 保留、新任务拒收（429）、reset 后自动续跑；
  B. 模拟负反馈 → repair_outbox → 修复草案（triage）→ 聚合归因，
     产出待人工批准的提案清单；人工批准与重跑对照由人另行执行。

诚实边界：全部信号带 simulated=true；mock 标题与路由不证明生成/评审质量；
「修复草案」只是待核查假设，不构成修复结论，更不自动改源码或发布。
用法：python3 scripts/drill_p2_evidence.py [--out runtime/service_p2_evidence_<date>]
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.config import RunConfig  # noqa: E402
from agent_system.data import Dataset  # noqa: E402
from agent_system.model import ModelError  # noqa: E402
from agent_system.repairs import RepairStore, group_defects  # noqa: E402
from agent_system.service import ServiceApp  # noqa: E402

TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")
EVIDENCE_NOTE = ("零成本 mock 演练：证明的是队列/熔断/信号回流机制，"
                 "不证明生成质量、评审质量或任何线上效果；模拟信号非人工判定、非 CTR。")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_until(predicate, timeout=90.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def http_json(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def start_service_process(cfg_path: Path, db_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_system.service", "--config", str(cfg_path),
         "--db", str(db_path), "--port", "0", "--workers", "1",
         "--max-tasks-per-day", "50"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = wait_until(lambda: proc.stdout.readline(), timeout=30) or ""
    # "agent-service on http://127.0.0.1:49442  db=..."
    port = int(line.split("http://127.0.0.1:")[1].split()[0]) if "http://127.0.0.1:" in line else 0
    if not port:
        proc.kill()
        raise RuntimeError(f"服务未报端口：{line!r}")
    return proc, port


def queue_counts(port: int) -> dict:
    return http_json(port, "GET", "/queue")[1]["counts"]


def task_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT task_id,video_id,state,result_status,error FROM task_queue")]
    finally:
        conn.close()


def write_batch_manifest(out: Path, db: Path, dataset: Dataset) -> None:
    """评审台兼容清单：只服务展示与入口，不是质量证据，不改任何冻结集合。"""
    by_vid = {r["videoID"]: r for r in dataset.rows}
    samples = []
    for i, r in enumerate(task_rows(db), 1):
        row = by_vid.get(r["video_id"]) or {}
        samples.append({"index": i, "video_id": r["video_id"], "task_id": r["task_id"],
                        "task_state": r["state"], "result_status": r["result_status"],
                        "category": row.get("采集品类") or row.get("内容领域") or "",
                        "domain_1": (str(row.get("内容领域") or "").split("-") + [""])[0],
                        "domain_2": (str(row.get("内容领域") or "").split("-") + ["", ""])[1],
                        "form": row.get("内容形态") or "", "title": row.get("title") or "",
                        "asset_dir": str(ASSETS / r["video_id"]), "duration": row.get("duration")})
    manifest = {"version": f"drill-{datetime.now(timezone.utc).date().isoformat()}",
                "created": now_iso()[:10],
                "role": "队列演练批次的评审台兼容清单（mock 标题，仅机制演示，不构成质量证据）",
                "limitations": [EVIDENCE_NOTE],
                "sample_size": len(samples), "samples": samples}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def usable_vids(dataset: Dataset) -> list[str]:
    """跳过缺素材的行：演练要证明的是队列机制，不是数据完整性。"""
    missing = set(dataset.audit().get("missing_assets") or [])
    return [r["videoID"] for r in dataset.rows if r["videoID"] not in missing]


def phase_a(out: Path, db: Path, cfg_path: Path, vids: list[str], evidence: dict) -> None:
    """进程级拔网线：kill 在途任务 → 重启 → 全部续到终态。"""
    steps = {"videos": vids}
    proc, port = start_service_process(cfg_path, db)
    steps["first_port"] = port
    for vid in vids:
        code, body = http_json(port, "POST", "/tasks", {"video_id": vid, "source": "drill"})
        assert code == 202, (code, body)
    def in_flight():
        counts = queue_counts(port)
        return counts if counts.get("running", 0) >= 1 and counts.get("queued", 0) >= 2 else None
    steps["snapshot_before_kill"] = wait_until(in_flight) or queue_counts(port)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    steps["killed"] = {"pid": proc.pid, "at": now_iso()}

    proc2, port2 = start_service_process(cfg_path, db)
    done = wait_until(lambda: (lambda c: c.get("queued", 0) == 0 and c.get("running", 0) == 0)(queue_counts(port2)), timeout=180)
    rows = task_rows(db)
    steps["final_rows"] = rows
    steps["final_counts"] = queue_counts(port2)
    with sqlite3.connect(db) as conn:
        materials = dict(conn.execute(
            "SELECT task_id, count(*) FROM material_store GROUP BY task_id"))
        recovered = conn.execute("SELECT count(*) FROM events WHERE event_type='running_recovered'").fetchone()[0]
    steps["materials_per_task"] = materials
    steps["running_recovered_events"] = recovered
    lost = [r for r in rows if r["state"] not in ("done", "failed")]
    assert not lost, f"丢任务：{lost}"
    assert recovered >= 1, "重启必须记录 running_recovered 事件"
    assert all(materials.get(r["task_id"], 0) >= 1 for r in rows), "终态任务必须写出物料"
    proc2.terminate()
    proc2.wait(timeout=10)
    steps["lost_tasks"] = 0
    evidence["phase_a_recovery"] = steps


def phase_a2(out: Path, db: Path, config: RunConfig, dataset: Dataset, evidence: dict) -> None:
    """模型断供演练：熔断 → backlog 保留 + 拒收新任务 → 恢复 → 自动续跑。"""
    state = {"outage": True}

    def factory(adapter):
        class Guarded:
            def complete(self, **kwargs):
                if state["outage"]:
                    raise ModelError("HTTP 402 Payment Required (drill: simulated outage)", retryable=False)
                return adapter.complete(**kwargs)
        return Guarded()

    app = ServiceApp(config, dataset, str(db), adapter_factory=factory, max_tasks_per_day=50)
    steps: dict = {}
    app.start_workers(1)  # 先起 worker：断供下第一任务必须真实撞上 402 触发熔断
    outage_task_ids = []
    for vid in usable_vids(dataset)[:2]:
        code, body = app.handle_post_task({"video_id": vid, "source": "drill-outage"})
        assert code == 202, (code, body)
        outage_task_ids.append(body["task_id"])
    # 等 worker 撞断供触发熔断（第二个任务此时应留在 backlog）。
    def trip():
        return app.breaker.open
    wait_until(trip, timeout=60)
    steps["circuit_open"] = {"open": app.breaker.open, "reason": app.breaker.reason}
    steps["backlog_while_open"] = app.store.counts()
    code, body = app.handle_post_task({"video_id": usable_vids(dataset)[2], "source": "drill-outage"})
    steps["new_task_rejected"] = {"code": code, "error": body.get("error")}
    assert code == 429, "熔断期间必须拒收新任务而不是囤积"
    # 恢复：模型可用 + 显式 reset（生产语义=运维确认账单/额度后操作）。
    state["outage"] = False
    app.breaker.reset()
    done = wait_until(lambda: (lambda c: c["queued"] == 0 and c["running"] == 0)(app.store.counts()), timeout=180)
    rows = {r["task_id"]: r for r in task_rows(db)}
    steps["outage_tasks"] = [dict(rows[t]) for t in outage_task_ids]
    steps["recovered"] = bool(done)
    assert done, "熔断恢复后 backlog 必须自动续到终态"
    app.shutdown()
    evidence["phase_a2_outage"] = steps


def phase_b(out: Path, db: Path, evidence: dict) -> None:
    """模拟负反馈 → repair_outbox → 修复草案（triage）→ 聚合归因 → 待人工批准清单。"""
    config = RunConfig.from_json(str(out / "mock_config.json"))
    config.asset_root = str(ASSETS)
    config.output_dir = str(out)
    app = ServiceApp(config, Dataset.from_paths(TABLE, ASSETS, "filtered"), str(db), max_tasks_per_day=200)
    steps: dict = {"simulated_note": EVIDENCE_NOTE}
    row = app.store.conn.execute(
        "SELECT * FROM material_store WHERE is_recommended=1 AND delivery_state='recommended' "
        "AND superseded_by IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        # mock 路由常落 needs_review；构造一行可消费物料演示机制，并如实记录。
        base = app.store.conn.execute(
            "SELECT * FROM material_store WHERE superseded_by IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        assert base, "演练库必须有物料行"
        app.store.conn.execute(
            "INSERT INTO material_store(material_id,video_id,task_id,candidate_id,full_title,is_recommended,"
            "delivery_state,version_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("mat-drill-demo", base["video_id"], base["task_id"], base["candidate_id"], base["full_title"],
             1, "recommended", base["version_fingerprint"], now_iso()))
        app.store.conn.commit()
        steps["constructed_demo_material"] = "mock 路由无 recommended 交付；构造演示行仅用于机制演练，已如实标注"
        row = app.store.conn.execute("SELECT * FROM material_store WHERE material_id='mat-drill-demo'").fetchone()
    material = dict(row)
    steps["material"] = {k: material[k] for k in ("material_id", "video_id", "task_id", "candidate_id", "full_title")}

    signal_body = {"material_id": material["material_id"], "event": "negative_feedback",
                   "ts": now_iso(), "idempotency_key": f"drill-{now_iso()}",
                   "payload": {"reason": "drill: simulated negative feedback"}}
    code, first = app.handle_post_signal(signal_body)
    steps["signal_first"] = {"code": code, "simulated": first.get("simulated"),
                             "repair_event_id": first.get("repair_event_id")}
    assert code == 202, (code, first)
    code2, second = app.handle_post_signal(signal_body)
    steps["signal_repeat"] = {"code": code2, "deduplicated": second.get("deduplicated")}
    assert code2 == 200 and second.get("deduplicated"), "重复信号必须幂等"

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    outbox = conn.execute("SELECT * FROM repair_outbox WHERE event_id=?", (first["repair_event_id"],)).fetchone()
    defect = conn.execute("SELECT * FROM repair_defects WHERE revision=?", (first["repair_event_id"],)).fetchone()
    human = conn.execute("SELECT count(*) c FROM human_decisions WHERE task_id=?", (material["task_id"],)).fetchone()["c"]
    event_trace = conn.execute("SELECT * FROM events WHERE task_id=? AND event_type='simulated_signal_received'",
                               (material["task_id"],)).fetchone()
    assert outbox["status"] == "delivered" and defect["status"] == "triage", (dict(outbox), dict(defect))
    assert human == 0, "模拟信号绝不能写人工判定"
    assert event_trace, "必须有 simulated_signal_received 事件留痕"
    steps["repair_outbox"] = {k: outbox[k] for k in ("event_id", "feedback_key", "status")}
    steps["repair_defect"] = {"feedback_key": defect["feedback_key"], "status": defect["status"],
                              "diagnosis": json.loads(defect["diagnosis"]), "proposal": json.loads(defect["proposal"])}
    steps["human_decisions_on_task"] = human
    groups = group_defects(RepairStore(conn))
    steps["grouped_attribution"] = groups
    conn.close()

    pending = {
        "note": "以下修复草案来自模拟负反馈（非人工判定、非 CTR）。批准需人工给出五要素：root_cause / change / regression_tests / counterexamples / acceptance。批准≠发布：发布另有 release 门禁，绝不自动改源码。",
        "proposals": [{"proposal_id": f"{d['feedback_key']}@{d['revision']}",
                       "status": d["status"], "headline": d["payload"]["candidate"].get("headline"),
                       "source": d["payload"].get("source"), "reason": d["payload"]["decision"].get("reason"),
                       "diagnosis_family": (d["diagnosis"] or {}).get("family")}
                      for d in RepairStore(conn_dummy(db)).list() if d["status"] == "triage"],
    }
    (out / "pending_approval.json").write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    app.shutdown()
    evidence["phase_b_signal_to_draft"] = steps


def conn_dummy(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=f"runtime/service_p2_evidence_{datetime.now(timezone.utc).date().isoformat()}")
    args = parser.parse_args()
    out = (ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    db = out / "agent.db"
    cfg_path = out / "mock_config.json"
    cfg_path.write_text(json.dumps({
        "model_provider": "mock", "generation_model": "mock", "review_model": "mock",
        "asset_root": str(ASSETS), "table": str(TABLE), "output_dir": str(out),
        "ocr_mode": "filtered", "target_count": 3, "max_chars": 20,
        "schema_retry_limit": 1, "memory_call_limit": 0,
        "case_bank_isolated": True, "cache_bank_isolated": True, "verification_enabled": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    config = RunConfig.from_json(str(cfg_path))
    dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")
    vids = usable_vids(dataset)[:6]

    evidence: dict = {"note": EVIDENCE_NOTE, "started_at": now_iso(), "db": str(db)}
    phase_a(out, db, cfg_path, vids, evidence)
    phase_a2(out, db, config, dataset, evidence)
    phase_b(out, db, evidence)
    evidence["finished_at"] = now_iso()
    write_batch_manifest(out, db, dataset)
    (out / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "note": EVIDENCE_NOTE}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
