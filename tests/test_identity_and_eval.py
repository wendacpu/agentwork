"""身份与评测口径回归：金标按任务隔离，严格率只数「可用」，推荐分母只数推荐。"""
from __future__ import annotations

import contextlib
import csv
import io
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_system import goldsheet

ROOT = Path(__file__).resolve().parents[1]
EVAL = runpy.run_path(str(ROOT / "scripts" / "eval.py"))


def _task(task_id, candidates, recommended, video_id="v"):
    return {
        "task_id": task_id,
        "status": "pending_human",
        "provider": "mock",
        "rule_version": "r",
        "created_at": "0",
        "state": {
            "candidates": [{"candidate_id": cid, "headline": cid} for cid in candidates],
            "route": {"action": "handoff", "recommended_candidate_id": recommended},
        },
    }


def _report(tmp_path, labels, tasks):
    gold_file = tmp_path / "gold.csv"
    gold_file.write_text("video_id,task_id,candidate_id,人工判定\n", encoding="utf-8-sig")
    main = EVAL["main"]
    out = tmp_path / "report.md"
    argv = ["eval", "--gold", str(gold_file), "--db", "memory", "--out", str(out)]
    with patch.dict(main.__globals__, {"load_gold": lambda p: labels, "index_tasks": lambda d: tasks}), \
            patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
        main()
    return out.read_text(encoding="utf-8")


def _label(candidate_id, verdict, task_id="t1", video_id="v"):
    return {"video_id": video_id, "task_id": task_id, "candidate_id": candidate_id, "人工判定": verdict}


def test_marginal_alone_is_not_a_strict_pass(tmp_path):
    text = _report(tmp_path, [_label("a", "勉强通过")], {"v": [_task("t1", ["a"], "a")]})
    assert "| 一次通过率（严格） | 0.0%" in text
    assert "| 一次通过率（含勉强通过） | 100.0%" in text


def test_recommendation_accuracy_divides_by_judged_recommendation_only(tmp_path):
    labels = [_label("a", "可用"), _label("b", "不可用")]
    text = _report(tmp_path, labels, {"v": [_task("t1", ["a", "b"], "a")]})
    assert "| 人工一致率 | 100.0% | 1 条可比" in text


def test_label_for_missing_candidate_is_rejected_not_counted(tmp_path):
    text = _report(tmp_path, [_label("zzz", "可用")], {"v": [_task("t1", ["a"], "a")]})
    assert "身份不一致" in text
    assert "| 一次通过率（严格） | —" in text or "有效判定：0 条候选" in text


def test_gold_upsert_keeps_distinct_tasks(tmp_path):
    target = tmp_path / "gold.csv"
    for task_id in ("t1", "t2"):
        goldsheet.record(
            video_id="v", task_id=task_id, candidate_id="a", headline=f"标题-{task_id}",
            verdict="不可用", final_headline=None, criteria_id="", fallback_node="",
            reason="x", reviewer="r", path=target,
        )
    rows = list(csv.DictReader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
    assert len(rows) == 2
    assert {r["task_id"] for r in rows} == {"t1", "t2"}


def test_gold_remove_is_task_scoped(tmp_path):
    target = tmp_path / "gold.csv"
    for task_id in ("t1", "t2"):
        goldsheet.record(
            video_id="v", task_id=task_id, candidate_id="a", headline="标题",
            verdict="不可用", final_headline=None, criteria_id="", fallback_node="",
            reason="x", reviewer="r", path=target,
        )
    goldsheet.remove(video_id="v", candidate_id="a", task_id="t1", path=target)
    rows = list(csv.DictReader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
    assert [r["task_id"] for r in rows] == ["t2"]


def _store(tmp_path):
    from agent_system.storage import Storage

    store = Storage(":memory:", case_bank_path=str(tmp_path / "cases.db"))
    task = store.create_task("v", {})
    store.set_state(task, "pending_human", {
        "video": {"video_id": "v"},
        "candidates": [{"candidate_id": "c1", "headline": "十个闹钟", "rendered_title": "【剧情演绎】十个闹钟"}],
        "candidate_decisions": [{"candidate_id": "c1", "status": "pass"}],
        "route": {"action": "handoff", "recommended_candidate_id": "c1", "eligible_candidate_ids": ["c1"]},
    })
    return store, task


def _state(store, task):
    import json as _json

    return _json.loads(store.get_task(task)["state_json"])


def test_main_rollback_does_not_leave_a_confirmed_case(tmp_path):
    store, task = _store(tmp_path)
    store.conn.execute(
        "CREATE TRIGGER fail_h01 BEFORE INSERT ON events WHEN NEW.node='H01' "
        "BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
    )
    store.conn.commit()
    with pytest.raises(Exception):
        store.record_human_decision(task, {"candidate_id": "c1", "decision": "approved"})
    assert store.case_conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_reopen_restores_original_system_recommendation(tmp_path):
    store, task = _store(tmp_path)
    store.record_human_decision(task, {"candidate_id": "c1", "decision": "rejected", "reason": "不对"})
    assert _state(store, task)["original_system_route"]["recommended_candidate_id"] == "c1"
    store.reopen_human_decision(task, "c1")
    assert _state(store, task)["route"]["recommended_candidate_id"] == "c1"


def test_marginal_keeps_rendered_prefix_and_edited_title(tmp_path):
    store, task = _store(tmp_path)
    store.record_human_decision(task, {
        "candidate_id": "c1", "decision": "marginal", "reason": "表达可再压缩",
        "final_headline": "【剧情演绎】改后的标题",
    })
    state = _state(store, task)
    assert state["final_headline"] == "【剧情演绎】改后的标题"
    assert store.get_task(task)["status"] == "approved_marginal"


def test_rejudging_to_rejected_clears_stale_approval(tmp_path):
    store, task = _store(tmp_path)
    store.record_human_decision(task, {"candidate_id": "c1", "decision": "approved"})
    store.record_human_decision(task, {"candidate_id": "c1", "decision": "rejected", "reason": "复核不合格"})
    state = _state(store, task)
    assert "final_headline" not in state
    assert (state.get("human_decision") or {}).get("candidate_id") != "c1"
