#!/usr/bin/env python3
"""一键编排离线评测：prepare（跑批+骨架）/ finalize（副标注+对比）。

用法：
    python3 scripts/run_eval.py --mock                 # 不花额度，全链路验证
    python3 scripts/run_eval.py --config config.deepseek.json       # 真跑
    python3 scripts/run_eval.py --stage prepare --config config.deepseek.json
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["prepare", "finalize", "all"], default="all")
    ap.add_argument("--config", default="")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.stage in ('finalize', 'all'):
        raise SystemExit('旧版 finalize 无法验证人工标签身份和完整分母，已停用；请先使用可信评审流程。')

    py = sys.executable
    mock = ["--mock"] if args.mock else []
    config = ["--config", args.config] if args.config else []
    limit = ["--limit", str(args.limit)] if args.limit else []

    if args.stage in ("prepare", "all"):
        run([py, "scripts/baseline_run.py", *config, *mock])
        run([py, "scripts/run_manifest.py", *config, *mock, *limit])
        run([py, "scripts/gold_skeleton.py", "--run", "runtime/baseline/eval-v1.json", "--output", "runtime/gold/baseline.csv"])
        run([py, "scripts/gold_skeleton.py", "--run", "runtime/current/eval-v1.json", "--output", "runtime/gold/eval-v1.csv"])
    if args.stage in ("finalize", "all"):
        run([py, "scripts/model_review.py", "--gold", "runtime/gold/baseline.csv", "--system", "baseline", *config, *mock, "--output", "runtime/review/baseline.json"])
        run([py, "scripts/model_review.py", "--gold", "runtime/gold/eval-v1.csv", "--system", "current", *config, *mock, "--output", "runtime/review/current.json"])
        run([py, "scripts/eval_compare.py", "--baseline-labels", "runtime/review/baseline.json", "--current-labels", "runtime/review/current.json", "--output", "runtime/eval/compare.md"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
