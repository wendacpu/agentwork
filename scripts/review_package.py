#!/usr/bin/env python3
"""生成「评审包」：三档标准 + 每个视频的材料 + 候选标题，供人工判定。

用法：
    python3 scripts/review_package.py --gold runtime/gold/eval-v1.csv --output runtime/review/package-current.md
    python3 scripts/review_package.py --gold runtime/gold/baseline.csv --output runtime/review/package-baseline.md
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.config import RunConfig
from agent_system.data import Dataset

TABLE = ROOT / "data" / "source" / "video_dataset.json"

RUBRIC = """## 判定标准（三档）

| 判定 | 标准 | 一句话判据 |
|---|---|---|
| 可用 | 准确、清楚，能兑现视频主要内容，可以直接用 | 不用改一处 |
| 勉强通过 | 基本准确且可用，但存在明确瑕疵；必须写一句瑕疵 | 只差局部一处，改完读者预期不变 |
| 不可用 | 标题不能使用；写一句读者可能误解之处 | 要换看点/补事实/改方向，一处修改救不回来；改完读者预期会变 |

分歧高发在「勉强 vs 不可用」：问一句「改这一处之后，读者对视频的预期会不会变？」会变 → 不可用。

盲评：先看标题和材料，不看推荐/路由。只填「判定」+ 一句理由（勉强/不可用必填），不要求给改写方案。
"""


def col(row, prefix):
    for k, v in row.items():
        if k and k.startswith(prefix):
            return (v or "").strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    config = RunConfig()
    dataset = Dataset.from_paths(TABLE, config.asset_root, config.ocr_mode)
    rows = list(csv.DictReader(open(args.gold, encoding="utf-8-sig", newline="")))

    by_video = {}
    for r in rows:
        by_video.setdefault(col(r, "video_id"), []).append(r)

    lines = ["# 标题评审包", "", "来源: " + args.gold + " · 共 " + str(len(rows)) + " 条候选 / " + str(len(by_video)) + " 个视频", "", RUBRIC, "", "---", ""]
    for vid, items in by_video.items():
        try:
            video = dataset.get(vid)
            desc = (video.description or "")[:220]
            asr = " / ".join(str(s.get("text", "")) for s in (video.asr_segments or [])[:6])[:400]
            ocr = " / ".join(str(s.get("text", "")) for s in (video.ocr_segments or [])[:4])[:300]
            title = video.title or ""
        except Exception:
            desc = asr = ocr = title = "（材料不可用）"
        lines.append("## " + vid + " · " + title)
        lines.append("")
        lines.append("- 描述：" + desc)
        lines.append("- ASR：" + (asr or "（无）"))
        lines.append("- OCR：" + (ocr or "（无）"))
        lines.append("")
        lines.append("| 候选 | 标题 | 判定（可用/勉强通过/不可用） | 理由 |")
        lines.append("|---|---|---|---|")
        for it in items:
            cid = col(it, "candidate_id")
            t = col(it, "标题").replace("|", "/")
            lines.append("| " + cid + " | " + t + " |  |  |")
        lines.append("")

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print("评审包 -> " + str(path) + "（" + str(len(rows)) + " 条候选 / " + str(len(by_video)) + " 视频）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
