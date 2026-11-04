import json
import threading
from unittest.mock import patch

from scripts.run_acceptance import BatchStop, run_one


def test_non_payment_failure_still_retries(tmp_path):
    task = {'status': 'prepared', 'state_json': '{}'}
    attempts = []

    class Conn:
        def close(self): pass
        def execute(self, *args): pass
        def commit(self): pass

    class Store:
        def __init__(self, path): self.conn = Conn()
        def get_task(self, task_id): return task
        def set_state(self, task_id, status, state):
            task.update(status=status, state_json=json.dumps(state))

    class Pipeline:
        def __init__(self, store):
            self.store = store
            self.adapter = self.review_adapter = object()
        def execute(self, task_id):
            attempts.append(1)
            if len(attempts) == 1:
                self.store.set_state(task_id, 'technical_failed', {'error': 'HTTP 500: server error'})
            else:
                self.store.set_state(task_id, 'pending_human', {'candidates': ['a']})

    with patch('scripts.run_acceptance.Storage', Store), patch('scripts.run_acceptance.make_pipeline', side_effect=lambda c, d, s: Pipeline(s)):
        result = run_one({'task_id': 'a', 'index': 1, 'category': 'x', 'video_id': 'v'},
                         None, None, tmp_path / 'a.db', 1, threading.Lock(), BatchStop())
    assert result['status'] == 'pending_human'
    assert result['attempts'] == 2
    assert result['candidate_count'] == 1
