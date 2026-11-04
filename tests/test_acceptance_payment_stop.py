import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_system.model import ModelError
from agent_system.storage import Storage
from scripts.run_acceptance import BatchStop, GuardedAdapter, fatal_payment_or_auth, run_one


def test_fatal_errors_are_specific():
    for error in ('HTTP 401: Unauthorized', 'HTTP 402: Payment Required', 'HTTP 403: Forbidden',
                  'missing API key environment variable: KEY'):
        assert fatal_payment_or_auth(error)
    for error in ('HTTP 429: rate limit', 'HTTP 500: server error', 'HTTP 400: bad request', 'unexpected 402 tokens'):
        assert not fatal_payment_or_auth(error)


def test_guard_stops_future_calls_but_allows_inflight():
    stop = BatchStop()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Adapter:
        def complete(self, **kwargs):
            calls.append(kwargs['name'])
            if kwargs['name'] == 'inflight':
                entered.set()
                assert release.wait(5)
                return 'ok'
            raise ModelError('HTTP 402: Payment Required', retryable=False)

    guarded = GuardedAdapter(Adapter(), stop)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(guarded.complete, name='inflight')
        assert entered.wait(5)
        try:
            guarded.complete(name='failure')
        except ModelError:
            pass
        try:
            guarded.complete(name='blocked')
            assert False, '应被熔断'
        except ModelError as exc:
            assert not exc.retryable
        release.set()
        assert future.result() == 'ok'
    assert calls == ['inflight', 'failure']


def test_run_one_preserves_failed_and_unstarted_samples(tmp_path):
    db = tmp_path / 'agent.db'
    store = Storage(db)
    # 直接使用测试替身模拟存储，避免依赖任务创建接口。
    store.conn.close()
    states = {str(i): {'status': 'prepared', 'state_json': '{}'} for i in range(3)}
    calls = []

    class Conn:
        def close(self): pass
        def execute(self, *args): pass
        def commit(self): pass

    class FakeStorage:
        def __init__(self, path): self.conn = Conn()
        def get_task(self, task_id): return states[task_id]
        def set_state(self, task_id, status, state):
            states[task_id] = {'status': status, 'state_json': json.dumps(state)}

    class Adapter:
        def complete(self, **kwargs):
            calls.append(1)
            raise ModelError('HTTP 402: Payment Required', retryable=False)

    class Pipeline:
        def __init__(self, store):
            self.store = store
            self.adapter = Adapter()
            self.review_adapter = self.adapter
        def execute(self, task_id):
            try:
                self.adapter.complete()
            except Exception as exc:
                self.store.set_state(task_id, 'technical_failed', {'error': str(exc), 'candidates': ['partial']})

    stop = BatchStop()
    with patch('scripts.run_acceptance.Storage', FakeStorage), patch('scripts.run_acceptance.make_pipeline', side_effect=lambda c, d, s: Pipeline(s)):
        results = [run_one({'task_id': str(i), 'index': i, 'category': 'x', 'video_id': str(i)},
                           None, None, db, 2, threading.Lock(), stop) for i in range(3)]
    assert len(calls) == 1
    assert [r['status'] for r in results] == ['technical_failed', 'stopped', 'stopped']
    assert [r['attempts'] for r in results] == [1, 0, 0]
    assert results[0]['candidate_count'] == 1
    assert all(r['stop_reason'] == 'batch_stopped_payment_or_auth' for r in results)
    assert all(states[str(i)]['status'] == 'stopped' for i in (1, 2))


def test_swallowed_blocked_call_marks_task_failed(tmp_path):
    state = {"status": "prepared", "state_json": "{}"}
    stop = BatchStop()
    calls = []

    class Conn:
        def close(self): pass

    class Store:
        def __init__(self, path): self.conn = Conn()
        def get_task(self, task_id): return state.copy()
        def set_state(self, task_id, status, data):
            state.update(status=status, state_json=json.dumps(data))

    class Adapter:
        def complete(self, **kwargs): calls.append(1)

    class Pipeline:
        def __init__(self, store):
            self.store = store
            self.adapter = self.review_adapter = Adapter()
        def execute(self, task_id):
            stop.trip("HTTP 402: Payment Required")
            try:
                self.review_adapter.complete()
            except ModelError:
                pass
            self.store.set_state(task_id, "pending_human", {"candidates": ["partial"]})

    with patch("scripts.run_acceptance.Storage", Store), patch("scripts.run_acceptance.make_pipeline", side_effect=lambda c, d, s: Pipeline(s)):
        result = run_one({"task_id": "a", "index": 1, "category": "x", "video_id": "v"}, None, None, tmp_path / "db", 1, threading.Lock(), stop)
    assert not calls
    assert result["status"] == state["status"] == "technical_failed"
    assert result["candidate_count"] == 1
    assert "batch_stopped_payment_or_auth" in result["error"]


def test_runner_exception_preserves_existing_state(tmp_path):
    task = {"status": "prepared", "state_json": "{}"}

    class Conn:
        def close(self): pass

    class Store:
        def __init__(self, path): self.conn = Conn()
        def get_task(self, task_id): return task.copy()
        def set_state(self, task_id, status, state):
            task.update(status=status, state_json=json.dumps(state))

    class Pipeline:
        def __init__(self, store):
            self.store = store
            self.adapter = self.review_adapter = object()
        def execute(self, task_id):
            self.store.set_state(task_id, "generating", {"candidates": ["partial"], "rewrite_round": 2})
            raise ValueError("failure")

    with patch("scripts.run_acceptance.Storage", Store), patch("scripts.run_acceptance.make_pipeline", side_effect=lambda c, d, s: Pipeline(s)):
        result = run_one({"task_id": "a", "index": 1, "category": "x", "video_id": "v"}, None, None, tmp_path / "db", 0, threading.Lock(), BatchStop())
    assert result["status"] == task["status"] == "runner_exception"
    assert result["candidate_count"] == 1 and result["rewrite_round"] == 2
    assert json.loads(task["state_json"])["candidates"] == ["partial"]
