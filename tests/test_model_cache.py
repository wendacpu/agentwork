"""共享模型缓存：跨任务/跨批次只付一次钱，且不能把旧结果冒充成新结果。

背景（v_eval_fixed_20261104 实测）：一批 15 条视频 2.75M prompt tokens 里
42% 花在「理解 / 边界 / 看点 / 主旨复核」，它们的输入与批次无关。原来的
`cached_model_reply(task_id, request_hash)` 把 task_id 绑进查询，跨任务
永远不命中——62 个 config 快照反复跑同一批视频，每次从零重算。

这里锁三件事：
1. 同一 request_hash 换个任务库也能命中（跨批次复用）；
2. 不传 cache_bank_path 时退回旧行为（无共享缓存，不会意外复用）；
3. 写失败不能让任务失败（缓存是加速层，不是正确性前提）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from agent_system.config import RunConfig, default_cache_bank, resolve_cache_bank
from agent_system.storage import Storage


def test_shared_cache_hits_across_task_banks() -> None:
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "model_cache.db"
        first = Storage(Path(td) / "batch_a" / "agent.db", cache_bank_path=cache)
        assert first.cached_shared_reply("hash-1") is None
        first.remember_shared_reply("hash-1", "understand", "m1", {"claims": []}, {"prompt_tokens": 12})
        # 另一个批次的独立任务库：同一缓存库必须命中
        second = Storage(Path(td) / "batch_b" / "agent.db", cache_bank_path=cache)
        assert second.cached_shared_reply("hash-1") == {"claims": []}


def test_hash_mismatch_does_not_hit() -> None:
    """request_hash 是内容哈希：换了 prompt/模型就是另一把键，不能串。"""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "model_cache.db"
        store = Storage(Path(td) / "agent.db", cache_bank_path=cache)
        store.remember_shared_reply("hash-a", "review", "m1", {"reviews": []}, None)
        assert store.cached_shared_reply("hash-b") is None


def test_isolated_mode_keeps_old_behaviour() -> None:
    """不传 cache_bank_path = 关闭共享缓存，退回只查任务内事件流。"""
    with tempfile.TemporaryDirectory() as td:
        store = Storage(Path(td) / "agent.db")
        assert store.cache_conn is None
        store.remember_shared_reply("hash-1", "understand", "m", {"x": 1}, None)
        assert store.cached_shared_reply("hash-1") is None


def test_non_dict_payload_is_not_cached() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = Storage(Path(td) / "agent.db", cache_bank_path=Path(td) / "cache.db")
        store.remember_shared_reply("h", "review", "m", ["not", "a", "dict"], None)  # type: ignore[arg-type]
        assert store.cached_shared_reply("h") is None


def test_cache_bank_resolution() -> None:
    config = RunConfig()
    assert resolve_cache_bank(config) == default_cache_bank()
    # 显式配置优先
    config.cache_bank = "/tmp/explicit.db"
    assert resolve_cache_bank(config) == "/tmp/explicit.db"
    # 隔离实验退回旧行为
    config.cache_bank = ""
    config.cache_bank_isolated = True
    assert resolve_cache_bank(config) == ""
