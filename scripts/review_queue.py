#!/usr/bin/env python3
"""待审队列：把散在 runtime/*/agent.db 里的候选摊成一张可判的清单。

为什么需要它：候选一直存在（58 条任务、38 条有候选），但分散在 34 个库里，
调试台一次只能开一条。于是"没有 case 可审"其实是"没有入口"。

用法：
    python scripts/review_queue.py                     # 写 runtime/review_queue.md
    python scripts/review_queue.py --only-real         # 只列真实模型产出（排除 mock）
    python scripts/review_queue.py --emit-gold out.csv # 同时吐一份 gold.csv 骨架

判定结果写进 templates/gold.csv（人工判定列），再跑 scripts/eval.py 出四个数。
只依赖标准库。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_PROVIDERS = {"deepseek", "qwen", "kimi"}
CURRENT_RULE = "red-dot-v0.2-modular"


def dedupe(tasks: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """每个视频只留一条：真实模型 > 当前规则版本 > 最新。

    同一视频被反复跑过十几次时，全列出来等于让人重复判同一件事。
    """
    best: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for t in tasks:
        vid = t["video_id"]
        counts[vid] = counts.get(vid, 0) + 1
        key = (t["provider"] in REAL_PROVIDERS, t["rule_version"] == CURRENT_RULE, str(t["created_at"]))
        if vid not in best or key > best[vid]["_key"]:
            best[vid] = {**t, "_key": key}
    ordered = sorted(best.values(), key=lambda t: (t["provider"] in REAL_PROVIDERS, str(t["created_at"])), reverse=True)
    return ordered, counts


def col(row: dict[str, str], prefix: str) -> str:
    for key, value in row.items():
        if key and str(key).startswith(prefix):
            return (value or "").strip()
    return ""


def load_judged(path: Path) -> set[tuple[str, str]]:
    """已判过的 (video_id, candidate_id)，避免重复占用评审时间。"""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {
            ((r.get("video_id") or "").strip(), (r.get("candidate_id") or "").strip())
            for r in csv.DictReader(f)
            if col(r, "人工判定")
        }


def scan(dbs: list[str]) -> list[dict]:
    tasks = []
    for db in dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT task_id, video_id, status, config_json, state_json, created_at FROM tasks ORDER BY created_at DESC"):
                state = json.loads(row["state_json"] or "{}")
                if not (state.get("candidates") or []):
                    continue
                cfg = json.loads(row["config_json"] or "{}")
                tasks.append({
                    "task_id": row["task_id"], "video_id": row["video_id"], "status": row["status"],
                    "created_at": row["created_at"], "db": db,
                    "provider": cfg.get("model_provider") or "?",
                    "rule_version": cfg.get("prompt_pack_version") or cfg.get("rule_version") or "?",
                    "state": state,
                })
            conn.close()
        except sqlite3.Error:
            continue
    return tasks


def evidence_text(video: dict, ids: list[str], limit: int = 150) -> str:
    index = {e.get("evidence_id"): e for e in (video.get("evidence") or [])}
    picks = [index[i].get("text", "") for i in ids if i in index and index[i].get("field") != "frame"]
    text = " / ".join(p.strip().replace("\n", " ") for p in picks if p)
    return (text[:limit] + "…") if len(text) > limit else text


def gates_of(state: dict, candidate_id: str) -> tuple[str, str]:
    for review in state.get("reviews") or []:
        if review.get("candidate_id") != candidate_id:
            continue
        gates = {k: str((v or {}).get("status", "")) for k, v in (review.get("gates") or {}).items()}
        scores = {k: str((v or {}).get("score", "")) for k, v in (review.get("quality") or {}).items()}
        return "|".join(gates.get(f"G{i}", "—") for i in range(1, 5)), "".join(scores.get(f"Q{i}", "·") for i in range(1, 4))
    return "—", "—"


def render(tasks: list[dict], judged: set[tuple[str, str]], only_real: bool) -> str:
    deduped, counts = dedupe(tasks)
    real = [t for t in deduped if t["provider"] in REAL_PROVIDERS]
    fake = [] if only_real else [t for t in deduped if t["provider"] not in REAL_PROVIDERS]

    lines = [
        "# 待审队列",
        "",
        f"- 扫描库：{len({t['db'] for t in tasks})} 个，有候选的任务 {len(tasks)} 条",
        f"- 去重后真实模型产出（可计入金标准）：**{len(real)} 个视频**",
        f"- mock / 占位产出（**不能用来判质量**）：{len(fake)} 个视频",
        "",
        "> 每个视频只列一条（真实模型 > 当前规则 > 最新）。同一视频被跑过多次的，"
        "版本数见行内「同视频 N 次运行」。",
        "> mock 的候选是确定性占位（形如「绿字标题压屏…事实前置」），判它等于测 mock。别花时间。",
        "",
    ]

    if real:
        lines += ["## 一、真实模型产出：先判这些", ""]
        for i, t in enumerate(real, 1):
            video = t["state"].get("video") or {}
            route = t["state"].get("route") or {}
            decided = {d.get("candidate_id") for d in (t["state"].get("human_decisions") or [])}
            asr = video.get("asr_segments") or []
            ocr = video.get("ocr_segments") or []
            lines += [
                f"### {i}. {video.get('video_id')} · {video.get('title', '')[:40]}",
                "",
                f"- 任务 `{t['task_id']}`（{t['status']}）｜provider **{t['provider']}**｜规则 `{t['rule_version']}`｜同视频 {counts.get(t['video_id'], 1)} 次运行",
                f"- 库：`{t['db']}`",
                f"- 素材：ASR {len(asr)} 段 / OCR {len(ocr)} 段；时长 {video.get('duration')}s；领域 `{json.dumps(video.get('domain'), ensure_ascii=False)}`",
                f"- 系统路由：`{route.get('action')}`，推荐候选 `{route.get('recommended_candidate_id')}`",
                "",
                "| 候选 | 标题 | 字数 | G1\\|G2\\|G3\\|G4 | Q1Q2Q3 | 已判 |",
                "|---|---|---|---|---|---|",
            ]
            for c in t["state"].get("candidates") or []:
                cid = str(c.get("candidate_id"))
                head = str(c.get("rendered_title") or c.get("headline") or "")
                mech = (c.get("mechanical_checks") or {}).get("title_length", len(head))
                g, q = gates_of(t["state"], cid)
                flag = "✅" if (t["video_id"], cid) in judged else ("人已判" if cid in decided else "")
                lines.append(f"| `{cid}` | {head} | {mech} | {g} | {q} | {flag} |")
            lines.append("")
            for c in t["state"].get("candidates") or []:
                ev = evidence_text(video, c.get("evidence_ids") or [])
                if ev:
                    lines.append(f"- `{c.get('candidate_id')}` 引用证据：{ev}")
            desc = (video.get("source_description") or "").strip().replace("\n", " ")
            if desc:
                lines += ["", f"- 原始说明（可核对事实）：{desc[:220]}"]
            lines.append("")

    if fake:
        lines += ["## 二、mock / 旧链路产出：不建议占用评审时间", ""]
        for t in fake:
            video = t["state"].get("video") or {}
            head = ((t["state"].get("candidates") or [{}])[0].get("rendered_title") or (t["state"].get("candidates") or [{}])[0].get("headline") or "")[:24]
            lines.append(f"- `{t['task_id']}` · {t['video_id']} · {video.get('title', '')[:22]} · provider={t['provider']} · 首条「{head}」")
        lines.append("")

    return "\n".join(lines)


def emit_gold(tasks: list[dict], path: Path) -> None:
    """吐 gold.csv 骨架：系统列已填，人工列留空。"""
    header = ["video_id", "task_id", "candidate_id", "标题", "系统路由", "系统推荐",
              "人工判定", "人工最终标题", "命中的判据ID", "一句话理由", "判定人", "判定日期"]
    rows = []
    for t in tasks:
        route = t["state"].get("route") or {}
        for c in t["state"].get("candidates") or []:
            rows.append({
                "video_id": t["video_id"], "task_id": t["task_id"], "candidate_id": c.get("candidate_id"),
                "标题": c.get("rendered_title") or c.get("headline"), "系统路由": route.get("action"),
                "系统推荐": "是" if route.get("recommended_candidate_id") == c.get("candidate_id") else "",
            })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append")
    ap.add_argument("--gold", default=str(ROOT / "templates" / "gold.csv"), help="已判记录，用于去重")
    ap.add_argument("--only-real", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "runtime" / "review_queue.md"))
    ap.add_argument("--emit-gold")
    args = ap.parse_args()

    dbs = args.db or [d for d in sorted(glob.glob(str(ROOT / "runtime" / "**" / "agent.db"), recursive=True)) if "_legacy" not in d]
    tasks = scan(dbs)
    judged = load_judged(Path(args.gold))

    real_n = len([t for t in tasks if t["provider"] in REAL_PROVIDERS])
    report = render(tasks, judged, args.only_real)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已写入 {out}")

    if args.emit_gold:
        deduped, _ = dedupe(tasks)
        real = [t for t in deduped if t["provider"] in REAL_PROVIDERS]
        emit_gold(real or deduped, Path(args.emit_gold))
        print(f"已写入 {args.emit_gold}（{len(real or tasks)} 条任务）")
    print(f"\n真实模型任务 {real_n} 条 / 有候选任务 {len(tasks)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
