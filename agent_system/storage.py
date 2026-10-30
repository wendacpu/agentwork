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


class Storage:
    def __init__(self, path: str | Path) -> None:
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
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS human_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, payload TEXT, confirmed INTEGER, rule_version TEXT);
        CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);
        """)
        self.conn.commit()

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
        for c in candidates: self.conn.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)", (c["candidate_id"], task_id, json.dumps(c, ensure_ascii=False)))
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
        row = self.conn.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='model_reply' ORDER BY id DESC",
            (task_id,),
        )
        for item in row:
            payload = json.loads(item[0])
            if payload.get("request_hash") == request_hash and isinstance(payload.get("data"), dict):
                return payload["data"]
        return None

    def record_human_decision(self, task_id: str, decision: dict[str, Any]) -> None:
        row = self.get_task(task_id)
        state = json.loads(row["state_json"] or "{}")
        # pending_human 是正常待审；stopped 里的候选同样真实（重写耗尽、业务停止），
        # 不能因为链路停了就把已产出的候选锁死——那会让可判样本凭空空掉一半。
        if row["status"] not in {"pending_human", "stopped"}:
            raise ValueError(f"human decisions require pending_human/stopped status (current status: {row['status']})")
        candidates = state.get("candidates", [])
        candidate = next((x for x in candidates if x.get("candidate_id") == decision.get("candidate_id")), None)
        if candidate is None:
            raise ValueError("candidate does not belong to task")
        # 三档：approved 直接可用 / marginal 勉强通过（有瑕疵但可放行）/ rejected 驳回
        if decision.get("decision") not in {"approved", "marginal", "rejected"}:
            raise ValueError("decision must be approved, marginal or rejected")
        # 历史数据里出现过字符串 "None"（前端 null 被 str 之后），这里一并当空缺处理
        passing = decision["decision"] in {"approved", "marginal"}
        if passing and str(decision.get("final_headline") or "").strip() in {"", "None", "null"}:
            decision["final_headline"] = None
        if passing and not decision.get("final_headline"):
            decision["final_headline"] = candidate.get("headline")
        # 勉强通过必须留下瑕疵说明，否则灰度档退化成"都能过"的橡皮章
        if decision["decision"] == "marginal" and not str(decision.get("reason") or "").strip():
            raise ValueError("marginal decision requires a stated defect")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT INTO human_decisions(task_id,payload_json) VALUES(?,?)", (task_id, json.dumps(decision, ensure_ascii=False)))
            state.setdefault("human_decisions", []).append(decision)
            if decision["decision"] in {"approved", "marginal"}:
                state["human_decision"] = decision
                state["final_headline"] = str(decision.get("final_headline") or candidate.get("headline"))
                status = "approved" if decision["decision"] == "approved" else "approved_marginal"
            else:
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
            case_id = self._auto_case(task_id, state, decision, candidate)
            self.conn.execute("INSERT INTO events(task_id,node,event_type,payload_json,created_at) VALUES(?,?,?,?,?)", (task_id, "H01", "human_decision", json.dumps({"decision": decision, "status": status, "auto_case_id": case_id}, ensure_ascii=False), timestamp))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _auto_case(self, task_id: str, state: dict[str, Any], decision: dict[str, Any], candidate: dict[str, Any]) -> str | None:
        """人工判定 → 案例草稿（D04 的语料来源）。

        - 驳回且给了改写建议：落一条 draft case，等人工确认
        - 采用且终稿与原标题不同：落一条已确认 case（这才是"人工认可的表达方式"）
        没有可迁移的表达差异时不落库，避免噪声。
        """
        from .types import CaseRecord

        original = str(candidate.get("headline") or "")
        final = str(decision.get("final_headline") or "")
        suggestion = str(decision.get("rewrite_suggestion") or "")
        status = str(decision.get("decision") or "")
        polarity = "positive"
        if status in {"approved", "marginal"}:
            if final and final != original:
                good, confirmed, case_type = final, True, "expression"
            elif status == "marginal":
                # 能用但有问题、且没改标题：沉淀成反例，让下次生成知道"这类表达勉强通过"
                good, confirmed, case_type, polarity = "", True, "expression", "negative"
            else:
                return None
        elif suggestion:
            good, confirmed, case_type = suggestion, False, "expression"
        elif status == "rejected":
            # 驳回且没给改写建议：至少留下"这么写不行"的反例
            good, confirmed, case_type, polarity = "", True, "expression", "negative"
        else:
            return None
        rule_version = str((state.get("rules") or {}).get("version") or "temporary-v1")
        video = state.get("video") or {}
        domain = video.get("domain") or {}
        scope = " ".join(str(x) for x in [domain.get("一级"), domain.get("二级"), video.get("format")] if x)
        issues = next((r.get("issues", []) for r in (state.get("reviews") or []) if r.get("candidate_id") == candidate.get("candidate_id")), [])
        problem_type = str(decision.get("reason_type") or (issues[0].get("issue_code") if issues else "expression"))
        case = CaseRecord(
            case_id=f"auto-{task_id}-{candidate.get('candidate_id')}", case_type=case_type,
            problem_type=problem_type, bad_example=original, good_example=good,
            applicability=str(decision.get("reason") or decision.get("upstream_field") or ""),
            source_task_id=task_id, rule_version=rule_version, confirmed=confirmed,
            compatible_rule_versions=[rule_version], case_status="confirmed" if confirmed else "draft",
            scope=scope, source_evidence=list(candidate.get("evidence_ids") or []),
        )
        payload = asdict(case)
        payload["trigger_issue_codes"] = [str(i.get("issue_code")) for i in issues if i.get("issue_code")]
        payload["polarity"] = polarity
        payload["confirmed"] = confirmed
        payload["case_status"] = "confirmed" if confirmed else "draft"
        self.conn.execute("INSERT OR REPLACE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,?,?)", (case.case_id, json.dumps(payload, ensure_ascii=False), int(confirmed), rule_version))
        return case.case_id

