"""共享案例库：人工判定跨运行可见，且同视频检索不依赖 tasks 表。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_system.config import RunConfig, default_case_bank, resolve_case_bank
from agent_system.memory import MemoryStore
from agent_system.storage import Storage


def test_resolve_prefers_explicit_then_isolation_then_shared():
    assert resolve_case_bank(RunConfig(case_bank="/tmp/x.db")) == "/tmp/x.db"
    assert resolve_case_bank(RunConfig(case_bank_isolated=True)) == ""
    assert resolve_case_bank(RunConfig()) == default_case_bank()
    # 调试台会传入不含这些字段的轻量配置对象，不能抛 AttributeError。
    assert resolve_case_bank(type("Config", (), {})()) == default_case_bank()


def test_verdict_lands_in_shared_bank_and_survives_new_run(tmp_path):
    bank = tmp_path / "cases.db"
    task_state = {
        "video": {"video_id": "BV1", "title": "早餐店油条豆腐脑花5块", "domain": {"一级": "美食", "二级": "探店"}, "format": "探店体验"},
        "rules": {"version": "red-dot-v0.2-modular"},
        "candidates": [{"candidate_id": "a", "headline": "博主称海惠花园附近早餐店油条豆腐脑花5块"}],
        "route": {}, "reviews": [],
    }
    first = Storage(tmp_path / "run1" / "agent.db", case_bank_path=bank)
    task = first.create_task("BV1", {})
    first.set_state(task, "pending_human", task_state)
    first.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "探访变成摘录"})
    assert first.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0, "案例不应留在任务库"
    assert first.case_conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1

    # 第二次运行：新的任务库、同一个共享案例库，必须能检索到上一次的经验。
    second = Storage(tmp_path / "run2" / "agent.db", case_bank_path=bank)
    found = second.case_conn.execute("SELECT payload FROM cases").fetchone()
    assert json.loads(found[0])["video_id"] == "BV1"

    store = MemoryStore(second.case_conn)
    hits = store.search("早餐店油条豆腐脑花5块", ["表达"], "red-dot-v0.2-modular", limit=3, scope=["美食", "探店体验"])
    assert hits and hits[0]["case_id"].startswith("auto-")


def test_same_video_lookup_works_without_tasks_table(tmp_path):
    """共享库没有 tasks 表，JOIN 写法会直接报 no such table。"""
    bank = tmp_path / "cases.db"
    store = Storage(tmp_path / "run" / "agent.db", case_bank_path=bank)
    task = store.create_task("BV9", {})
    store.set_state(task, "pending_human", {
        "video": {"video_id": "BV9", "title": "标题"}, "rules": {"version": "v1"},
        "candidates": [{"candidate_id": "a", "headline": "原标题"}], "route": {}, "reviews": [],
    })
    store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "错了"})

    conn = sqlite3.connect(bank)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='tasks'").fetchone()[0] == 0
    memory = MemoryStore(conn)
    same = memory.search_for_video("BV9", "v1")
    assert len(same) == 1 and same[0]["video_id"] == "BV9"
    assert memory.search_for_video("BV-other", "v1") == []


def test_draft_cases_never_leak_into_retrieval(tmp_path):
    bank = tmp_path / "cases.db"
    store = Storage(tmp_path / "run" / "agent.db", case_bank_path=bank)
    store.case_conn.execute(
        "INSERT INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,0,?)",
        ("draft-1", json.dumps({"case_id": "draft-1", "problem_type": "表达", "bad_example": "早餐店油条豆腐脑花5块"}), "red-dot-v0.2-modular"),
    )
    store.case_conn.commit()
    assert MemoryStore(store.case_conn).search("早餐店油条豆腐脑花5块", ["表达"], "red-dot-v0.2-modular") == []


def test_isolated_config_keeps_cases_local(tmp_path):
    store = Storage(tmp_path / "run" / "agent.db", case_bank_path=(resolve_case_bank(RunConfig(case_bank_isolated=True)) or None))
    task = store.create_task("BV1", {})
    store.set_state(task, "pending_human", {
        "video": {"video_id": "BV1", "title": "标题"}, "rules": {"version": "v1"},
        "candidates": [{"candidate_id": "a", "headline": "原标题"}], "route": {}, "reviews": [],
    })
    store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "错了"})
    assert store.case_conn is store.conn
    assert store.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


def test_pipeline_actually_retrieves_across_runs(tmp_path):
    """端到端：新一次运行必须真的读到上一次运行沉淀的案例。

    这是「案例库只存不用」的回归点——单测检索能命中还不够，
    要证明 Pipeline 在共享库配置下把案例注入了生成输入。
    """
    from agent_system.model import MockAdapter
    from agent_system.pipeline import Pipeline
    from agent_system.types import Evidence, VideoInput

    bank = tmp_path / "cases.db"
    version = RunConfig().prompt_pack_version
    seed = Storage(tmp_path / "run-old" / "agent.db", case_bank_path=bank)
    seed.case_conn.execute(
        "INSERT INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,1,?)",
        ("auto-old-a", json.dumps({
            "case_id": "auto-old-a", "problem_type": "表达", "video_id": "BV-old",
            "bad_example": "早餐店油条豆腐脑花5块写成摘录", "good_example": "探店早餐店油条豆腐脑花5块",
            "applicability": "标题要切中核心看点", "scope": "生活 日常 生活记录",
            "source_task_id": "task-old", "trigger_issue_codes": ["G3_META_DISCOURSE"],
        }, ensure_ascii=False), version),
    )
    seed.case_conn.commit()

    class OneVideo:
        def get(self, video_id):
            return self.video

    video = VideoInput(
        video_id="BV-new", title="早餐店油条豆腐脑花5块", author="作者", author_uid=None,
        url="", domain={"一级": "生活", "二级": "日常"}, format="生活记录",
        published_at="2020-01-01", duration=10, dimensions={}, description="", caption="",
        asr_segments=[], ocr_segments=[],
        evidence=[Evidence("e-title", "title", "早餐店油条豆腐脑花5块", quality="source")],
        asset_paths={}, provenance={},
    )
    dataset = OneVideo()
    dataset.video = video
    config = RunConfig(
        asset_root=str(tmp_path), output_dir=str(tmp_path / "run-new"), verification_enabled=False,
        extra={"require_human_on_low_quality_input": False,
               "business_policy": {"status": "approved", "version": "t", "rules": [{"rule_id": "P1", "text": "政策"}]}},
    )
    store = Storage(tmp_path / "run-new" / "agent.db", case_bank_path=bank)
    pipeline = Pipeline(dataset, store, MockAdapter(), config)
    task_id = pipeline.run("BV-new")
    state = json.loads(store.get_task(task_id)["state_json"])
    traces = state.get("memory_trace") or []
    assert traces, "运行必须留下检索痕迹，否则回流是否发生无法观测"
    assert any(t.get("status") == "ok" and t.get("used_case_ids") for t in traces), traces


def test_zero_overlap_case_is_not_injected(tmp_path):
    """类型标签相同但内容零重合的案例是噪声，不许进生成提示词。

    实测这类占旧版命中的 18%（如「美食」类视频互相撞上「餐厅」「美食」）。
    """
    bank = tmp_path / "cases.db"
    version = RunConfig().prompt_pack_version
    seed = Storage(tmp_path / "run-old" / "agent.db", case_bank_path=bank)
    seed.case_conn.execute(
        "INSERT INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,1,?)",
        ("auto-noise", json.dumps({
            "case_id": "auto-noise", "problem_type": "表达", "video_id": "BV-old",
            "bad_example": "鸡腿排煎好再下葱段酱汁", "good_example": "妈妈做菜的日常",
            "applicability": "只是过程中的一个步骤", "scope": "美食 烹饪教程 教程演示",
            "source_task_id": "task-old", "trigger_issue_codes": [],
        }, ensure_ascii=False), version),
    )
    seed.case_conn.commit()
    store = MemoryStore(seed.case_conn)
    # 问题类型对得上，但一个实词都不重合 → 必须为空
    assert store.search("日本美食蟹寿司", ["表达"], version, scope=["美食", "烹饪教程", "教程演示"]) == []
    # 有实词重合才放行
    assert store.search("鸡腿排煎好之后怎么下葱段", ["表达"], version, scope=["美食", "烹饪教程", "教程演示"])
