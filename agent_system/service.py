"""服务化 MVP：任务队列 + worker + 物料出口 + HTTP 入口。

设计契约见 `specs/serviceization-mvp/design.md`。原则：
- 不改流水线内部；服务层只是调度器与物料出口，全部 stdlib + SQLite。
- 与任务库同库（默认 `runtime/service/agent.db`），自建 `task_queue / material_store /
  service_state / service_usage` 四表，不侵入 `storage.py`。
- 熔断与预算口径对齐 `scripts/run_acceptance.py`：401/402/403/missing key 触发批次熔断。
- 崩溃恢复靠「running 复位 queued + Pipeline 自带 checkpoint 续跑」，无租约——生产映射写在线 C 文档。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from .cli import make_pipeline
from .config import RunConfig, resolve_case_bank
from .data import Dataset
from .model import ModelError
from .storage import Storage

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLE = ROOT / "data" / "source" / "video_dataset.json"

# 与 scripts/run_acceptance.py 完全同口径的熔断识别，不另造一套。
_FATAL_RE = re.compile(r"\bHTTP\s+(?:401|402|403)\b|missing API key environment variable", re.I)


def fatal_payment_or_auth(error: object) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    return str(code) in ("401", "402", "403") or bool(_FATAL_RE.search(str(error)))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class CircuitBreaker:
    """持久化熔断状态：服务重启后仍保持 open，直到显式 reset。"""

    def __init__(self, store: "ServiceStore") -> None:
        self.store = store
        self.lock = threading.Lock()
        self._reason = ""
        saved = store.get_state("circuit")
        if saved and saved.get("open"):
            self._reason = saved.get("reason", "")

    @property
    def open(self) -> bool:
        with self.lock:
            return bool(self._reason)

    @property
    def reason(self) -> str:
        with self.lock:
            return self._reason

    def trip(self, error: object) -> None:
        with self.lock:
            if not self._reason:
                self._reason = str(error)
                self.store.set_state("circuit", {"open": True, "reason": self._reason, "at": now_iso()})

    def reset(self) -> None:
        with self.lock:
            self._reason = ""
            self.store.set_state("circuit", {"open": False, "at": now_iso()})


class GuardedAdapter:
    """在模型调用边界检查熔断；已准入的在途请求允许自然完成（口径同 run_acceptance）。"""

    def __init__(self, adapter, breaker: CircuitBreaker) -> None:
        self.adapter, self.breaker = adapter, breaker

    def complete(self, **kwargs):
        if self.breaker.open:
            raise ModelError(f"circuit_open: {self.breaker.reason}", retryable=False)
        try:
            return self.adapter.complete(**kwargs)
        except Exception as exc:
            if fatal_payment_or_auth(exc):
                self.breaker.trip(exc)
            raise


class ServiceStore:
    """服务层自建表，与任务库同文件；只调度与出口，不替代 Storage 的任务读写。"""

    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(db_path)
        # 共享连接 + 显式锁串行化：MVP 单库多线程访问的唯一安全模型。
        # worker 另建 Storage 连接执行任务，与本连接互不共享对象。
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_queue(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT UNIQUE NOT NULL, video_id TEXT NOT NULL, source TEXT,
          idempotency_key TEXT UNIQUE,
          state TEXT NOT NULL DEFAULT 'queued',
          enqueued_at TEXT, claimed_at TEXT, finished_at TEXT,
          duration_ms INTEGER, result_status TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS material_store(
          material_id TEXT PRIMARY KEY,
          video_id TEXT NOT NULL, task_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
          slot TEXT, full_title TEXT, prefix TEXT, body TEXT, char_count INTEGER,
          is_recommended INTEGER DEFAULT 0,
          delivery_state TEXT, route_action TEXT, route_reason TEXT,
          risk_flags TEXT, review_summary TEXT, version_fingerprint TEXT,
          superseded_by TEXT, created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_material_video ON material_store(video_id);
        CREATE INDEX IF NOT EXISTS idx_material_task ON material_store(task_id);
        CREATE TABLE IF NOT EXISTS service_state(key TEXT PRIMARY KEY, value_json TEXT);
        CREATE TABLE IF NOT EXISTS service_usage(date TEXT PRIMARY KEY, tasks INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER);
        """)
        self.conn.commit()
        self.lock = threading.Lock()

    # ---- 队列 ----

    def find_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM task_queue WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def enqueue(self, task_id: str, video_id: str, source: str, idempotency_key: str | None) -> dict[str, Any]:
        with self.lock:
            self.conn.execute(
                "INSERT INTO task_queue(task_id,video_id,source,idempotency_key,state,enqueued_at) VALUES(?,?,?,?,?,?)",
                (task_id, video_id, source, idempotency_key, "queued", now_iso()))
            self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM task_queue WHERE task_id=?", (task_id,)).fetchone())

    def claim(self) -> dict[str, Any] | None:
        """BEGIN IMMEDIATE + 条件更新完成互斥领取；rowcount 为 1 才算领到。"""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute("SELECT * FROM task_queue WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    self.conn.rollback()
                    return None
                cur = self.conn.execute(
                    "UPDATE task_queue SET state='running', claimed_at=? WHERE id=? AND state='queued'",
                    (now_iso(), row["id"]))
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return None
                self.conn.commit()
                return dict(row)
            except Exception:
                self.conn.rollback()
                raise

    def finish(self, task_id: str, state: str, result_status: str | None, error: str | None, duration_ms: int | None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE task_queue SET state=?, finished_at=?, duration_ms=?, result_status=?, error=? WHERE task_id=?",
                (state, now_iso(), duration_ms, result_status, error, task_id))
            self.conn.commit()

    def recover_running(self) -> int:
        """服务启动时把 running 复位 queued。不清任务 state——execute 自带 checkpoint 续跑。"""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE task_queue SET state='queued', claimed_at=NULL WHERE state='running'")
            self.conn.commit()
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM task_queue GROUP BY state").fetchall()
        counts = {row["state"]: row["n"] for row in rows}
        return {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "done": counts.get("done", 0), "failed": counts.get("failed", 0)}

    def latency_stats(self) -> dict[str, Any]:
        with self.lock:
            vals = [r["duration_ms"] for r in self.conn.execute(
                "SELECT duration_ms FROM task_queue WHERE duration_ms IS NOT NULL ORDER BY duration_ms").fetchall()]
        if len(vals) < 2:
            return {"samples": len(vals), "p50_ms": None, "p95_ms": None}
        return {"samples": len(vals),
                "p50_ms": statistics.quantiles(vals, n=100)[49],
                "p95_ms": statistics.quantiles(vals, n=100)[94]}

    def queue_row(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM task_queue WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    # ---- 预算与用量 ----

    def budget_allow(self, max_tasks_per_day: int) -> tuple[bool, dict[str, Any]]:
        date = today()
        with self.lock:
            row = self.conn.execute("SELECT * FROM service_usage WHERE date=?", (date,)).fetchone()
            used = row["tasks"] if row else 0
            snapshot = {"date": date, "max_tasks": max_tasks_per_day, "used": used}
            if used >= max_tasks_per_day:
                return False, snapshot
            self.conn.execute(
                "INSERT INTO service_usage(date,tasks,prompt_tokens,completion_tokens) VALUES(?,?,0,0) "
                "ON CONFLICT(date) DO UPDATE SET tasks=tasks+1", (date, 1))
            self.conn.commit()
            return True, {"date": date, "max_tasks": max_tasks_per_day, "used": used + 1}

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO service_usage(date,tasks,prompt_tokens,completion_tokens) VALUES(?,0,?,?) "
                "ON CONFLICT(date) DO UPDATE SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?",
                (today(), prompt_tokens, completion_tokens, prompt_tokens, completion_tokens))
            self.conn.commit()

    def usage_snapshot(self) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM service_usage WHERE date=?", (today(),)).fetchone()
        return dict(row) if row else {"date": today(), "tasks": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ---- 服务状态 ----

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT value_json FROM service_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO service_state(key,value_json) VALUES(?,?)",
                              (key, json.dumps(value, ensure_ascii=False)))
            self.conn.commit()

    # ---- 物料出口 ----

    def write_materials(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        video_id = rows[0]["video_id"]
        task_id = rows[0]["task_id"]
        with self.lock:
            # 版本链：旧行指向新 task；已 superseded 的行保持不动，只追加不覆盖。
            self.conn.execute(
                "UPDATE material_store SET superseded_by=? WHERE video_id=? AND superseded_by IS NULL AND task_id<>?",
                (task_id, video_id, task_id))
            for row in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO material_store(material_id,video_id,task_id,candidate_id,slot,full_title,prefix,body,char_count,is_recommended,delivery_state,route_action,route_reason,risk_flags,review_summary,version_fingerprint,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["material_id"], row["video_id"], row["task_id"], row["candidate_id"], row["slot"],
                     row["full_title"], row["prefix"], row["body"], row["char_count"], row["is_recommended"],
                     row["delivery_state"], row["route_action"], row["route_reason"],
                     row["risk_flags"], row["review_summary"], row["version_fingerprint"], row["created_at"]))
            self.conn.commit()

    def materials(self, video_id: str | None = None, task_id: str | None = None, include_superseded: bool = False) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM material_store WHERE 1=1", []
        if video_id:
            sql += " AND video_id=?"
            params.append(video_id)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY created_at, candidate_id"
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def build_material_rows(task: dict[str, Any], state: dict[str, Any], config: RunConfig) -> list[dict[str, Any]]:
    """把任务终态翻译成对外物料行。delivery_state 只描述系统判定，不冒充人工结论。"""
    route = state.get("route") or {}
    recommended = route.get("recommended_candidate_id")
    eligible = set(route.get("eligible_candidate_ids") or [])
    decisions = {str(d.get("candidate_id")): d for d in state.get("candidate_decisions") or [] if isinstance(d, dict)}
    task_status = task.get("status") or ""
    fingerprint = json.dumps({
        "rule_version": getattr(config, "rule_version", None),
        "prompt_pack_version": getattr(config, "prompt_pack_version", None),
        "generation_model": getattr(config, "generation_model", None),
        "review_model": getattr(config, "review_model", None),
        "run_manifest": (state.get("run_manifest") or {}),
        "prompt_hashes": state.get("prompt_hashes") or {},
    }, ensure_ascii=False)
    rows: list[dict[str, Any]] = []
    for c in state.get("candidates") or []:
        cid = str(c.get("candidate_id") or "")
        if not cid:
            continue
        mech = c.get("mechanical_checks") or {}
        full_title = c.get("rendered_title") or mech.get("rendered_title") or c.get("headline") or ""
        body = c.get("headline") or ""
        decision = decisions.get(cid) or {}
        d_status = str(decision.get("status") or "")
        if route.get("action") == "handoff" and cid == recommended:
            delivery = "recommended"
        elif cid in eligible:
            delivery = "not_recommended"
        elif d_status in ("needs_review", "rewrite") or route.get("action") == "needs_review":
            delivery = "needs_review"
        elif task_status in ("stopped", "technical_failed"):
            delivery = "failed"
        else:
            delivery = "rejected"
        risk = {
            "policy_hits": mech.get("policy_hits") or [],
            "limit_pending": mech.get("limit_pending") or [],
            "verification_risk": state.get("verification_risk") or {},
            "mechanical_error_codes": [e.get("code") for e in (mech.get("errors") or []) if isinstance(e, dict)],
            "decision_issue_codes": [i.get("code") for i in (decision.get("issues") or []) if isinstance(i, dict)],
        }
        review = {
            "decision_status": d_status or None,
            "issues": decision.get("issues") or [],
            "route_reason": route.get("reason"),
        }
        rows.append({
            "material_id": f"mat-{uuid.uuid4().hex[:12]}",
            "video_id": task["video_id"], "task_id": task["task_id"], "candidate_id": cid,
            "slot": c.get("slot_id") or None,
            "full_title": full_title,
            "prefix": "+".join(str(x) for x in (c.get("prefix_labels") or [])) or None,
            "body": body, "char_count": len(full_title),
            "is_recommended": 1 if cid == recommended else 0,
            "delivery_state": delivery,
            "route_action": route.get("action"), "route_reason": route.get("reason"),
            "risk_flags": json.dumps(risk, ensure_ascii=False),
            "review_summary": json.dumps(review, ensure_ascii=False),
            "version_fingerprint": fingerprint,
            "created_at": now_iso(),
        })
    return rows


class TaskWorker(threading.Thread):
    def __init__(self, app: "ServiceApp", index: int) -> None:
        super().__init__(daemon=True, name=f"svc-worker-{index}")
        self.app, self.index = app, index
        self.stop_event = threading.Event()

    def run(self) -> None:
        app = self.app
        while not self.stop_event.is_set():
            if app.breaker.open:
                # 熔断后 worker 保持存活但停止领新任务；backlog 原样保留。
                self.stop_event.wait(0.2)
                continue
            row = app.store.claim()
            if row is None:
                self.stop_event.wait(0.2)
                continue
            self.execute_row(row)

    def execute_row(self, row: dict[str, Any]) -> None:
        app = self.app
        task_id = row["task_id"]
        started = time.monotonic()
        storage = Storage(Path(app.db_path), case_bank_path=(resolve_case_bank(app.config) or None))
        pipeline = make_pipeline(app.config, app.dataset, storage)
        if app.adapter_factory is not None:
            pipeline.adapter = app.adapter_factory(pipeline.adapter)
            pipeline.review_adapter = app.adapter_factory(pipeline.review_adapter)
        pipeline.adapter = GuardedAdapter(pipeline.adapter, app.breaker)
        pipeline.review_adapter = GuardedAdapter(pipeline.review_adapter, app.breaker)
        error_text: str | None = None
        result_status: str | None = None
        try:
            storage.event(task_id, "SVC", "task_claimed", {"worker": self.name, "source": row.get("source")})
            pipeline.execute(task_id)
            queue_state = "done"
        except Exception as exc:  # 任务级失败只标记本任务，不影响其它样本。
            if fatal_payment_or_auth(exc):
                app.breaker.trip(exc)
            error_text = str(exc)[:2000]
            queue_state = "failed"
            try:
                storage.event(task_id, "SVC", "task_failed", {"error": error_text, "worker": self.name})
            except Exception:
                pass
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            task_row = storage.get_task(task_id)
            task = {"task_id": task_id, "video_id": task_row["video_id"], "status": task_row["status"]}
            result_status = task_row["status"]
            state = json.loads(task_row["state_json"] or "{}")
            app.store.write_materials(build_material_rows(task, state, app.config))
            prompt, completion = sum_task_usage(storage.conn, task_id)
            app.store.add_usage(prompt, completion)
        except Exception as exc:
            error_text = (error_text or "") + f" | material_write_error: {exc}"[:500]
            queue_state = "failed" if queue_state == "done" else queue_state
        finally:
            app.store.finish(task_id, queue_state, result_status, error_text, duration_ms)
        close_storage(storage)


def sum_task_usage(conn: sqlite3.Connection, task_id: str) -> tuple[int, int]:
    prompt = completion = 0
    for row in conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='model_reply'", (task_id,)).fetchall():
        usage = (json.loads(row["payload_json"]) or {}).get("usage") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return prompt, completion


def close_storage(storage: Storage) -> None:
    """Storage 类没有 close：逐连接关闭，独立 case/cache 库一并释放。"""
    seen: set[int] = set()
    for attr in ("conn", "case_conn", "cache_conn"):
        conn = getattr(storage, attr, None)
        if conn is None or id(conn) in seen:
            continue
        seen.add(id(conn))
        try:
            conn.close()
        except Exception:
            pass


class ServiceApp:
    """业务路由与 HTTP 解耦：所有端点先走 handle_*，测试不需要起服务器。

    `_api_storage` 不能常驻共享：SQLite 连接绑定创建线程，而 HTTPServer 在另一条
    线程处理请求。API 侧改为按请求现建现关（prepare/GET 不消费模型，建连成本毫秒级）；
    worker 侧本来就按任务建 Storage。共享的只有 ServiceStore 自身（check_same_thread=False + 锁）。
    """

    def __init__(self, config: RunConfig, dataset: Dataset, db_path: str,
                 adapter_factory: Callable | None = None,
                 max_tasks_per_day: int = 50) -> None:
        self.config, self.dataset, self.db_path = config, dataset, db_path
        self.adapter_factory = adapter_factory
        self.max_tasks_per_day = max_tasks_per_day
        self.store = ServiceStore(db_path)
        self.breaker = CircuitBreaker(self.store)
        self.workers: list[TaskWorker] = []

    def _open_storage(self) -> Storage:
        return Storage(Path(self.db_path), case_bank_path=(resolve_case_bank(self.config) or None))

    def start_workers(self, n: int) -> None:
        recovered = self.store.recover_running()
        if recovered:
            storage = self._open_storage()
            try:
                storage.event("service", "SVC", "running_recovered", {"count": recovered})
            finally:
                close_storage(storage)
        for i in range(n):
            worker = TaskWorker(self, i)
            worker.start()
            self.workers.append(worker)

    # ---- 端点逻辑 ----

    def handle_post_task(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        video_id = str(payload.get("video_id") or "")
        source = str(payload.get("source") or "api")
        idem = payload.get("idempotency_key")
        if not video_id:
            return 400, {"error": "video_id is required"}
        if idem:
            existing = self.store.find_by_idempotency(str(idem))
            if existing:
                return 200, {"task_id": existing["task_id"], "state": existing["state"], "deduplicated": True}
        if self.breaker.open:
            return 429, {"error": "circuit_open", "reason": self.breaker.reason}
        allowed, budget = self.store.budget_allow(self.max_tasks_per_day)
        if not allowed:
            return 429, {"error": "budget_exhausted", "budget": budget}
        storage = self._open_storage()
        try:
            pipeline = make_pipeline(self.config, self.dataset, storage)
            task_id = pipeline.prepare(video_id, launch_source=f"service.{source}")
        except KeyError:
            self._refund_budget()
            return 404, {"error": f"video not in dataset: {video_id}"}
        except Exception as exc:
            self._refund_budget()
            return 500, {"error": f"prepare_failed: {exc}"[:500]}
        finally:
            close_storage(storage)
        row = self.store.enqueue(task_id, video_id, source, str(idem) if idem else None)
        return 202, {"task_id": task_id, "state": row["state"], "queue": self.store.counts()}

    def _refund_budget(self) -> None:
        with self.store.lock:
            self.store.conn.execute("UPDATE service_usage SET tasks=MAX(tasks-1,0) WHERE date=?", (today(),))
            self.store.conn.commit()

    def handle_get_task(self, task_id: str) -> tuple[int, dict[str, Any]]:
        queue_row = self.store.queue_row(task_id)
        storage = self._open_storage()
        try:
            task = storage.get_task(task_id)
        except KeyError:
            return 404, {"error": f"task not found: {task_id}"}
        finally:
            close_storage(storage)
        state = json.loads(task["state_json"] or "{}")
        route = state.get("route") or {}
        candidates = [{"candidate_id": c.get("candidate_id"), "slot_id": c.get("slot_id"),
                       "full_title": c.get("rendered_title") or c.get("headline"),
                       "is_recommended": c.get("candidate_id") == route.get("recommended_candidate_id")}
                      for c in state.get("candidates") or []]
        return 200, {"task_id": task_id, "video_id": task["video_id"], "status": task["status"],
                     "queue_state": queue_row["state"] if queue_row else None,
                     "route": {"action": route.get("action"), "reason": route.get("reason"),
                               "recommended_candidate_id": route.get("recommended_candidate_id")},
                     "candidates": candidates}

    def handle_get_queue(self) -> tuple[int, dict[str, Any]]:
        return 200, {"counts": self.store.counts(),
                     "circuit": {"open": self.breaker.open, "reason": self.breaker.reason or None},
                     "budget": self.store.usage_snapshot(),
                     "latency": self.store.latency_stats()}

    def handle_get_materials(self, video_id: str | None, include_superseded: bool) -> tuple[int, dict[str, Any]]:
        rows = self.store.materials(video_id=video_id, include_superseded=include_superseded)
        return 200, {"materials": rows, "count": len(rows)}

    def handle_reset_circuit(self) -> tuple[int, dict[str, Any]]:
        self.breaker.reset()
        storage = self._open_storage()
        try:
            storage.event("service", "SVC", "circuit_reset", {"at": now_iso()})
        finally:
            close_storage(storage)
        return 200, {"circuit": {"open": False}}

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = [p for p in path.split("/") if p]
        if method == "POST" and parts == ["tasks"]:
            return self.handle_post_task(body)
        if method == "POST" and parts == ["admin", "circuit"]:
            if body.get("reset"):
                return self.handle_reset_circuit()
            return 400, {"error": "reset:true required"}
        if method == "GET" and parts == ["queue"]:
            return self.handle_get_queue()
        if method == "GET" and parts == ["materials"]:
            video_id = (query.get("video_id") or [None])[0]
            include_all = (query.get("all") or [""])[0] == "1"
            return self.handle_get_materials(video_id, include_all)
        if method == "GET" and len(parts) == 2 and parts[0] == "tasks":
            return self.handle_get_task(parts[1])
        return 404, {"error": f"no route: {method} {path}"}

    def shutdown(self) -> None:
        for worker in self.workers:
            worker.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=2)
        self.store.close()


def make_handler(app: ServiceApp):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _request(self, method: str) -> None:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json body"})
            try:
                code, payload = app.handle(method, parsed.path, query, body if isinstance(body, dict) else {})
            except Exception as exc:  # 服务层错误不能带走整个响应线程
                code, payload = 500, {"error": f"internal: {exc}"[:500]}
            self._send(code, payload)

        def do_GET(self):
            self._request("GET")

        def do_POST(self):
            self._request("POST")

        def log_message(self, *args):  # 静默访问日志
            return

    return Handler


def build_app(config_path: str | None, db_path: str, max_tasks_per_day: int) -> ServiceApp:
    config = RunConfig.from_json(config_path) if config_path else RunConfig()
    config.apply_env()
    dataset = Dataset.from_paths(config.table or DEFAULT_TABLE, config.asset_root, config.ocr_mode)
    return ServiceApp(config, dataset, db_path, max_tasks_per_day=max_tasks_per_day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-service", description=__doc__)
    parser.add_argument("--config", default=None, help="RunConfig JSON（provider/asset_root 等），同 CLI")
    parser.add_argument("--db", default="runtime/service/agent.db", help="服务与任务共用库路径")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1", help="仅允许本机回环；对外暴露不是 MVP 的职责")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tasks-per-day", type=int, default=50)
    parser.add_argument("--no-worker", action="store_true", help="只开 API，不领取任务")
    parser.add_argument("--reset-circuit", action="store_true")
    args = parser.parse_args(argv)

    app = build_app(args.config, args.db, args.max_tasks_per_day)
    if args.reset_circuit:
        app.handle_reset_circuit()
    if not args.no_worker:
        app.start_workers(max(1, args.workers))
    # 单线程 HTTP 串行处理：内部 Storage 连接受 SQLite check_same_thread 限制，
    # 只能被 serve_forever 所在线程使用；GET/POST 都是快操作，真并发在 worker 线程侧。
    # 生产映射 = 无状态 API 多实例 + 共享队列（写在线 C 文档，不在 MVP 实现）。
    server = HTTPServer((args.host, args.port), make_handler(app))
    print(f"agent-service on http://{args.host}:{server.server_address[1]}  db={args.db}  workers={args.workers}  熔断={app.breaker.open and 'OPEN:' + app.breaker.reason or 'closed'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
