#!/usr/bin/env python3
"""【已停用】旧的「基线 vs 当前」对比表生成器。

`summarize()` 仍被复现脚本与测试引用，保留原样；`main()` 现在只做输入校验后
直接阻断，不再输出对比表。原因是旧标签格式无法区分「未产出」与「未评」，也不携带
候选身份，用它出正式配对结论会把缺评算成失败、把失败样本移出分母。

需要正式对照结论时用 scripts/review_quality_report.py（按臂与选择方式分组）
与 scripts/metrics_vs_target.py（冻结分母 + 未评显式标注）。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

VERDICTS = ("可用", "勉强通过", "不可用")


def summarize(labels: list[dict], max_chars: int, denom: set[str] | None = None) -> dict:
    # 配对了分母就只统计配对集合内的标签：集合外的行会把超长率和判定分布算偏，
    # 而"配对视频数"却看不出来。
    if denom is not None:
        labels = [l for l in labels if l.get("video_id") in denom]
    by_video: dict[str, list[dict]] = {}
    for l in labels:
        by_video.setdefault(l["video_id"], []).append(l)
    videos = sorted(denom) if denom is not None else sorted(by_video)
    n_videos = len(videos)
    strict = sum(1 for v in videos if any(r.get("verdict") == "可用" for r in by_video.get(v, [])))
    loose = sum(1 for v in videos if any(r.get("verdict") in ("可用", "勉强通过") for r in by_video.get(v, [])))
    long_titles = sum(1 for l in labels if len(str(l.get("title") or "")) > max_chars)
    total_titles = len(labels)
    dist = Counter(l.get("verdict") or "?" for l in labels)
    return {
        "videos": n_videos, "strict": strict, "loose": loose,
        "strict_rate": strict / n_videos if n_videos else 0.0,
        "loose_rate": loose / n_videos if n_videos else 0.0,
        "long": long_titles, "titles": total_titles,
        "long_rate": long_titles / total_titles if total_titles else 0.0,
        "dist": dict(dist),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-labels", required=True)
    ap.add_argument("--current-labels", required=True)
    ap.add_argument("--baseline-run", default="")
    ap.add_argument("--current-run", default="")
    ap.add_argument("--manifest", required=True, help="冻结样本清单，必须包含 samples[].video_id")
    ap.add_argument("--output", default="runtime/eval/compare.md")
    ap.add_argument("--max-chars", type=int, default=20)
    args = ap.parse_args()

    bl = json.loads(Path(args.baseline_labels).read_text(encoding="utf-8"))
    cu = json.loads(Path(args.current_labels).read_text(encoding="utf-8"))
    bl_labels = bl.get("labels", [])
    cu_labels = cu.get("labels", [])

    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    ids = [s.get('video_id') for s in manifest.get('samples', [])]
    if not ids or any(not x for x in ids) or len(ids) != len(set(ids)):
        raise SystemExit('冻结样本清单为空或存在重复/缺失 ID')
    denom = set(ids)
    # 本工具旧标签格式不能区分「未产出」与「未评」，且不携带足够身份信息。
    # 不得自动把空标签当失败并宣布可比；先阻止正式报告。
    raise SystemExit('旧版标签格式无法验证候选身份、未产出和未评状态；禁止生成正式配对结论，请使用完整身份评审工具。')

    sb = summarize(bl_labels, args.max_chars, denom)
    sc = summarize(cu_labels, args.max_chars, denom)

    def pct(x):
        return f"{x * 100:.1f}%"

    def delta(a, b):
        return f"{(b - a) * 100:+.1f}%"

    lines = [
        "# 基线 vs 当前系统 · 评测对比",
        "",
        f"- 长度上限：{args.max_chars} 字",
        f"- 配对视频：{sb['videos']} 条（基线 {bl.get('system', 'baseline')} / 当前 {cu.get('system', 'current')}）",
        f"- 候选条数：基线 {sb['titles']} / 当前 {sc['titles']}",
        "",
        "| 指标 | 基线 | 当前 | Δ |",
        "|---|---|---|---|",
        f"| 一次通过率（严格） | {pct(sb['strict_rate'])} | {pct(sc['strict_rate'])} | {delta(sb['strict_rate'], sc['strict_rate'])} |",
        f"| 一次通过率（含勉强） | {pct(sb['loose_rate'])} | {pct(sc['loose_rate'])} | {delta(sb['loose_rate'], sc['loose_rate'])} |",
        f"| 超长流入率 | {pct(sb['long_rate'])} | {pct(sc['long_rate'])} | {delta(sb['long_rate'], sc['long_rate'])} |",
        "",
        "## 判定分布",
        "",
        "| 判定 | 基线 | 当前 |",
        "|---|---|---|",
    ]
    for v in VERDICTS:
        lines.append(f"| {v} | {sb['dist'].get(v, 0)} | {sc['dist'].get(v, 0)} |")

    lines += ["", "> 分母是两边都跑到的视频集合；没有可用候选的视频按「失败」计入分母，不会被悄悄剔除。",
              "> 这批数用于「当前系统 vs 基线」的相对比较，不是线上 CTR 预测。"]
    report = "\n".join(lines)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
