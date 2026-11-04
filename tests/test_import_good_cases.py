"""「精彩」→ 共享案例库 的导入测试。

这组测试盯的不是脚本能跑，而是四条「写进去也会被检索漏掉」的坑：
confirmed、rule_version、scope、payload 里的 video_id。少一个都等于白导。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("import_good_cases", ROOT / "scripts" / "import_good_cases.py")
good = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(good)


def _make_batch(tmp_path: Path, *, highlight: bool = True, decision: str = "approved",
                with_video: bool = True) -> Path:
    batch = tmp_path / "batch"
    batch.mkdir(exist_ok=True)
    video = ({"video_id": "BV1SxnZ6AEe3", "title": "体测跑步前热身很重要",
              "domain": {"一级": "健康运动", "二级": "运动技能"}, "format": "教程演示"}
             if with_video else {})
    with sqlite3.connect(batch / "agent.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, "
                     "config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?)",
                     ("t1", "BV1SxnZ6AEe3", "pending_human", "{}",
                      json.dumps({"video": video}, ensure_ascii=False), "", ""))
    with sqlite3.connect(batch / "human_feedback_v2.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS candidate_decisions_v2(batch_id TEXT NOT NULL, task_id TEXT NOT NULL, "
                     "candidate_id TEXT NOT NULL, title_hash TEXT NOT NULL, payload_json TEXT NOT NULL, "
                     "PRIMARY KEY(batch_id, task_id, candidate_id, title_hash))")
        payload = {"batch": "batch", "blind_id": "R01", "task_id": "t1", "video_id": "BV1SxnZ6AEe3",
                   "candidate_id": "c1", "original_headline": "博主示范体测跑前热身动作及次数",
                   "decision": decision, "reason": "动作数量有画面依据，归属清楚",
                   "final_headline": None, "highlight": highlight}
        conn.execute("INSERT OR REPLACE INTO candidate_decisions_v2 VALUES(?,?,?,?,?)",
                     ("batch", "t1", "c1", "h", json.dumps(payload, ensure_ascii=False)))
    return batch


def _empty_bank(tmp_path: Path) -> Path:
    bank = tmp_path / "cases.db"
    with sqlite3.connect(bank) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, confirmed INTEGER, "
                     "rule_version TEXT)")
    return bank


def _payloads(bank: Path) -> list[dict]:
    with sqlite3.connect(bank) as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM cases")]


def test_build_cases_keeps_the_fields_retrieval_actually_filters_on(tmp_path):
    """scope 必须由视频领域拼出来，缺了它检索时会被领域过滤整条跳过。"""
    batch = _make_batch(tmp_path)
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, skipped = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    assert skipped == []
    assert len(cases) == 1
    case = cases[0]["case"]
    assert case.scope == "健康运动 运动技能 教程演示"
    assert case.confirmed is True
    assert case.rule_version == "red-dot-v0.2-modular"
    assert case.good_example == "博主示范体测跑前热身动作及次数"


def test_written_case_carries_video_id_for_same_video_recall(tmp_path):
    """create_draft 不写 video_id；导入路径自己补上，否则同视频复用永远空。"""
    batch = _make_batch(tmp_path)
    bank = _empty_bank(tmp_path)
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, _ = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    written, removed = good.apply_writes(bank, cases, "batch")
    assert (written, removed) == (1, 0)
    payload = _payloads(bank)[0]
    assert payload["video_id"] == "BV1SxnZ6AEe3"
    assert payload["source"] == "human_highlight"
    assert payload["polarity"] == "positive"
    assert payload["confirmed"] is True


def test_verify_proves_the_case_is_actually_retrievable(tmp_path):
    """写进表≠能被取到；回查必须用视频自己的标题当 query。"""
    batch = _make_batch(tmp_path)
    bank = _empty_bank(tmp_path)
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, _ = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    good.apply_writes(bank, cases, "batch")
    assert good.verify(bank, cases, "red-dot-v0.2-modular") == 1


def test_clearing_highlight_removes_the_case_from_this_batch_only(tmp_path):
    batch = _make_batch(tmp_path)
    bank = _empty_bank(tmp_path)
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, _ = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    good.apply_writes(bank, cases, "batch")
    assert len(_payloads(bank)) == 1

    cleared = _make_batch(tmp_path, highlight=False)
    decisions = good.read_feedback(cleared / "human_feedback_v2.db")
    cases, _ = good.build_cases(decisions, cleared / "agent.db", "red-dot-v0.2-modular")
    assert cases == []
    written, removed = good.apply_writes(bank, cases, "batch")
    assert (written, removed) == (0, 1)
    assert _payloads(bank) == []


def test_non_approved_highlight_is_ignored_by_the_importer(tmp_path):
    """后端已把非「可用」的 highlight 压成 false；脚本再防一层，避免脏数据绕过。"""
    batch = _make_batch(tmp_path, highlight=True, decision="rejected")
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, skipped = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    assert cases == []
    assert skipped == []


def test_missing_video_domain_is_skipped_instead_of_written_unusable(tmp_path):
    batch = _make_batch(tmp_path, with_video=False)
    decisions = good.read_feedback(batch / "human_feedback_v2.db")
    cases, skipped = good.build_cases(decisions, batch / "agent.db", "red-dot-v0.2-modular")
    assert cases == []
    assert skipped and "检索不到" in skipped[0][1]
