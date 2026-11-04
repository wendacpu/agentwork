from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from typing import Any

from .types import CaseRecord

_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 2-gram 切分会把虚词切出来（「一个高压锅」→ 一个/个高/高压/压锅），
# 这些 gram 在任意两条标题间都会撞上，制造大量假命中。只剔除确定不承载主题的。
_STOP = {
    "一个", "这个", "那个", "就是", "不是", "可以", "什么", "怎么",
    "这样", "那样", "还有", "没有", "这些", "那些", "时候", "现在",
}


def _terms(text: str) -> set[str]:
    """中文按 2-gram、英文按词切分。

    旧实现直接用 ``str.split()``，中文整句会变成一个 token，只有整句完全相同才命中，
    导致 case 检索在中文场景下几乎永远返回空。
    """
    normalized = str(text or "").lower()
    out: set[str] = set()
    for token in _SPLIT.split(normalized):
        if not token:
            continue
        out.add(token)
        if _CJK.search(token):
            out.update(token[i:i + 2] for i in range(len(token) - 1))
    return {item for item in out if item and item not in _STOP}


def _scope_set(value: Any) -> set[str]:
    """兼容三种存法：空格串（历史数据）、列表、空值。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(x) for x in value]
    else:
        items = [x for x in re.split(r"[\s,，/|]+", str(value or ""))]
    return {x.strip().lower() for x in items if x and x.strip()}


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_draft(self, case: CaseRecord) -> None:
        data = asdict(case)
        confirmed = bool(case.confirmed or case.case_status == "confirmed")
        data["confirmed"] = confirmed
        data["case_status"] = "confirmed" if confirmed else "draft"
        if isinstance(data.get("scope"), (list, tuple)):
            data["scope"] = " ".join(str(x) for x in data["scope"])
        self.conn.execute("INSERT OR REPLACE INTO cases(case_id, payload, confirmed, rule_version) VALUES(?,?,?,?)", (case.case_id, json.dumps(data, ensure_ascii=False), int(confirmed), case.rule_version))
        self.conn.commit()

    def confirm(self, case_id: str) -> bool:
        row = self.conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return False
        data = json.loads(row[0])
        data["confirmed"] = True
        data["case_status"] = "confirmed"
        self.conn.execute("UPDATE cases SET confirmed=1, payload=? WHERE case_id=?", (json.dumps(data, ensure_ascii=False), case_id))
        self.conn.commit()
        return True

    def get(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def search_for_video(self, video_id: str, rule_version: str, limit: int = 5) -> list[dict[str, Any]]:
        """同一条视频此前被判过的案例。

        只看 payload 里的 ``video_id``，不 JOIN tasks：案例库可能是跨实验共享的
        独立库，里面没有 tasks 表，JOIN 会直接抛 no such table。
        """
        if not video_id:
            return []
        rows = self.conn.execute(
            "SELECT payload FROM cases WHERE confirmed=1 AND (rule_version=? OR rule_version='*')",
            (rule_version,),
        ).fetchall()
        out = []
        for (payload,) in rows:
            data = json.loads(payload)
            if str(data.get("video_id") or "") == str(video_id):
                out.append(data)
        return out[:limit]

    def search(self, query: str, problem_types: list[str], rule_version: str, limit: int = 5,
               scope: list[str] | None = None, min_overlap: int = 1) -> list[dict[str, Any]]:
        """按「与当前视频有实际词重合」检索案例。

        ``min_overlap`` 是入选下限：默认 1，即至少一个非虚词重合。此前只要
        ``score > 0`` 就入选，于是「问题类型标签相同、内容零重合」的案例也会注入
        生成提示词——实测这类占命中的 18%，纯噪声。
        """
        rows = self.conn.execute(
            "SELECT payload FROM cases WHERE confirmed=1 AND (rule_version=? OR rule_version='*')",
            (rule_version,),
        ).fetchall()
        query_terms = _terms(query)
        problem_types = [str(x) for x in (problem_types or [])]
        requested_scope = _scope_set(scope)
        result: list[dict[str, Any]] = []
        for (payload,) in rows:
            data = json.loads(payload)
            if requested_scope:
                case_scope = _scope_set(data.get("scope"))
                if case_scope and not case_scope.intersection(requested_scope):
                    continue
            text = " ".join(str(data.get(k, "")) for k in ["problem_type", "applicability", "bad_example", "good_example", "trigger_issue_codes"])
            overlap = len(query_terms & _terms(text))
            if overlap < min_overlap:
                continue
            type_hit = 1 if str(data.get("problem_type", "")) in problem_types else 0
            # issue_code 精确命中是强信号：D04 的检索依据是问题类型，不是句子相似度
            code_hit = sum(1 for item in problem_types if item and item in str(data.get("trigger_issue_codes", "")))
            data["_score"] = overlap + type_hit * 3 + code_hit * 2
            data["_overlap"] = overlap
            result.append(data)
        return sorted(result, key=lambda x: (x["_overlap"], x["_score"]), reverse=True)[:limit]
