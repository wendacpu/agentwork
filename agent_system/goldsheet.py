"""把评审台的判定写进 templates/gold.csv。

评审台原本只把判定写进 SQLite（human_decisions / labels），而 eval.py 读的是
gold.csv——两边不通，于是"在评审台上判了几十条"和"能出四个数"之间断了一环。

这里做的只有一件事：按 (video_id, candidate_id) 覆盖写一行，并保留表头原文。
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "templates" / "gold.csv"


def _col(row: dict[str, str], prefix: str) -> str:
    for key in row:
        if key and str(key).startswith(prefix):
            return key
    return ""


def read_criteria(path: Path | None = None) -> list[dict[str, str]]:
    """读判据表，给评审台当"系统性文档"用。"""
    source = path or (ROOT / "templates" / "criteria.csv")
    if not source.exists():
        return []
    out = []
    with source.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out.append({
                "id": (row.get("判据ID") or "").strip(),
                "name": (row.get("判据名") or "").strip(),
                "condition": (row.get("具体条件（要可判定，能两人判出同一结果）") or row.get("具体条件") or "").strip(),
                "gate": (row.get("归属 Gate") or "").strip(),
                "issue_code": (row.get("issue_code") or "").strip(),
                "verdict": (row.get("判定结果") or "").strip(),
                "judge": (row.get("判定者") or "").strip(),
            })
    return [c for c in out if c["id"]]


def upsert(entry: dict[str, str], path: Path | None = None) -> Path:
    """写入或覆盖一行；列名按前缀匹配，容忍表头里的括号说明。"""
    target = path or GOLD
    target.parent.mkdir(parents=True, exist_ok=True)

    header: list[str] = []
    rows: list[dict[str, str]] = []
    if target.exists():
        with target.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            rows = list(reader)
    if not header:
        header = ["video_id", "task_id", "candidate_id", "标题", "系统路由", "系统推荐",
                  "人工判定", "人工最终标题", "驳回原因", "命中的判据ID", "建议回退节点", "一句话理由", "判定人", "判定日期"]
    else:
        # 旧表头没有的新字段（如后来才加的「驳回原因」）就地补列，不让历史行报错
        for key in entry:
            if key and key not in header and not any(str(h).startswith(key) for h in header):
                header.append(key)

    def value_of(key: str) -> str:
        """gold.csv 表头可能带括号说明，按前缀匹配字段名。"""
        if key in entry:
            return entry[key]
        for entry_key, entry_value in entry.items():
            if key.startswith(entry_key):
                return entry_value
        return ""

    key_video = next((k for k in header if k.startswith("video_id")), "video_id")
    key_cand = next((k for k in header if k.startswith("candidate_id")), "candidate_id")
    key_task = next((k for k in header if k.startswith("task_id")), "task_id")
    replaced = False
    for existing in rows:
        # 身份必须是「任务 + 候选」：只按 video+candidate 覆盖会把另一个任务（含失败的
        # 实验臂）的标签写成当前任务的结果，历史对照因此被污染过。
        if (existing.get(key_video) == entry.get("video_id")
                and existing.get(key_task, "") == entry.get("task_id", "")
                and existing.get(key_cand) == entry.get("candidate_id")):
            for k in header:
                existing[k] = value_of(k)
            replaced = True
            break
    if not replaced:
        rows.append({k: value_of(k) for k in header})

    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})
    return target


def remove(*, video_id: str, candidate_id: str, task_id: str = "", path: Path | None = None) -> Path:
    """撤回人工判定时移除对应金标准行，避免旧标签继续参与评估。"""
    target = path or GOLD
    if not target.exists():
        return target
    with target.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    key_video = next((k for k in header if k.startswith("video_id")), "video_id")
    key_cand = next((k for k in header if k.startswith("candidate_id")), "candidate_id")
    key_task = next((k for k in header if k.startswith("task_id")), "task_id")

    def matched(row: dict[str, str]) -> bool:
        if row.get(key_video) != video_id or row.get(key_cand) != candidate_id:
            return False
        # 传了 task_id 就必须同任务才撤回；没传时只撤回任务列为空的历史行，
        # 不把别的实验臂的标签一起抹掉。
        return row.get(key_task, "") == (task_id or "")

    rows = [r for r in rows if not matched(r)]
    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return target


def record(*, video_id: str, task_id: str, candidate_id: str, headline: str, verdict: str,
           final_headline: str | None, criteria_id: str, fallback_node: str, reason: str,
           reviewer: str, route: str = "", recommended: str = "", rejection_reason: str = "",
           day: str | None = None, path: Path | None = None) -> Path:
    return upsert({
        "video_id": video_id,
        "task_id": task_id,
        "candidate_id": candidate_id,
        "标题": headline,
        "系统路由": route,
        "系统推荐": recommended,
        "人工判定": verdict,
        "人工最终标题": final_headline or "",
        "驳回原因": rejection_reason or "",
        "命中的判据ID": criteria_id or "",
        "建议回退节点": fallback_node or "",
        "一句话理由": reason,
        "判定人": reviewer,
        "判定日期": day or date.today().isoformat(),
    }, path)
