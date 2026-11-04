#!/usr/bin/env python3
"""把 templates/gold.csv 里的人工判定拆成 baseline / current 两份 labels.json。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def col(row, prefix):
    for k, v in row.items():
        if k and k.startswith(prefix):
            return (v or "").strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(ROOT / "templates" / "gold.csv"))
    ap.add_argument("--manifest", default=str(ROOT / "templates" / "eval_manifest.json"))
    ap.add_argument("--outdir", default=str(ROOT / "runtime" / "review"))
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    videos = {s["video_id"] for s in manifest.get("samples", [])}
    rows = list(csv.DictReader(open(args.gold, encoding="utf-8-sig", newline="")))
    # 当前系统只统计「交付层」候选（与 gold_skeleton 的 eligible 集合对齐），
    # 人工可能在评审台多判了重写轮候选，那些不进指标
    eval_rows = []
    eval_path = ROOT / "runtime" / "gold" / "eval-v1.csv"
    if eval_path.exists():
        eval_rows = list(csv.DictReader(open(eval_path, encoding="utf-8-sig", newline="")))
    eligible = {(col(r, "video_id"), col(r, "candidate_id")) for r in eval_rows}

    baseline, current = [], []
    for r in rows:
        vid = col(r, "video_id")
        cid = col(r, "candidate_id")
        verdict = col(r, "人工判定")
        if vid not in videos or not verdict:
            continue
        item = {"video_id": vid, "candidate_id": cid, "title": col(r, "标题"), "verdict": verdict, "reason": col(r, "一句话理由")}
        if cid == "baseline":
            baseline.append(item)
        elif (vid, cid) in eligible:
            current.append(item)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "human-baseline.json").write_text(json.dumps({"system": "baseline", "labels": baseline}, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "human-current.json").write_text(json.dumps({"system": "current", "labels": current}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"baseline {len(baseline)} 条 / current {len(current)} 条 -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
