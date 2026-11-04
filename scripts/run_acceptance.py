#!/usr/bin/env python3
"""验收运行器：分层固定抽样跑 20 条，付款或鉴权失败时停止后续模型调用。

普通失败只影响当前样本；付款或鉴权不可恢复错误触发批次熔断。
已发出的并发请求无法撤回，但后续模型调用和重试会被阻止，所有样本保留终态。

抽样口径与原批次完全一致（同 SEED、同分层规则、每类 2 条），两次结果才可比。

用法：

    python scripts/run_acceptance.py                    # 写到 runtime/acceptance_<时间戳>/
    python scripts/run_acceptance.py --out runtime/xxx --workers 3 --retries 1
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import re
import sys
import threading
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.cli import make_pipeline  # noqa: E402
from agent_system.config import RunConfig  # noqa: E402
from agent_system.data import Dataset, assess_material  # noqa: E402
from agent_system.storage import Storage  # noqa: E402
from agent_system.model import ModelError  # noqa: E402

DEFAULT_SEED = 20261104  # 与原批次一致，保证抽到同一批视频
BATCH_KEY = "acceptance.deepseek.frozen.20"  # 冻结 20 条批次的固定 key，跨批次沿用以便按同一 task_id 对齐
ALLOWED_PROVIDERS = {"deepseek", "juflow", "siliconflow"}


def fatal_payment_or_auth(error: object) -> bool:
    """仅识别明确的付款/鉴权故障，不把普通不可重试错误扩大为批次故障。"""
    text = str(error)
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    return str(code) in ("401", "402", "403") or bool(
        re.search(r"\bHTTP\s+(?:401|402|403)\b|missing API key environment variable", text, re.I)
    )


class BatchStop:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reason = ""

    def trip(self, error: object) -> None:
        with self.lock:
            if not self.reason:
                self.reason = str(error)

    def get_reason(self) -> str:
        with self.lock:
            return self.reason


class GuardedAdapter:
    """在每次模型调用边界检查熔断；已进入的请求允许自然完成。"""
    def __init__(self, adapter, stop: BatchStop) -> None:
        self.adapter, self.stop = adapter, stop
        self.failure = ""

    def complete(self, **kwargs):
        with self.stop.lock:
            if self.stop.reason:
                self.failure = f"batch_stopped_payment_or_auth: {self.stop.reason}"
                raise ModelError(self.failure, retryable=False)
            # 此检查即调用准入点；已准入的请求属于并发在途请求。
        try:
            return self.adapter.complete(**kwargs)
        except Exception as exc:
            if fatal_payment_or_auth(exc):
                self.failure = str(exc)
                self.stop.trip(exc)
            raise


def reset_task(store: Storage, task_id: str) -> None:
    """重试前清掉半截状态：execute() 见到空 state 会从头重建。"""
    store.conn.execute("UPDATE tasks SET status='pending', state_json='{}' WHERE task_id=?", (task_id,))
    store.conn.commit()


def select_samples(dataset: Dataset, seed: int) -> list[tuple[str, dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in dataset.rows:
        domain = row["内容领域"]
        category = f"{domain['一级']}-{domain['二级']}" if domain["一级"] == "资讯" else domain["一级"]
        groups[category].append(row)
    rng = random.Random(seed)
    selected = [
        (category, row)
        for category in sorted(groups)
        for row in rng.sample(sorted(groups[category], key=lambda r: r["videoID"]), 2)
    ]
    if len(selected) != 20 or len(groups) != 10:
        raise RuntimeError(f"抽样口径异常：{len(groups)} 类 / {len(selected)} 条，预期 10 类 20 条")
    return selected


def select_from_manifest(path: Path, start: int, end: int, dataset: Dataset) -> tuple[list[tuple[int, str, dict]], dict]:
    """严格校验旧序号和视频映射；不根据历史状态筛选或替换。"""
    if start < 1 or end < start:
        raise ValueError("index 范围必须为正整数且起点不大于终点")
    raw = path.read_bytes()
    manifest = json.loads(raw)
    samples = manifest.get("samples") if isinstance(manifest, dict) else None
    if not isinstance(samples, list):
        raise ValueError("旧 manifest 缺少 samples 列表")
    by_index = {}
    for sample in samples:
        if not isinstance(sample, dict) or type(sample.get("index")) is not int or sample["index"] < 1:
            raise ValueError("旧 manifest 存在无效 index")
        if sample["index"] in by_index:
            raise ValueError(f"旧 manifest 存在重复 index：{sample['index']}")
        by_index[sample["index"]] = sample
    rows_by_id = defaultdict(list)
    for row in dataset.rows:
        rows_by_id[row["videoID"]].append(row)
    selected, seen = [], set()
    for index in range(start, end + 1):
        sample = by_index.get(index)
        if not sample:
            raise ValueError(f"旧 manifest 缺少 index {index}")
        video_id, category = sample.get("video_id"), sample.get("category")
        if not isinstance(video_id, str) or not video_id or not isinstance(category, str) or not category:
            raise ValueError(f"index {index} 缺少 video_id 或 category")
        if video_id in seen or len(rows_by_id.get(video_id, [])) != 1:
            raise ValueError(f"index {index} 的视频重复或无法唯一映射到数据集：{video_id}")
        seen.add(video_id)
        selected.append((index, category, rows_by_id[video_id][0]))
    return selected, {
        "source_manifest": str(path.resolve()),
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "source_config_sha256": manifest.get("config_sha256"),
        "index_start": start, "index_end": end,
        "non_continuous_experiment": True,
        "note": "独立补跑，非原实验的连续运行；不可视为原批次续跑。",
    }


def run_one(sample: dict, config, dataset, db_path: Path, retries: int, lock: threading.Lock, stop: BatchStop) -> dict:
    """跑一个样本，失败重试。无论结果如何都返回带终态的 dict。"""
    last_error = ""
    for attempt in range(retries + 1):
        store = Storage(db_path)
        try:
            reason = stop.get_reason()
            if reason:
                task = store.get_task(sample["task_id"])
                state = json.loads(task["state_json"] or "{}")
                state.update(stop_reason="batch_stopped_payment_or_auth", batch_stop_error=reason)
                store.set_state(sample["task_id"], "stopped", state)
                return {**sample, "status": "stopped", "error": state.get("error"), "route": state.get("route"),
                        "stop_reason": "batch_stopped_payment_or_auth", "batch_stop_error": reason,
                        "candidate_count": len(state.get("candidates") or []),
                        "rewrite_round": state.get("rewrite_round", 0), "attempts": attempt}
            if attempt:
                with stop.lock:
                    if stop.reason:
                        task = store.get_task(sample["task_id"])
                        state = json.loads(task["state_json"] or "{}")
                        state.update(stop_reason="batch_stopped_payment_or_auth", batch_stop_error=stop.reason)
                        store.set_state(sample["task_id"], "stopped", state)
                        return {**sample, "status": "stopped", "error": state.get("error"),
                                "route": state.get("route"), "stop_reason": state["stop_reason"],
                                "batch_stop_error": stop.reason, "candidate_count": len(state.get("candidates") or []),
                                "rewrite_round": state.get("rewrite_round", 0), "attempts": attempt}
                    with lock:
                        reset_task(store, sample["task_id"])
            pipeline = make_pipeline(config, dataset, store)
            pipeline.adapter = GuardedAdapter(pipeline.adapter, stop)
            pipeline.review_adapter = GuardedAdapter(pipeline.review_adapter, stop)
            print(f"{'RETRY' if attempt else 'START'} {sample['index']:02d} {sample['category']} {sample['video_id']}", flush=True)
            pipeline.execute(sample["task_id"])
            task = store.get_task(sample["task_id"])
            state = json.loads(task["state_json"] or "{}")
            # 有些节点会吞掉模型异常并返回降级结果；发生熔断仍须清晰记录。
            if stop.get_reason():
                state["batch_stop_error"] = stop.get_reason()
                state["batch_stop_reason"] = "batch_stopped_payment_or_auth"
                store.set_state(sample["task_id"], task["status"], state)
            result = {
                **sample,
                "status": task["status"],
                "route": state.get("route"),
                "stop_reason": state.get("stop_reason"),
                "error": state.get("error"),
                "candidate_count": len(state.get("candidates") or []),
                "rewrite_round": state.get("rewrite_round", 0),
                "attempts": attempt + 1,
            }
            if pipeline.adapter.failure or pipeline.review_adapter.failure:
                result["status"] = "technical_failed"
                result["error"] = pipeline.adapter.failure or pipeline.review_adapter.failure
                state.update(error=result["error"], stop_reason="batch_stopped_payment_or_auth")
                store.set_state(sample["task_id"], "technical_failed", state)
            if result["status"] == "technical_failed" and fatal_payment_or_auth(state.get("error") or ""): 
                stop.trip(state.get("error"))
            if result["status"] == "technical_failed" and stop.get_reason():
                result["stop_reason"] = "batch_stopped_payment_or_auth"
                result["batch_stop_error"] = stop.get_reason()
                state.update(stop_reason=result["stop_reason"], batch_stop_error=result["batch_stop_error"])
                store.set_state(sample["task_id"], "technical_failed", state)
                return result
            if task["status"] == "technical_failed" and attempt < retries:
                last_error = str(state.get("error") or "")
                print(f"FAILED {sample['index']:02d} (将重试): {last_error[:60]}", flush=True)
                continue
            print(f"DONE  {sample['index']:02d} {result['status']} cand={result['candidate_count']}", flush=True)
            return result
        except Exception as exc:  # 运行器自身异常也不能让线程静默死掉
            last_error = repr(exc)
            if fatal_payment_or_auth(exc):
                stop.trip(exc)
            if stop.get_reason() or attempt >= retries:
                print(f"EXC   {sample['index']:02d} {last_error[:70]}", flush=True)
                reason = stop.get_reason()
                try:
                    task = store.get_task(sample["task_id"])
                    state = json.loads(task["state_json"] or "{}")
                    state["error"] = last_error
                    if reason:
                        state.update(stop_reason="batch_stopped_payment_or_auth", batch_stop_error=reason)
                    store.set_state(sample["task_id"], "runner_exception", state)
                except Exception as persistence_error:
                    state = {}
                    last_error += f"; 无法持久化终态: {persistence_error!r}"
                return {**sample, "status": "runner_exception", "error": last_error, "route": state.get("route"),
                        "stop_reason": state.get("stop_reason"), "batch_stop_error": reason or None,
                        "candidate_count": len(state.get("candidates") or []),
                        "rewrite_round": state.get("rewrite_round", 0), "attempts": attempt + 1}
        finally:
            store.conn.close()
    return {**sample, "status": "technical_failed", "error": last_error, "route": None,
            "stop_reason": None, "candidate_count": 0, "rewrite_round": 0, "attempts": retries + 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="")
    parser.add_argument("--config", default="config.deepseek.json")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1, help="单个样本失败后的重试次数")
    parser.add_argument("--source-manifest", help="从旧批次 manifest 按原 index 选择，不重新抽样")
    parser.add_argument("--index-start", type=int, help="旧 index 起点（含）")
    parser.add_argument("--index-end", type=int, help="旧 index 终点（含）")
    args = parser.parse_args()
    source_args = (args.source_manifest, args.index_start, args.index_end)
    if any(value is not None for value in source_args) and not all(value is not None for value in source_args):
        parser.error("--source-manifest、--index-start 和 --index-end 必须同时指定")
    if args.workers < 1 or args.retries < 0:
        parser.error("workers 必须为正整数，retries 不得为负")

    out = Path(args.out).resolve() if args.out else (ROOT / "runtime" / f"acceptance_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    if args.source_manifest:
        source_path = Path(args.source_manifest).resolve()
        if out == source_path.parent or source_path.parent in out.parents:
            parser.error("补跑目录不能位于原批次目录内")
    if out.exists():
        raise SystemExit(f"输出目录已存在，拒绝复用：{out}")

    config = RunConfig.from_json(ROOT / args.config).apply_env()
    # 原来只放行 deepseek，本意是防「环境变量把配置悄悄改成别的 provider」。
    # 但换网关（juflow）是显式指定 --config 的正常用法，被一并挡掉了。
    # 现在改成白名单放行 + 把实际模型打进日志，防误用的目的不变、可查性反而更强。
    if config.model_provider not in ALLOWED_PROVIDERS:
        raise SystemExit(
            f"配置被环境覆盖成未登记的 provider：{config.model_provider}，停止以免误用模型"
            f"（已登记：{', '.join(sorted(ALLOWED_PROVIDERS))}）"
        )
    print(f"模型 {config.model_provider}/{config.generation_model}｜评审 {config.review_model}｜思考 {config.reasoning_effort or '默认'}", flush=True)
    config.output_dir = str(out)
    config.case_bank = ""  # 案例库为空，批次内人工评审前不更新

    # 表必须跟 config 走：写死主池表会让 --config 指定的另一个池（字段表不同）
    # 在视频 ID 不重合时直接报映射失败，重合时更糟——静默用错表还以为跑的是新池。
    table = config.table or str(ROOT / "data/source/video_dataset.json")
    dataset = Dataset.from_paths(table, config.asset_root, config.ocr_mode)
    provenance = None
    if args.source_manifest:
        try:
            selected, provenance = select_from_manifest(source_path, args.index_start, args.index_end, dataset)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"旧 manifest 选择无效：{exc}")
    else:
        selected = [(index, category, row) for index, (category, row) in enumerate(select_samples(dataset, args.seed), 1)]
    out.mkdir(parents=True, exist_ok=False)
    (out / "config.json").write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")

    storage = Storage(out / "agent.db")
    pipeline = make_pipeline(config, dataset, storage)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "selection": "实际内容领域分层，每类按 videoID 排序后固定随机抽2条；不参考历史判定或生成结果",
        "selection_policy": "推荐候选优先；无推荐取最后生成轮第一条；无候选保留失败，不替换视频",
        "purpose": f"验收基线｜{config.model_provider}/{config.generation_model}｜抽样与 acceptance_deepseek_20261101_114430 一致，可跨批次按 video_id 对齐",
        "memory": "新任务库，案例库为空，批次内人工评审前不更新",
        "config_sha256": hashlib.sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "samples": [],
    }
    if provenance:
        manifest.update(
            seed=None,
            selection=f"按旧 manifest 原始 index {args.index_start}–{args.index_end} 闭区间选择，不重新抽样",
            purpose=f"独立补跑｜{config.model_provider}/{config.generation_model}｜非原实验的连续运行",
            rerun_provenance=provenance,
        )
    for index, category, row in selected:
        task_id = pipeline.prepare(row["videoID"], BATCH_KEY)
        manifest["samples"].append({
            "index": index, "category": category, "video_id": row["videoID"], "task_id": task_id,
            "source_title": row["title"], "input_quality": assess_material(dataset.get(row["videoID"])),
        })
    storage.conn.close()
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"批次目录 {out}｜{len(manifest['samples'])} 条｜workers={args.workers} retries={args.retries}", flush=True)

    lock = threading.Lock()
    stop = BatchStop()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_one, sample, config, dataset, out / "agent.db", args.retries, lock, stop)
                   for sample in manifest["samples"]]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            results.sort(key=lambda r: r["index"])
            with lock:
                (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    import collections
    tally = collections.Counter(r["status"] for r in results)
    print(f"FINISHED {out}")
    print("状态分布: " + ", ".join(f"{k}={v}" for k, v in tally.most_common()))
    unfinished = [r["index"] for r in results if r["status"] in ("not_started_after_nonretryable_failure", "runner_exception")]
    if unfinished:
        print(f"仍有未完成样本: {unfinished}")
    return 1 if stop.get_reason() or any(r["status"] in ("technical_failed", "runner_exception") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
