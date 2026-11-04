from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 调试台的英文 reason_type / issue_code → 检索用的中文问题类型。
# 不统一口径时，首轮检索的 problem_types（["表达", "事实与信源"]）永远对不上案例的 problem_type。
_REASON_TO_PROBLEM_TYPE = {
    "fact": "事实与信源",
    "context": "事实与信源",
    "point": "表达",
    "grammar": "表达",
    "unclear": "表达",
    "other": "表达",
}


def _canonical_problem_type(raw_reason: str, issue_codes: list[str]) -> str:
    raw = (raw_reason or "").strip()
    if raw in _REASON_TO_PROBLEM_TYPE:
        return _REASON_TO_PROBLEM_TYPE[raw]
    for code in [raw, *issue_codes]:
        text = str(code or "")
        if not text:
            continue
        low = text.lower()
        if text.startswith("expression") or text.startswith("G3") or "expression" in low:
            return "表达"
        if text.startswith("G1") or "source" in low or "fact" in low:
            return "事实与信源"
        if text.startswith("G2") or "policy" in low:
            return "政策"
    return raw or "root_cause_unresolved"


class Storage:
    def __init__(self, path: str | Path, case_bank_path: str | Path | None = None,
                 cache_bank_path: str | Path | None = None) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, node TEXT, event_type TEXT, payload_json TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS candidates(candidate_id TEXT PRIMARY KEY, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS candidates_by_task(task_id TEXT, candidate_id TEXT, payload_json TEXT, PRIMARY KEY(task_id, candidate_id));
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS human_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, confirmed INTEGER, rule_version TEXT);
        CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, case_id TEXT, video_id TEXT, layer TEXT, problem_type TEXT, raw_reason_type TEXT, reason TEXT, check_hint TEXT, created_at TEXT);
        """)
        from .repairs import RepairStore
        RepairStore(self.conn)
        self.conn.commit()
        # 案例库：默认与任务库同库（向后兼容）；配 case_bank 时用独立库，让案例跨实验目录共享。
        if case_bank_path and str(Path(case_bank_path).resolve()) != str(Path(path).resolve()):
            Path(case_bank_path).parent.mkdir(parents=True, exist_ok=True)
            self.case_conn = sqlite3.connect(case_bank_path, timeout=30)
            self.case_conn.row_factory = sqlite3.Row
            self.case_conn.execute("PRAGMA journal_mode=WAL")
            self.case_conn.execute("PRAGMA busy_timeout=30000")
            self.case_conn.execute("CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, confirmed INTEGER, rule_version TEXT)")
            self.case_conn.execute("CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, case_id TEXT, video_id TEXT, layer TEXT, problem_type TEXT, raw_reason_type TEXT, reason TEXT, check_hint TEXT, created_at TEXT)")
            self.case_conn.commit()
        else:
            self.case_conn = self.conn
        # 模型调用共享缓存：默认不传 = 关闭（旧行为）。传路径时用独立库，
        # 不往任务库里塞可能上 GB 的模型原始输出。
        self.cache_conn = self._open_cache_bank(cache_bank_path)

    def _open_cache_bank(self, cache_bank_path: str | Path | None) -> sqlite3.Connection | None:
        if not cache_bank_path:
            return None
        target = str(Path(cache_bank_path).resolve())
        if target == str(Path(self.path).resolve()):
            conn = self.conn
        else:
            Path(cache_bank_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_cache("
            "request_hash TEXT PRIMARY KEY, task TEXT, model TEXT, "
            "data_json TEXT NOT NULL, usage_json TEXT, created_at TEXT)"
        )
        conn.commit()
        return conn

    def create_task(self, video_id: str, config: dict[str, Any]) -> str:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        t = now()
        self.conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (task_id, video_id, "prepared", json.dumps(config, ensure_ascii=False), "{}", t, t))
        self.conn.commit()
        return task_id

    def set_state(self, task_id: str, status: str, state: dict[str, Any]) -> None:
        self.conn.execute("UPDATE tasks SET status=?, state_json=?, updated_at=? WHERE task_id=?", (status, json.dumps(state, ensure_ascii=False), now(), task_id))
        self.conn.commit()

    def checkpoint(self, task_id: str, status: str, state: dict[str, Any], node: str, stage: str, summary: dict[str, Any]) -> None:
        timestamp = now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE tasks SET status=?,state_json=?,updated_at=? WHERE task_id=?", (status, json.dumps(state, ensure_ascii=False), timestamp, task_id))
            self.conn.execute("INSERT INTO events(task_id,node,event_type,payload_json,created_at) VALUES(?,?,?,?,?)", (task_id, node, "checkpoint_saved", json.dumps({"stage": stage, **summary}, ensure_ascii=False), timestamp))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_task(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None: raise KeyError(task_id)
        return row

    def event(self, task_id: str, node: str, event_type: str, payload: Any) -> None:
        self.conn.execute("INSERT INTO events(task_id,node,event_type,payload_json,created_at) VALUES(?,?,?,?,?)", (task_id, node, event_type, json.dumps(payload, ensure_ascii=False), now()))
        self.conn.commit()

    def save_candidates(self, task_id: str, candidates: list[dict[str, Any]]) -> None:
        for c in candidates:
            payload = json.dumps(c, ensure_ascii=False)
            # 旧表只有 candidate_id 主键，跨任务同名候选会互相覆盖；新表按任务隔离，
            # 旧表保留只为兼容历史读取，不再作为身份来源。
            self.conn.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)", (c["candidate_id"], task_id, payload))
            self.conn.execute("INSERT OR REPLACE INTO candidates_by_task VALUES(?,?,?)", (task_id, c["candidate_id"], payload))
        self.conn.commit()

    def save_reviews(self, task_id: str, reviews: Any) -> None:
        self.conn.execute("INSERT INTO reviews(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(reviews, ensure_ascii=False))); self.conn.commit()

    def save_human(self, task_id: str, decision: Any) -> None:
        self.conn.execute("INSERT INTO human_decisions(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(decision, ensure_ascii=False))); self.conn.commit()

    def add_label(self, task_id: str, payload: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO labels(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(payload, ensure_ascii=False))); self.conn.commit()

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in self.conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT task_id,video_id,status,created_at,updated_at,state_json FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        return [dict(row) for row in rows]

    def cached_model_reply(self, task_id: str, request_hash: str) -> dict[str, Any] | None:
        """任务内缓存（历史行为）：从本任务的事件流里找同 hash 的成功回复。

        它是 O(事件数) 的线性扫描，只在「同任务重试 / resume」时命中，
        跨任务、跨批次永远不命中——这正是共享缓存要补的那一块。
        """
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='model_reply' ORDER BY id DESC",
            (task_id,),
        )
        for item in row:
            payload = json.loads(item[0])
            if payload.get("request_hash") == request_hash and isinstance(payload.get("data"), dict):
                return payload["data"]
        return None

    # ---- 共享模型缓存（跨任务 / 跨批次） -------------------------------------
    # 键是请求内容哈希（system + user + schema + model），由 pipeline._call 计算，
    # 不含 task_id。因此「同一批视频换一个 output_dir 再跑」不再从零付费。
    # 隔离由 cache_bank_isolated 控制：置 true 时不建这张表，行为退回旧版。
    def cached_shared_reply(self, request_hash: str) -> dict[str, Any] | None:
        if self.cache_conn is None:
            return None
        row = self.cache_conn.execute(
            "SELECT data_json FROM model_cache WHERE request_hash=?", (request_hash,)
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def remember_shared_reply(self, request_hash: str, task: str, model: str,
                              data: dict[str, Any], usage: dict[str, Any] | None) -> None:
        if self.cache_conn is None or not isinstance(data, dict):
            return
        try:
            self.cache_conn.execute(
                "INSERT OR REPLACE INTO model_cache(request_hash, task, model, data_json, usage_json, created_at) VALUES(?,?,?,?,?,?)",
                (request_hash, task, model, json.dumps(data, ensure_ascii=False),
                 json.dumps(usage or {}, ensure_ascii=False), now()),
            )
            self.cache_conn.commit()
        except sqlite3.Error:
            # 缓存是加速层，写失败不能让整条任务失败。
            pass

    def reopen_human_decision(self, task_id: str, candidate_id: str) -> None:
        row = self.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        candidates = state.get("candidates", [])
        if not any(x.get("candidate_id") == candidate_id for x in candidates):
            raise ValueError("candidate does not belong to task")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            prior = self.conn.execute("SELECT id FROM human_decisions WHERE task_id=? AND json_extract(payload_json,'$.candidate_id')=? ORDER BY id DESC LIMIT 1", (task_id, candidate_id)).fetchone()
            if prior:
                self.conn.execute("DELETE FROM human_decisions WHERE id=?", (prior[0],))
            self.conn.execute("DELETE FROM labels WHERE task_id=? AND json_extract(payload_json,'$.candidate_id')=?", (task_id, candidate_id))
            self.conn.execute("DELETE FROM events WHERE task_id=? AND node='H01' AND event_type='human_decision' AND json_extract(payload_json,'$.decision.candidate_id')=?", (task_id, candidate_id))
            if self.case_conn is self.conn:
                self.case_conn.execute("DELETE FROM cases WHERE case_id=?", (f"auto-{task_id}-{candidate_id}",))
                # 撤回后旧反馈必须一起清：否则上一次的驳回理由会继续挂在案例库里，
                # 检索时把"已撤回的判定"当成有效经验。
                self.case_conn.execute("DELETE FROM feedback WHERE case_id=?", (f"auto-{task_id}-{candidate_id}",))
            state["human_decisions"] = [x for x in state.get("human_decisions", []) if x.get("candidate_id") != candidate_id]
            if state.get("human_decision", {}).get("candidate_id") == candidate_id:
                state.pop("human_decision", None)
                state.pop("final_headline", None)
            state["human_rejected_candidate_ids"] = [x for x in state.get("human_rejected_candidate_ids", []) if x != candidate_id]
            state["route"] = state.get("route") or {}
            original = state.get("original_system_route") or {}
            state["route"]["action"] = "handoff"
            state["route"]["reason"] = "human_decision_reopened"
            state["route"]["eligible_candidate_ids"] = [x.get("candidate_id") for x in candidates]
            # 撤回不能顺手抹掉系统原推荐：那是评测的分母来源，必须由不可变快照恢复。
            if original.get("recommended_candidate_id"):
                state["route"]["recommended_candidate_id"] = original["recommended_candidate_id"]
            else:
                state["route"].pop("recommended_candidate_id", None)
            approvals = [x for x in state["human_decisions"] if x.get("decision") in {"approved", "marginal"}]
            if approvals:
                state["human_decision"] = approvals[-1]
                state["final_headline"] = approvals[-1].get("final_headline")
                status = "approved" if approvals[-1].get("decision") == "approved" else "approved_marginal"
            else:
                state.pop("human_decision", None)
                state.pop("final_headline", None)
                status = "pending_human"
            self.conn.execute("UPDATE tasks SET status=?,state_json=?,updated_at=? WHERE task_id=?", (status, json.dumps(state, ensure_ascii=False), now(), task_id))
            from .repairs import RepairStore
            candidate = next(x for x in candidates if x.get("candidate_id") == candidate_id)
            RepairStore(self.conn).enqueue(task_id, state, {"candidate_id": candidate_id}, candidate, withdrawn=True)
            self.conn.commit()
            if self.case_conn is not self.conn:
                self.case_conn.execute("DELETE FROM cases WHERE case_id=?", (f"auto-{task_id}-{candidate_id}",))
                self.case_conn.execute("DELETE FROM feedback WHERE case_id=?", (f"auto-{task_id}-{candidate_id}",))
                self.case_conn.commit()
        except Exception:
            if self.case_conn is not self.conn:
                self.case_conn.rollback()
            self.conn.rollback()
            raise

        self.process_repairs()

    def record_human_decision(self, task_id: str, decision: dict[str, Any]) -> None:
        row = self.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        if decision.get("decision") == "reopen":
            self.reopen_human_decision(task_id, str(decision.get("candidate_id") or ""))
            return
        # pending_human 是正常待审；stopped 里的候选同样真实（重写耗尽、业务停止），
        # 不能因为链路停了就把已产出的候选锁死——那会让可判样本凭空空掉一半。
        if row["status"] not in {"pending_human", "stopped", "approved", "approved_marginal"}:
            raise ValueError(f"human decisions require a reviewable task (current status: {row['status']})")
        candidates = state.get("candidates", [])
        candidate = next((x for x in candidates if x.get("candidate_id") == decision.get("candidate_id")), None)
        if candidate is None:
            raise ValueError("candidate does not belong to task")
        # 三档：approved 可用 / marginal 勉强通过 / rejected 不可用
        if decision.get("decision") not in {"approved", "marginal", "rejected"}:
            raise ValueError("decision must be approved, marginal or rejected")
        # 历史数据里出现过字符串 "None"（前端 null 被 str 之后），这里一并当空缺处理
        passing = decision["decision"] in {"approved", "marginal"}
        if passing and str(decision.get("final_headline") or "").strip() in {"", "None", "null"}:
            decision["final_headline"] = None
        if passing and not decision.get("final_headline"):
            # 存渲染后的完整串（含平台前缀），否则人工通过的标题和系统记录的标题不是同一个东西
            decision["final_headline"] = candidate.get("rendered_title") or candidate.get("headline")
        # 勉强通过必须留下瑕疵说明，否则灰度档退化成"都能过"的橡皮章
        if decision["decision"] in {"marginal", "rejected"} and not str(decision.get("reason") or "").strip():
            raise ValueError("marginal decision requires a stated defect")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # 首次人工判定时冻结系统原推荐；之后人工驳回/改判只影响可审范围，不改写这个快照。
            state.setdefault("original_system_route", json.loads(json.dumps(state.get("route") or {})))
            # 同一候选可撤回并重新判定；更新既有人工记录，避免历史判定继续影响盲评统计。
            prior = self.conn.execute("SELECT id FROM human_decisions WHERE task_id=? AND json_extract(payload_json,'$.candidate_id')=? ORDER BY id DESC LIMIT 1", (task_id, decision["candidate_id"])).fetchone()
            if prior:
                self.conn.execute("UPDATE human_decisions SET payload_json=? WHERE id=?", (json.dumps(decision, ensure_ascii=False), prior[0]))
                self.case_conn.execute("DELETE FROM cases WHERE case_id=?", (f"auto-{task_id}-{decision['candidate_id']}",))
                self.conn.execute("DELETE FROM labels WHERE task_id=? AND json_extract(payload_json,'$.candidate_id')=?", (task_id, decision["candidate_id"]))
                self.conn.execute("DELETE FROM events WHERE task_id=? AND node='H01' AND event_type='human_decision' AND json_extract(payload_json,'$.decision.candidate_id')=?", (task_id, decision["candidate_id"]))
            else:
                self.conn.execute("INSERT INTO human_decisions(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(decision, ensure_ascii=False)))
            state["human_decisions"] = [x for x in state.get("human_decisions", []) if x.get("candidate_id") != decision["candidate_id"]]
            state.setdefault("human_decisions", []).append(decision)
            if decision["decision"] in {"approved", "marginal"}:
                state["human_decision"] = decision
                state["final_headline"] = str(decision.get("final_headline") or candidate.get("rendered_title") or candidate.get("headline"))
                status = "approved" if decision["decision"] == "approved" else "approved_marginal"
            else:
                # 改判为驳回时，必须清掉上一条残留的「已通过」结果，否则任务会一直
                # 带着旧终稿和 approved 状态，后续报告与金标都会读到过期结论。
                if (state.get("human_decision") or {}).get("candidate_id") == decision["candidate_id"]:
                    state.pop("human_decision", None)
                    state.pop("final_headline", None)
                rejected = set(state.get("human_rejected_candidate_ids", []))
                rejected.add(decision["candidate_id"])
                state["human_rejected_candidate_ids"] = sorted(rejected)
                route = state.get("route") or {}
                reviewable = [x.get("candidate_id") for x in state.get("candidate_decisions", []) if x.get("status") != "reject"]
                if not reviewable:
                    reviewable = [x.get("candidate_id") for x in candidates]
                remaining = [cid for cid in reviewable if cid and cid not in rejected]
                if remaining:
                    # 只缩小可选范围，不改写 recommended：推荐是 Q01 的系统判断，
                    # 一旦被人工驳回结果反过来改写，"人工一致率"就变成自己证明自己。
                    route.update({"eligible_candidate_ids": remaining, "reason": "human_rejected_candidate; remaining_candidates_available_for_human_review"})
                    status = "pending_human"
                else:
                    route.update({"action": "stop", "eligible_candidate_ids": [], "recommended_candidate_id": None, "reason": "ALL_REVIEWED_CANDIDATES_REJECTED_BY_HUMAN"})
                    status = "stopped"
                state["route"] = route
            timestamp = now()
            self.conn.execute("UPDATE tasks SET status=?,state_json=?,updated_at=? WHERE task_id=?", (status, json.dumps(state, ensure_ascii=False), timestamp, task_id))
            label = {"kind": "human_title_decision", **decision, "original_headline": candidate.get("headline"), "rule_version": state.get("rules", {}).get("version"), "prompt_hashes": state.get("prompt_hashes", {})}
            self.conn.execute("INSERT INTO labels(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(label, ensure_ascii=False)))
            from .repairs import RepairStore
            RepairStore(self.conn).enqueue(task_id, state, decision, candidate)
            case_id = self._auto_case(task_id, state, decision, candidate)
            self.conn.execute("INSERT INTO events(task_id,node,event_type,payload_json,created_at) VALUES(?,?,?,?,?)", (task_id, "H01", "human_decision", json.dumps({"decision": decision, "status": status, "auto_case_id": case_id}, ensure_ascii=False), timestamp))
            self.conn.commit()
            # 独立案例库只能在主库提交成功后再提交：否则主库回滚了，案例却已经入册，
            # 检索会把一条并不存在的通过/驳回当成有效经验。
            if self.case_conn is not self.conn:
                self.case_conn.commit()
        except Exception:
            if self.case_conn is not self.conn:
                self.case_conn.rollback()
            self.conn.rollback()
            raise
        self.process_repairs()

    def process_repairs(self):
        from .repairs import RepairStore
        result = RepairStore(self.conn).drain()
        self.conn.commit()
        return result

    def record_feedback(self, task_id: str, case_id: str, video_id: str, layer: str, problem_type: str, raw_reason_type: str, reason: str, check_hint: str) -> None:
        self.case_conn.execute(
            "INSERT INTO feedback(task_id, case_id, video_id, layer, problem_type, raw_reason_type, reason, check_hint, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, case_id, video_id, layer, problem_type, raw_reason_type, reason, check_hint, now()),
        )
        self.case_conn.commit()

    def _auto_case(self, task_id: str, state: dict[str, Any], decision: dict[str, Any], candidate: dict[str, Any]) -> str | None:
        """人工判定 → 案例草稿（D04 的语料来源）。仅沉淀标题判定与瑕疵，不要求人工提供改写。"""
        from .types import CaseRecord

        original = str(candidate.get("headline") or "")
        final = str(decision.get("final_headline") or "")
        status = str(decision.get("decision") or "")
        polarity = "positive"
        confirmed = True
        bad_example, good_example = "", ""
        if status == "approved":
            # 判可用 = 原样通过，本身就是一条正例：good=原标题
            # （万一走了「编辑后通过」，把原标题也记成反例，避免丢掉修正信息）
            if final and final != original:
                bad_example, good_example = original, final
            else:
                good_example = original
        elif status == "marginal":
            # 勉强通过：原标题是反例、改后是正例（正常流程都会改标题）
            if final and final != original:
                bad_example, good_example = original, final
            else:
                bad_example, polarity = original, "negative"
        elif status == "rejected":
            # 驳回 = 反例
            bad_example, polarity = original, "negative"
        else:
            return None
        rule_version = str((state.get("rules") or {}).get("version") or "temporary-v1")
        video = state.get("video") or {}
        domain = video.get("domain") or {}
        scope = " ".join(str(x) for x in [domain.get("一级"), domain.get("二级"), video.get("format")] if x)
        issues = next((r.get("issues", []) for r in (state.get("reviews") or []) if r.get("candidate_id") == candidate.get("candidate_id")), [])
        raw_reason = str(decision.get("reason_type") or "")
        issue_codes = [str(i.get("issue_code")) for i in issues if i.get("issue_code")]
        problem_type = _canonical_problem_type(raw_reason, issue_codes)
        case = CaseRecord(
            case_id=f"auto-{task_id}-{candidate.get('candidate_id')}", case_type="expression",
            problem_type=problem_type, bad_example=bad_example, good_example=good_example,
            applicability=str(decision.get("reason") or decision.get("upstream_field") or ""),
            source_task_id=task_id, rule_version=rule_version, confirmed=confirmed,
            compatible_rule_versions=[rule_version], case_status="confirmed" if confirmed else "draft",
            scope=scope, source_evidence=list(candidate.get("evidence_ids") or []),
        )
        payload = asdict(case)
        payload["raw_reason_type"] = raw_reason
        # video_id 落进 payload：共享案例库里没有 tasks 表，
        # 同视频检索若靠 JOIN 会直接失败，历史判定也就无法复用。
        payload["video_id"] = str(video.get("video_id") or "")
        payload["trigger_issue_codes"] = [str(i.get("issue_code")) for i in issues if i.get("issue_code")]
        payload["polarity"] = polarity
        payload["confirmed"] = confirmed
        payload["case_status"] = "confirmed" if confirmed else "draft"
        self.case_conn.execute("INSERT OR REPLACE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,?,?)", (case.case_id, json.dumps(payload, ensure_ascii=False), int(confirmed), rule_version))
        # 自动回流：把这次判定归到一个设计层，写进 feedback 表（供后续按层修）
        from .feedback import classify
        video_id = str((state.get("video") or {}).get("video_id") or "")
        layer, hint = classify(raw_reason, problem_type, str(decision.get("reason") or ""))
        # 同库写入加入人工判定事务，不调用会提前 commit 的公共方法。
        self.case_conn.execute("INSERT INTO feedback(task_id,case_id,video_id,layer,problem_type,raw_reason_type,reason,check_hint,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, case.case_id, video_id, layer, problem_type, raw_reason, str(decision.get("reason") or ""), hint, now()))
        # 不在此处提交：由 record_human_decision 在主库提交成功后统一提交，
        # 保证"主库回滚但案例已入册"不会发生。
        return case.case_id

