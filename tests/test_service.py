"""服务化 MVP（agent_system/service.py）回归。

全部用 mock provider，零模型成本；每个测试独立临时库，不碰 runtime/。
覆盖设计契约的出口判据：入队→领取→终态→物料写出、幂等、预算闸、
熔断、supersede 版本链、崩溃恢复、HTTP 环回。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from agent_system.config import RunConfig
from agent_system.data import Dataset
from agent_system.model import ModelError
from agent_system.service import ServiceApp, fatal_payment_or_auth, make_handler

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
ASSETS = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-100")
VIDEO = "BV1Ltsf6xEpV"


def wait_for(predicate, timeout=90.0, interval=0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def payment_error_factory(adapter):
    class Failing:
        def complete(self, **kwargs):
            raise ModelError("HTTP 402 Payment Required", retryable=False)
    return Failing()


class ServiceAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        self.config = RunConfig(asset_root=str(ASSETS), output_dir=self.tmp.name)
        self.dataset = Dataset.from_paths(TABLE, ASSETS, "filtered")

    def make_app(self, adapter_factory=None, max_tasks_per_day=50, workers=1) -> ServiceApp:
        app = ServiceApp(self.config, self.dataset, self.db,
                         adapter_factory=adapter_factory, max_tasks_per_day=max_tasks_per_day)
        if workers:
            app.start_workers(workers)
        self.addCleanup(app.shutdown)
        return app

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 判据 1：入队→终态→物料写出 ----

    def test_enqueue_runs_to_terminal_and_writes_materials(self):
        app = self.make_app()
        code, body = app.handle_post_task({"video_id": VIDEO, "source": "test"})
        self.assertEqual(code, 202)
        task_id = body["task_id"]
        finished = wait_for(lambda: (app.store.queue_row(task_id) or {}).get("state") in ("done", "failed"))
        row = app.store.queue_row(task_id)
        self.assertTrue(finished, f"任务未在时限内到达终态：{row}")
        self.assertEqual(row["state"], "done", f"worker 失败：{row['error']}")
        status, detail = app.handle_get_task(task_id)
        self.assertEqual(status, 200)
        self.assertNotIn(detail["status"], ("prepared", "generating", "reviewing"), "GET /tasks 必须回终态")
        materials = app.store.materials(video_id=VIDEO)
        self.assertGreaterEqual(len(materials), 1, "任务终态必须写出物料行")
        for m in materials:
            self.assertTrue(m["full_title"])
            self.assertEqual(m["task_id"], task_id)
            self.assertIn(m["delivery_state"], ("recommended", "not_recommended", "needs_review", "rejected", "failed"))
            fp = json.loads(m["version_fingerprint"])
            self.assertTrue(fp.get("rule_version") or fp.get("prompt_pack_version"), "物料必须带版本指纹")
        budget = app.store.usage_snapshot()
        self.assertEqual(budget["tasks"], 1)

    # ---- 判据 4：幂等 ----

    def test_idempotency_key_deduplicates(self):
        app = self.make_app()
        code1, first = app.handle_post_task({"video_id": VIDEO, "idempotency_key": "k-1"})
        code2, second = app.handle_post_task({"video_id": VIDEO, "idempotency_key": "k-1"})
        self.assertEqual(code1, 202)
        self.assertEqual(code2, 200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["task_id"], second["task_id"])
        counts = app.store.counts()
        self.assertEqual(counts["queued"] + counts["running"] + counts["done"] + counts["failed"], 1)

    def test_budget_gate_rejects_over_quota(self):
        app = self.make_app(workers=0, max_tasks_per_day=1)
        code1, _ = app.handle_post_task({"video_id": VIDEO})
        self.assertEqual(code1, 202)
        code2, body2 = app.handle_post_task({"video_id": VIDEO, "idempotency_key": "other"})
        self.assertEqual(code2, 429)
        self.assertEqual(body2["error"], "budget_exhausted")
        self.assertEqual(app.store.counts()["queued"], 1)

    def test_unknown_video_404_and_refunds_budget(self):
        app = self.make_app(workers=0)
        code, body = app.handle_post_task({"video_id": "BVnotexist"})
        self.assertEqual(code, 404)
        self.assertIn("video not in dataset", body["error"])
        self.assertEqual(app.store.usage_snapshot()["tasks"], 0, "prepare 失败必须归还配额")

    # ---- 护栏：熔断 ----

    def test_circuit_trips_on_payment_error_and_blocks_new_tasks(self):
        app = self.make_app(adapter_factory=payment_error_factory)
        code, body = app.handle_post_task({"video_id": VIDEO})
        self.assertEqual(code, 202)
        tripped = wait_for(lambda: app.breaker.open)
        self.assertTrue(tripped, "402 必须触发熔断（口径同 run_acceptance）")
        code2, body2 = app.handle_post_task({"video_id": VIDEO, "idempotency_key": "k-2"})
        self.assertEqual(code2, 429)
        self.assertEqual(body2["error"], "circuit_open")
        status, queue = app.handle_get_queue()
        self.assertTrue(queue["circuit"]["open"])
        code3, _ = app.handle("POST", "/admin/circuit", {}, {"reset": True})
        self.assertEqual(code3, 200)
        self.assertFalse(app.breaker.open)

    def test_fatal_detection_matches_run_acceptance_scope(self):
        self.assertTrue(fatal_payment_or_auth(ModelError("HTTP 402 Payment Required")))
        self.assertTrue(fatal_payment_or_auth(ModelError("missing API key environment variable: X")))
        self.assertTrue(fatal_payment_or_auth(RuntimeError("HTTP 401 Unauthorized")))
        self.assertFalse(fatal_payment_or_auth(ModelError("HTTP 500 upstream")))
        self.assertFalse(fatal_payment_or_auth(RuntimeError("解析失败")))

    # ---- 物料出口：supersede 版本链 ----

    def test_supersede_chain_keeps_history(self):
        app = self.make_app()
        _, first = app.handle_post_task({"video_id": VIDEO})
        self.assertTrue(wait_for(lambda: (app.store.queue_row(first["task_id"]) or {}).get("state") == "done"))
        _, second = app.handle_post_task({"video_id": VIDEO})
        self.assertTrue(wait_for(lambda: (app.store.queue_row(second["task_id"]) or {}).get("state") == "done"))
        latest = app.store.materials(video_id=VIDEO)
        self.assertTrue(latest)
        self.assertEqual({m["task_id"] for m in latest}, {second["task_id"]}, "默认只回最新版")
        history = app.store.materials(video_id=VIDEO, include_superseded=True)
        old = [m for m in history if m["task_id"] == first["task_id"]]
        self.assertTrue(old, "历史版本必须保留")
        for m in old:
            self.assertEqual(m["superseded_by"], second["task_id"])

    # ---- 崩溃恢复：running 复位 queued 并续完 ----

    def test_startup_recovers_running_tasks(self):
        app = self.make_app(workers=0)
        _, body = app.handle_post_task({"video_id": VIDEO})
        task_id = body["task_id"]
        # 模拟 worker 领取后进程被杀：队列行停在 running、任务 state 未清。
        app.store.conn.execute("UPDATE task_queue SET state='running' WHERE task_id=?", (task_id,))
        app.store.conn.commit()
        app.start_workers(1)  # 内部先 recover_running
        self.assertTrue(wait_for(lambda: (app.store.queue_row(task_id) or {}).get("state") == "done", timeout=120),
                        "复位后必须续跑到终态")
        self.assertEqual(app.store.counts()["running"], 0)

    # ---- HTTP 环回：真实 server 一次 ----

    def test_http_roundtrip(self):
        app = self.make_app()
        server = HTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=5)))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        req = urllib.request.Request(f"{base}/tasks", data=json.dumps({"video_id": VIDEO}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 202)
            task_id = json.loads(resp.read())["task_id"]
        with urllib.request.urlopen(f"{base}/queue", timeout=10) as resp:
            queue = json.loads(resp.read())
        self.assertIn("counts", queue)
        self.assertTrue(wait_for(lambda: (app.store.queue_row(task_id) or {}).get("state") == "done"))
        with urllib.request.urlopen(f"{base}/materials?video_id={VIDEO}", timeout=10) as resp:
            payload = json.loads(resp.read())
        self.assertGreaterEqual(payload["count"], 1)


if __name__ == "__main__":
    unittest.main()
