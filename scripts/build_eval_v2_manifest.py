#!/usr/bin/env python3
"""生成对照实验用的 40 条分层样本（eval-v2）。

为什么需要它：原固定评测集 `templates/eval_manifest.json` 只有 20 条，而后续
真正跑过的批次与它只重叠 3 条视频（口径漂了）。20 条样本上 6/20 vs 8/20 的
差异不构成任何统计结论，所以对照实验必须换一份更大的、两臂共用的样本。

抽样口径与原 20 条一致，只是每类由 2 条扩到 4 条：
- 分层单位：一级内容领域；「资讯」按二级拆开（与原批次 category 口径一致）
- 每类保留原有那 2 条，再按 videoID 排序后固定随机补 2 条
- 保留原有样本，是为了原批次的结论还能部分对上；新增 20 条用来把样本量拉起来

用法：
    python3 scripts/build_eval_v2_manifest.py --output templates/eval_manifest_v2.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"
BASE_MANIFEST = ROOT / "runtime" / "pro_juflow_20_20261102" / "manifest.json"
PER_CATEGORY = 4


def category_of(row: dict) -> str:
    domain = row.get("内容领域") or {}
    if not isinstance(domain, dict):
        return str(domain)
    first, second = domain.get("一级"), domain.get("二级")
    if first == "资讯" and second:
        return f"{first}-{second}"
    return str(first)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(ROOT / "templates" / "eval_manifest_v2.json"))
    ap.add_argument("--base-manifest", default=str(BASE_MANIFEST))
    ap.add_argument("--seed", type=int, default=20261102)
    args = ap.parse_args()

    rows = json.loads(TABLE.read_text(encoding="utf-8"))
    rows = rows if isinstance(rows, list) else (rows.get("videos") or [])
    by_category: dict[str, list[str]] = {}
    for row in rows:
        vid = row.get("videoID")
        if vid:
            by_category.setdefault(category_of(row), []).append(vid)

    base = json.loads(Path(args.base_manifest).read_text(encoding="utf-8"))
    seeded: dict[str, list[str]] = {}
    for sample in base.get("samples", []):
        seeded.setdefault(sample["category"], []).append(sample["video_id"])

    rng = random.Random(args.seed)
    samples: list[dict] = []
    report: list[str] = []
    for category in sorted(by_category):
        pool = sorted(by_category[category])
        keep = [v for v in seeded.get(category, []) if v in pool]
        rest = [v for v in pool if v not in keep]
        need = PER_CATEGORY - len(keep)
        if need > len(rest):
            report.append(f"{category}: 候选池不足（需 {PER_CATEGORY}，池 {len(pool)}），实取 {len(keep) + len(rest)}")
            need = len(rest)
        picked = rng.sample(rest, need) if need else []
        chosen = sorted(keep + picked)
        report.append(f"{category}: 池 {len(pool)}，保留 {len(keep)}，新增 {len(picked)}")
        for vid in chosen:
            samples.append({"category": category, "video_id": vid})

    samples.sort(key=lambda s: (s["category"], s["video_id"]))
    for i, sample in enumerate(samples, 1):
        sample["index"] = i

    out = {
        "version": "eval-v2",
        "created": "2026-11-02",
        "purpose": "对照实验样本：基线臂与系统臂共用同一份 40 条分层样本",
        "selection": f"一级内容领域分层（资讯按二级拆），每类 {PER_CATEGORY} 条；"
                     f"保留原 20 条样本，再按 videoID 排序后固定随机（seed={args.seed}）补足",
        "sample_size": len(samples),
        "samples": samples,
    }
    path = Path(args.output)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"样本 {len(samples)} 条 -> {path}")
    for line in report:
        print(" ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
