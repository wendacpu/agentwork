#!/usr/bin/env python3
"""R01 评审模型能力筛选跑批：对盲题库每题做三档直评。

此工具只做开发题库中的固定标题复评：不生成/改写标题，不写任务库或案例库，也不读取
``labels_provenance.jsonl``。它不是独立验收，更不是端到端流水线评估。

固定输入：候选标题，以及盲题库白名单中的视频元数据、ASR/OCR（各最多 20 段）。
输出：每题一条独立 JSONL 记录，含所有尝试的原始响应、耗时、token 用量与错误；模型输出
三档之外为 ``pending``，不能解析或调用失败为 ``failure``。失败不从分母中消失。

真跑必须显式给出 ``--config``、``--max-items`` 和 ``--out``，且模型读取配置的
``review_model``（不是 generation_model）。付费候选、价格、预算、重试/超时上限仍须事前
获用户批准；本脚本不会替代该授权。

用法：
    python3 scripts/review_screening.py --mock --limit 2 --out runtime/review_screening/mock.jsonl
    python3 scripts/review_screening.py --bank runtime/review_selection_development_v2/blind_inputs.jsonl --estimate
    python3 scripts/review_screening.py --config config.review_screening.json --env-file .env.siliconflow \
      --max-items 76 --out runtime/review_screening/deepseek-v4.1-flash.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.cli import PROVIDER_DEFAULTS
from agent_system.model import MockAdapter, ModelError, OpenAICompatibleAdapter

BANK = ROOT / "runtime" / "review_selection_development_v2" / "blind_inputs.jsonl"
SYSTEM = """你是短视频标题评审。只看标题和必要的视频材料，判这条标题能不能用：
- 可用：准确、清楚，能兑现视频主要内容，不用改一处；
- 勉强通过：基本可用但有明确瑕疵，只差局部一处；
- 不可用：要换看点/补事实/改方向，一处修改救不回来。
只输出 JSON：{"verdict": "可用" 或 "勉强通过" 或 "不可用", "reason": "一句话"}"""
VERDICT_MAP = {"可用": "approved", "勉强通过": "marginal", "不可用": "rejected"}
MAX_SEGMENTS = 20
MATERIAL_FIELDS = ("video_id", "title", "author", "domain", "format", "description", "caption")
LEAK_KEYS = {"human_decision", "human_reason", "human_final_headline", "decision", "verdict", "labels"}


def load_blind(path: Path) -> list[dict[str, Any]]:
    """加载标签盲输入，并在发请求前硬拒绝标签字段或重复题目。"""
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [str(item.get("item_id") or "") for item in items]
    if not items or any(not item_id for item_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("盲题库为空、缺失题目标识或题目标识重复")
    def forbidden_keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return ({str(key) for key in value if str(key) in LEAK_KEYS}
                    | set().union(*(forbidden_keys(child) for child in value.values())))
        if isinstance(value, list):
            return set().union(*(forbidden_keys(child) for child in value))
        return set()

    leaked = set().union(*(forbidden_keys(item) for item in items))
    if leaked:
        raise ValueError(f"盲输入混入标签字段：{sorted(leaked)}")
    return items


def build_payload(item: dict[str, Any]) -> dict[str, Any]:
    """只白名单透传盲输入中的材料；不把未知字段、历史状态或人工标签带给模型。"""
    materials = item.get("materials") or {}
    video = {field: materials.get(field) for field in MATERIAL_FIELDS if field in materials}
    for short_name, field in (("asr", "asr_segments"), ("ocr", "ocr_segments")):
        video[short_name] = [str(segment.get("text", "")) for segment in (materials.get(field) or [])][:MAX_SEGMENTS]
    return {"title": str(item.get("candidate_title") or ""), "video": video}


def _accumulate(total: dict[str, int], usage: dict[str, Any] | None) -> None:
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = (usage or {}).get(field)
        if isinstance(value, int):
            total[field] = total.get(field, 0) + value


def _error_record(attempt: int, exc: Exception) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "status": "error",
        "error": str(exc)[:1000],
        "retryable": bool(getattr(exc, "retryable", True)),
        "raw_response": str(getattr(exc, "raw", "") or ""),
    }


def judge(adapter: Any, payload: dict[str, Any], retry_limit: int) -> dict[str, Any]:
    """按重试上限评一题；不重试有效但三档外的 pending，也不重试明确不可重试错误。"""
    attempts: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    latency_ms = 0
    final: dict[str, Any] = {"parse_status": "failure", "verdict": None, "verdict_raw": None, "reason": ""}
    for attempt in range(retry_limit + 1):
        try:
            reply = adapter.complete(task="model_review", system=SYSTEM, user=payload, schema="review", attempt=attempt)
        except Exception as exc:  # 单题失败必须记录后继续下一题。
            attempts.append(_error_record(attempt, exc))
            if isinstance(exc, ModelError) and not exc.retryable:
                break
            continue
        latency_ms += int(reply.latency_ms or 0)
        _accumulate(usage, reply.usage)
        data = reply.data if isinstance(reply.data, dict) else {}
        raw_verdict = str(data.get("verdict") or "")
        attempt_record = {
            "attempt": attempt,
            "status": "response",
            "raw_response": reply.raw_text or "",
            "usage": reply.usage or {},
            "latency_ms": int(reply.latency_ms or 0),
        }
        if raw_verdict in VERDICT_MAP:
            attempt_record["parse_status"] = "ok"
            attempts.append(attempt_record)
            final = {"parse_status": "ok", "verdict": VERDICT_MAP[raw_verdict], "verdict_raw": raw_verdict,
                     "reason": str(data.get("reason") or "")}
            break
        if data:
            attempt_record["parse_status"] = "pending"
            attempts.append(attempt_record)
            final = {"parse_status": "pending", "verdict": None, "verdict_raw": raw_verdict,
                     "reason": str(data.get("reason") or "")}
            break
        attempt_record["parse_status"] = "invalid"
        attempts.append(attempt_record)
    return {**final, "attempts": attempts, "latency_ms": latency_ms, "usage": usage}


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("_")[:60] or "unnamed"


def run_screening(items: list[dict[str, Any]], adapter: Any, model: str, out_path: Path,
                  retry_limit: int) -> list[dict[str, Any]]:
    """写入可恢复的 partial 文件；完整完成才原子替换到最终结果。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = out_path.with_suffix(out_path.suffix + ".partial")
    if partial_path.exists():
        raise FileExistsError(f"发现上次未完成输出，先人工处理后再运行：{partial_path}")
    records: list[dict[str, Any]] = []
    with partial_path.open("x", encoding="utf-8") as file:
        for item in items:
            result = judge(adapter, build_payload(item), retry_limit)
            record = {
                "item_id": item["item_id"],
                "video_id": (item.get("materials") or {}).get("video_id"),
                "candidate_title": item.get("candidate_title"),
                "model": model,
                "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **result,
            }
            records.append(record)
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            file.flush()
    partial_path.replace(out_path)
    return records


def write_meta(out_path: Path, meta: dict[str, Any]) -> None:
    out_path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_env_file(path: str) -> None:
    """读取 KEY=VALUE 到进程环境；不输出文件内容，也不覆盖已经存在的显式环境变量。"""
    if not path:
        return
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"缺少环境文件：{source}")
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _config_fingerprint(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """只保留非秘密执行参数，用可复算摘要证明同一模型运行条件。"""
    public = {key: config.get(key) for key in (
        "model_provider", "base_url", "api_key_env", "review_model", "model_timeout", "reasoning_effort",
        "structured_output",
    )}
    serialized = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bank", default=str(BANK))
    parser.add_argument("--config", default="", help="RunConfig JSON；真跑必须提供")
    parser.add_argument("--env-file", default="", help="KEY=VALUE 凭据文件；内容不回显")
    parser.add_argument("--out", default="", help="结果 JSONL；真跑必须显式提供，已有文件拒绝覆盖")
    parser.add_argument("--mock", action="store_true", help="零成本离线冒烟（MockAdapter，不联网）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--max-items", type=int, default=0, help="预算闸：本次题数超过即拒绝启动")
    parser.add_argument("--retry-limit", type=int, default=2, help="调用/解析失败的额外重试次数")
    parser.add_argument("--estimate", action="store_true", help="不调模型，只输出题数和输入体积")
    args = parser.parse_args(argv)
    if args.limit < 0 or args.max_items < 0 or args.retry_limit < 0:
        raise SystemExit("limit、max-items 和 retry-limit 不能为负数")

    bank_path = Path(args.bank)
    items = load_blind(bank_path)
    if args.estimate:
        sizes = sorted(len(json.dumps(build_payload(item), ensure_ascii=False)) for item in items)
        print(json.dumps({"items": len(items), "avg_payload_chars": sum(sizes) // len(sizes),
                          "median_payload_chars": sizes[len(sizes) // 2], "max_payload_chars": sizes[-1],
                          "truncation": {"asr": MAX_SEGMENTS, "ocr": MAX_SEGMENTS}}, ensure_ascii=False))
        return 0

    if args.limit:
        items = items[:args.limit]
    if args.mock:
        adapter: Any = MockAdapter()
        model, provider, config_hash, public_config = "mock", "mock", "", {}
    else:
        if not args.config or not args.max_items or not args.out:
            raise SystemExit("真跑必须同时提供 --config、--max-items 和 --out")
        _load_env_file(args.env_file)
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        provider = str(config.get("model_provider") or "")
        if provider not in PROVIDER_DEFAULTS:
            raise SystemExit(f"unknown model_provider: {provider}")
        model = str(config.get("review_model") or "")
        if not model:
            raise SystemExit("config.review_model 缺失；筛选模型不得回退到 generation_model")
        default_base, default_key = PROVIDER_DEFAULTS[provider]
        adapter = OpenAICompatibleAdapter(
            str(config.get("base_url") or default_base), str(config.get("api_key_env") or default_key), model,
            timeout=int(config.get("model_timeout") or 120),
            reasoning_effort=str(config.get("reasoning_effort") or ""), structured_output="off",
        )
        config_hash, public_config = _config_fingerprint(config)

    if args.max_items and len(items) > args.max_items:
        raise SystemExit(f"本次将发 {len(items)} 题，超过预算闸 {args.max_items}")
    out_path = Path(args.out or ROOT / "runtime" / "review_screening" / f"{time.strftime('%Y%m%d')}_{_slug(model)}.jsonl")
    if out_path.exists():
        raise SystemExit(f"输出已存在，拒绝覆盖：{out_path}")

    records = run_screening(items, adapter, model, out_path, args.retry_limit)
    counts = {status: sum(record["parse_status"] == status for record in records)
              for status in ("ok", "pending", "failure")}
    write_meta(out_path, {
        "kind": "review-capability-screening", "provider": provider, "model": model,
        "structured_output": "off", "config_sha256": config_hash, "public_config": public_config,
        "items": len(records), "counts": counts, "retry_limit": args.retry_limit, "max_segments": MAX_SEGMENTS,
        "bank": str(bank_path), "bank_sha256": hashlib.sha256(bank_path.read_bytes()).hexdigest(),
        "system_sha256": hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest(),
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    print(json.dumps({"out": str(out_path), "items": len(records), **counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
