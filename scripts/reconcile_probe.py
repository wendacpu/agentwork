#!/usr/bin/env python3
"""一次性实验：把「跨契约分歧」的判断交给模型，看它能否分开详略与互斥。

背景：词法判据已被证伪。真分歧（canary 剧情）overlap=0.333，而人读定性为同义详略的
那次也是 overlap=0.333——**任何阈值都分不开**。本脚本用**已保存的契约**问一次模型，
拿到语义判断，再与人工读法、词法判据三方对照。

不跑全流程、不重生成、不改任何任务状态。只读 runtime 下的 agent.db，调用模型一次/契约对。

用法：
  python3 scripts/reconcile_probe.py --limit 8            # 先试 8 对，看成本与质量
  python3 scripts/reconcile_probe.py                      # 全量
  python3 scripts/reconcile_probe.py --only BV1PwsR6oEKW  # 只看某条视频
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_system.model import ModelError, OpenAICompatibleAdapter  # noqa: E402
from agent_system.prompts import RECONCILE_SYSTEM, RECONCILE_USER_TEMPLATE  # noqa: E402
from agent_system.thesis import contracts_disagree  # noqa: E402

# 人工读法（我读契约得出的定性），用来判断模型判得对不对。只有这 6 个视频有。
HUMAN = {
    "BV1PwsR6oEKW": "contradiction",
    "BV1t9x56XEn7": "detail",
    "BV1u9HA6JEdw": "missing_point",
    "BV1WyHV6KEui": "contradiction?",
    "BV19AHd6PEAP": "detail",
    "BV1j6JR6AEAh": "detail",
}


def load_pairs(root: Path) -> list[dict]:
    seen: dict[str, dict] = {}
    for db in sorted(glob.glob(str(root / "**" / "agent.db"), recursive=True)):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT task_id, video_id, state_json FROM tasks").fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            try:
                state = json.loads(row["state_json"] or "{}")
            except json.JSONDecodeError:
                continue
            left, right = state.get("thesis_contract"), state.get("review_thesis_contract")
            if not (isinstance(left, dict) and isinstance(right, dict) and left and right):
                continue
            fingerprint = hashlib.md5(json.dumps([left.get("core_promise"), left.get("required_semantics"),
                                                  right.get("core_promise"), right.get("required_semantics")],
                                                 ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
            key = f"{row['video_id']}:{fingerprint}"
            if key in seen:
                continue
            evidence_by_id = {str(e.get("evidence_id")): e for e in ((state.get("video") or {}).get("evidence") or []) if isinstance(e, dict)}
            seen[key] = {"key": key, "video_id": row["video_id"], "task_id": row["task_id"],
                         "generated": left, "reviewed": right, "evidence": evidence_by_id}
        conn.close()
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="runtime")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--key-env", default="DEEPSEEK_API_KEY")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    if not os.getenv(args.key_env):
        print(f"缺少 {args.key_env}；请用 scripts/run.sh 或先 source .secrets.env", file=sys.stderr)
        return 2

    pairs = load_pairs(Path(args.root))
    if args.only:
        pairs = [p for p in pairs if p["video_id"] == args.only]
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        print("没有可比的契约对")
        return 0

    adapter = OpenAICompatibleAdapter(args.base_url, args.key_env, args.model, reasoning_effort="off")
    print(f"待比对契约对 {len(pairs)} 个，模型 {args.model}\n")

    def probe(pair: dict) -> dict:
        user = {"thesis_contract": pair["generated"], "review_thesis_contract": pair["reviewed"]}
        try:
            reply = adapter.complete(task="thesis_consistency", system=RECONCILE_SYSTEM,
                                     user=user, schema=RECONCILE_USER_TEMPLATE)
            data = reply.data if isinstance(reply.data, dict) else {}
        except ModelError as exc:
            return {**pair, "error": str(exc)}
        relation = str(data.get("relation") or "").strip()
        lexical = contracts_disagree(pair["generated"], pair["reviewed"], pair["evidence"])
        return {**pair, "relation": relation, "point": str(data.get("contradiction_point") or "").strip(),
                "reason": str(data.get("reason") or "").strip(),
                "lexical_triggered": bool(lexical),
                "lexical_overlap": (lexical or {}).get("semantics_overlap"),
                "human": HUMAN.get(pair["video_id"], "")}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(probe, pairs))

    model_hits = sum(1 for r in results if r.get("relation") == "contradiction")
    lexical_hits = sum(1 for r in results if r.get("lexical_triggered"))
    print(f"{'视频':14s} {'模型判定':16s} {'词法':4s} {'人工读法':16s} 说明")
    for r in sorted(results, key=lambda x: (x["video_id"], x["key"])):
        flag = "" if not r.get("error") else f"  错误:{r['error'][:40]}"
        print(f"{r['video_id']:14s} {r.get('relation',''):16s} {'是' if r.get('lexical_triggered') else '否':4s} "
              f"{r.get('human',''):16s} {(r.get('point') or r.get('reason') or '')[:50]}{flag}")

    print(f"\n模型判互斥 {model_hits}/{len(results)}；词法触发 {lexical_hits}/{len(results)}")
    labeled = [r for r in results if r.get("human")]
    if labeled:
        print(f"\n有人工读法的 {len(labeled)} 对：")
        for r in labeled:
            want = r["human"].rstrip("?")
            got = r.get("relation")
            ok = "✓" if got == want else ("?" if r["human"].endswith("?") else "✗")
            print(f"  {ok} {r['video_id']:14s} 模型={got:16s} 人工={r['human']:16s} 词法={'触发' if r['lexical_triggered'] else '不触发'}")

    if args.json_out:
        slim = [{k: v for k, v in r.items() if k != "evidence"} for r in results]
        Path(args.json_out).write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
