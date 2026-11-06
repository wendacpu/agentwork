#!/usr/bin/env python3
"""评审筛选评分：把跑批结果与人工标签 join，按 docs/评审模型选型与题库方案.md 第 4 节口径出指标。

口径要点（预先锁定，不随调用变化）：
- 单位是候选；主比较集 = semantic_label_eligible 的题（补评超长 10 条单列，不并入纯语义主比较）；
- 严格口径：模型通过 = approved；宽松口径：模型通过 = approved 或 marginal。两套并列报，不选有利的；
- 待核（pending）不进通过/失败分母，但计入有效评审；结构坏/调用失败记 failure，留在应评分母不当剔除；未执行的缺记录单列，绝不伪装成模型失败；
- 风险漏放需要单独核实过的风险标签，本脚本没有该输入，如实报「不可计算」，不拿 human_reason 关键词冒充；
- 单价须跑批前重新查证后手工传入，缺省只报 token 与延迟，不臆算金额；
- 同视频多候选相关不独立：按视频聚类另报题数与视频数。

用法：
    python3 scripts/review_screening_score.py \
        --result runtime/review_screening/mock_smoke.jsonl \
        --out runtime/review_screening/screening_report.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BANK_DIR = ROOT / "runtime" / "review_selection_development_v2"
STRICT_PASS = {"approved"}
STRICT_FAIL = {"marginal", "rejected"}
LENIENT_PASS = {"approved", "marginal"}
LENIENT_FAIL = {"rejected"}
HUMAN_STRICT_OK = {"approved"}
HUMAN_LENIENT_OK = {"approved", "marginal"}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_labels(bank_dir: Path) -> dict[str, dict]:
    labels = {x["item_id"]: x for x in _load_jsonl(bank_dir / "labels_provenance.jsonl")}
    if len(labels) != len(_load_jsonl(bank_dir / "labels_provenance.jsonl")):
        raise ValueError("题库标签标识重复")
    return labels


def load_results(paths: list[Path]) -> dict[str, dict[str, dict]]:
    """{model: {item_id: record}}；同模型重复题直接拒绝——跑批完整性问题不能悄悄取其一。"""
    by_model: dict[str, dict[str, dict]] = {}
    for path in paths:
        for record in _load_jsonl(path):
            model = str(record.get("model") or path.stem)
            bucket = by_model.setdefault(model, {})
            if record["item_id"] in bucket:
                raise ValueError(f"同模型重复题目：{model}/{record['item_id']}")
            bucket[record["item_id"]] = record
    return by_model


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def pct(value: float | None) -> str:
    return "不可计算" if value is None else f"{value * 100:.1f}%"


def _label_distribution(records: list[dict], labels: dict[str, dict]) -> dict[str, int]:
    """待核/失败也按人工标签列出，暴露选择性缺失，不把它们静默排除。"""
    return {decision: sum(labels[record["item_id"]]["human_decision"] == decision for record in records)
            for decision in ("approved", "marginal", "rejected")}


def score_model(records: dict[str, dict], labels: dict[str, dict], eligible: bool = True) -> dict:
    subset = {item_id: label for item_id, label in labels.items()
              if bool(label.get("semantic_label_eligible")) == eligible}
    unknown_ids = set(records) - set(labels)
    if unknown_ids:
        raise ValueError(f"结果含不属于题库的 item_id：{sorted(unknown_ids)[:3]}")
    matched = {item_id: record for item_id, record in records.items() if item_id in subset}
    expected = len(subset)
    valid = [record for record in matched.values() if record.get("parse_status") in ("ok", "pending")]
    pending = [record for record in valid if record.get("parse_status") == "pending"]
    decided = [record for record in valid if record.get("parse_status") == "ok"]
    failed = [record for record in matched.values() if record.get("parse_status") == "failure"]

    out = {
        "expected": expected,
        "in_results": len(matched),
        "missing_records": expected - len(matched),
        "coverage": _ratio(len(valid), expected),
        "missing_rate": _ratio(expected - len(matched), expected),
        "failure_rate": _ratio(len(failed), expected),
        "pending_rate": _ratio(len(pending), len(valid)),
        "pending_label_distribution": _label_distribution(pending, labels),
        "failure_label_distribution": _label_distribution(failed, labels),
        "latency_ms": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    for key in out["usage"]:
        out["usage"][key] = sum(int((record.get("usage") or {}).get(key) or 0) for record in matched.values())
    latencies = [int(record.get("latency_ms") or 0) for record in matched.values()]
    if latencies:
        ordered = sorted(latencies)
        p90_index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.9 + 0.999999)))
        out["latency_ms"] = {"median": statistics.median(ordered), "p90": ordered[p90_index]}

    def bucket_metrics(pass_set: set[str], fail_set: set[str], human_ok: set[str]) -> dict:
        model_pass = [record for record in decided if record.get("verdict") in pass_set]
        eligible_decided = [record for record in decided if record.get("verdict") in pass_set | fail_set]
        true_positive = sum(labels[record["item_id"]]["human_decision"] in human_ok for record in model_pass)
        human_ok_decided = sum(labels[record["item_id"]]["human_decision"] in human_ok for record in eligible_decided)
        return {
            "precision": _ratio(true_positive, len(model_pass)),
            "recall": _ratio(true_positive, human_ok_decided),
            "pass_share": _ratio(len(model_pass), expected),
            "verdict_counts": {verdict: sum(record.get("verdict") == verdict for record in decided)
                               for verdict in ("approved", "marginal", "rejected")},
        }

    out["strict"] = bucket_metrics(STRICT_PASS, STRICT_FAIL, HUMAN_STRICT_OK)
    out["lenient"] = bucket_metrics(LENIENT_PASS, LENIENT_FAIL, HUMAN_LENIENT_OK)
    out["exact_match"] = _ratio(sum(record.get("verdict") == labels[record["item_id"]]["human_decision"]
                                     for record in decided), len(decided))
    out["videos"] = len({label["video_id"] for label in subset.values()})
    out["risk_leak"] = "不可计算（需单独核实过的风险标签，不以 human_reason 关键词冒充）"
    return out


def render(models: dict[str, dict], excluded: dict[str, dict], note: str) -> str:
    lines = ["# 评审模型筛选报告", "", f"- 口径：docs/评审模型选型与题库方案.md 第 4 节，预先锁定。{note}",
             "- 主比较集为 semantic_label_eligible 候选；同视频多候选相关不独立，按视频聚类列示。", "",
             "| 模型 | 应评 | 有效覆盖 | 缺记录率 | 失败率 | 待核率 | 严格精准 | 严格召回 | 宽松精准 | 宽松召回 | 通过占比 | 三档全对 | 题/视频 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in models.items():
        st, ln = s["strict"], s["lenient"]
        lines.append(f"| {name} | {s['expected']} | {pct(s['coverage'])} | {pct(s['missing_rate'])} | {pct(s['failure_rate'])} | {pct(s['pending_rate'])} | "
                     f"{pct(st['precision'])} | {pct(st['recall'])} | {pct(ln['precision'])} | {pct(ln['recall'])} | "
                     f"{pct(st['pass_share'])} | {pct(s['exact_match'])} | {s['expected']}/{s['videos']} |")
    lines += ["", "## 每模型明细", ""]
    for name, s in models.items():
        lines += [f"### {name}",
                  f"- 判定分布（严格三档）：{s['strict']['verdict_counts']}；缺记录 {s['missing_records']}",
                  f"- token：{s['usage']}；延迟中位 {s['latency_ms'].get('median', '—')} ms",
                  f"- 风险漏放：{s['risk_leak']}",
                  "- 金额：单价须跑批前重新查证并传入，缺省不臆算。", ""]
    lines += ["## 单列：补评超长混合标签集（不并入主比较）", "",
              "| 模型 | 应评 | 有效覆盖 | 缺记录率 | 失败率 | 待核率 | 严格精准 | 严格召回 |", "|---|---|---|---|---|---|---|---|"]
    for name, s in excluded.items():
        lines.append(f"| {name} | {s['expected']} | {pct(s['coverage'])} | {pct(s['missing_rate'])} | {pct(s['failure_rate'])} | {pct(s['pending_rate'])} | "
                     f"{pct(s['strict']['precision'])} | {pct(s['strict']['recall'])} |")
    lines += ["", "> 本报告是开发集现象（历史 20 视频、单人标签、问题富集），不构成独立验收、不宣布冠军模型。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bank-dir", default=str(BANK_DIR))
    parser.add_argument("--result", action="append", required=True, help="可多次；一个模型一个结果 jsonl")
    parser.add_argument("--out", default="", help="markdown 报告输出（缺省只打印）")
    parser.add_argument("--json-out", default="", help="指标 JSON 输出")
    parser.add_argument("--note", default="", help="备注（如运行授权范围、批次日期）")
    args = parser.parse_args(argv)

    labels = load_labels(Path(args.bank_dir))
    by_model = load_results([Path(p) for p in args.result])
    main_scores = {m: score_model(r, labels, True) for m, r in sorted(by_model.items())}
    excluded_scores = {m: score_model(r, labels, False) for m, r in sorted(by_model.items())}
    report = render(main_scores, excluded_scores, args.note)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps({"main": main_scores, "excluded": excluded_scores},
                                                  ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
