#!/usr/bin/env python3
"""把基线臂与系统臂的候选合成一个盲评批次。

对照实验要成立，三条必须同时满足：
1. 两臂看的是同一批视频（同一份 manifest）；
2. 两边由同一个人判，且判的时候不知道哪条来自哪一臂；
3. 记分口径一致——视频级「该视频有没有一条可用标题」。

第 2 条最容易被破坏：基线臂只有一个裸模型直出的标题，系统臂有推荐/待核/重写等状态。
只要界面把系统状态露出来，盲评立刻失效。所以本脚本给两条候选统一 `status`，
并在批次说明里写明「状态在评完后公布」——挡住的是信息泄露，不是透明。

用法：
    python3 scripts/build_compare_review.py \
        --baseline runtime/compare_v2_baseline/eval-v2.json \
        --current-batch runtime/compare_v2_current \
        --manifest templates/eval_manifest_v2.json \
        --out runtime/compare_v2_review
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "source" / "video_dataset.json"


def title_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def load_baseline(path: Path) -> tuple[dict[str, str], set[str], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    results = [r for r in data.get("results", []) if r.get("video_id")]
    ids = [r["video_id"] for r in results]
    if len(ids) != len(set(ids)):
        raise ValueError("基线视频 ID 重复，无法确定候选身份")
    # 新结果明确携带审核状态；未通过或审核异常的原稿仅保留在基线结果中，
    # 不能作为本工具的正式基线推荐。旧结果没有明确状态，带 error 的当审核异常处理，
    # 其余维持兼容但只算诊断（见 comparability.not_formal_reason）。
    titles = {
        r["video_id"]: str(r.get("title") or "")
        if r.get("audit_status") in (None, "passed") and not (r.get("audit_status") is None and r.get("error"))
        else ""
        for r in results
    }
    # 失败样本写在 errors 里，不进 results。只读 results 会让基线臂看起来
    # 「本来就只有这些视频」，两臂样本集不同也查不出来。
    covered = set(ids)
    errors = data.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("基线 errors 必须是列表")
    for err in errors:
        if (not isinstance(err, dict) or not isinstance(err.get("video_id"), str)
                or not err["video_id"].strip() or err["video_id"] != err["video_id"].strip()
                or not isinstance(err.get("error"), str) or not err["error"].strip()):
            raise ValueError("基线 errors 必须包含有效视频 ID 和错误信息")
        vid = err["video_id"]
        if vid in covered:
            raise ValueError(f"基线 errors 视频 ID 重复或与 results 冲突：{vid}")
        covered.add(vid)
    meta = {
        "audit_status_by_video": {
            r["video_id"]: r.get("audit_status") or ("legacy_error" if r.get("error") else "legacy_unknown")
            for r in results
        },
        "model_provider": data.get("model_provider"),
        "generation_model": data.get("generation_model"),
        "version": data.get("version"),
        "sample_size": data.get("sample_size"),
    }
    return titles, covered, meta


def _candidate_index(state: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for key in ("candidates", "delivery_candidates", "pending_review_candidates"):
        for candidate in state.get(key) or []:
            cid = str((candidate or {}).get("candidate_id") or "")
            if cid:
                if cid in index:
                    previous = index[cid]
                    previous_title = str(previous.get("rendered_title") or previous.get("headline") or "")
                    candidate_title = str(candidate.get("rendered_title") or candidate.get("headline") or "")
                    if previous_title != candidate_title:
                        raise ValueError(f"系统臂候选 ID 对应多个标题：{cid}")
                else:
                    index[cid] = candidate
    return index


def pick_delivery_candidate(state: dict) -> tuple[dict | None, str]:
    """按路由取推荐、待核或候选；待核不等于正式交付。

    未进入路由表面的历史候选不得冒充系统臂输出。待核候选仅供诊断，
    不得作为推荐或自动交付参与正式对照。
    """
    route = state.get("route") or {}
    index = _candidate_index(state)
    recommended = str(route.get("recommended_candidate_id") or "")
    if route.get("action") == "handoff" and recommended and recommended in index:
        return index[recommended], "recommended"
    for key, label in (("pending_review_candidate_ids", "pending_review"), ("eligible_candidate_ids", "eligible")):
        for cid in reversed([str(x) for x in (route.get(key) or [])]):
            if cid in index:
                return index[cid], label
    # 未交付的历史候选不得冒充系统臂输出。它们应在原批次诊断口径单独评审。
    return None, "none"


def load_current(db_path: Path) -> dict[str, dict]:
    """每个视频取一条交付面候选，并把「怎么挑的」和机械检查状态一并带出来核对。"""
    if not db_path.exists():
        raise SystemExit(f"系统臂任务库不存在（还没跑完？）：{db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT task_id, video_id, status, state_json FROM tasks"):
        if row["video_id"] in out:
            conn.close()
            raise ValueError(f"系统臂视频 ID 重复：{row['video_id']}，无法确定任务身份")
        status = str(row["status"] or "")
        if status not in {"pending_human", "stopped", "technical_failed", "approved", "approved_marginal"}:
            conn.close()
            raise ValueError(f"系统臂任务尚未终态或状态未知：{row['video_id']} ({status})")
        state = json.loads(row["state_json"] or "{}")
        route = state.get("route") or {}
        picked, selection = (None, "none") if status == "technical_failed" else pick_delivery_candidate(state)
        checks = (picked or {}).get("mechanical_checks") or {}
        out[row["video_id"]] = {
            "task_id": row["task_id"], "status": row["status"], "route": route,
            "stop_reason": state.get("stop_reason") or route.get("reason"),
            "video": state.get("video") or {},
            "selection": selection,
            "title": str((picked or {}).get("rendered_title") or (picked or {}).get("headline") or ""),
            "candidate_id": str((picked or {}).get("candidate_id") or ""),
            "mechanical_status": str(checks.get("status") or "unknown"),
            "mechanical_errors": [str(e.get("code") or "") for e in (checks.get("errors") or [])],
        }
    conn.close()
    return out, _current_meta(db_path.parent)


def _current_meta(batch_dir: Path) -> dict:
    """系统臂的模型与配置。缺这份文件就无法证明两臂可比。"""
    for name in ("config.json", "manifest.json"):
        path = batch_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if name == "config.json":
            return {"model_provider": data.get("model_provider"),
                    "generation_model": data.get("generation_model"),
                    "review_model": data.get("review_model"),
                    "source": str(path.name)}
        return {"generation_model": data.get("generation_model"),
                "source": str(path.name)}
    return {"source": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="runtime/compare_v2_baseline/eval-v2.json")
    ap.add_argument("--current-batch", default="runtime/compare_v2_current")
    ap.add_argument("--manifest", default="templates/eval_manifest_v2.json")
    ap.add_argument("--out", default="runtime/compare_v2_review")
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--diagnostic-only", action="store_true",
                    help="两臂模型不同时仍生成，但批次只能当诊断用")
    args = ap.parse_args()

    baseline, baseline_covered, baseline_meta = load_baseline(Path(args.baseline))
    current, current_meta = load_current(Path(args.current_batch) / "agent.db")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    samples = manifest["samples"]
    if not samples or any(not isinstance(s.get("video_id"), str) or not s["video_id"].strip() or s["video_id"] != s["video_id"].strip() for s in samples):
        raise ValueError("manifest 必须包含非空样本及规范的视频 ID")
    if len({s["video_id"] for s in samples}) != len(samples) or len({s["index"] for s in samples}) != len(samples):
        raise ValueError("manifest 中视频 ID 或序号重复")
    manifest_ids = {s["video_id"] for s in samples}

    # 配对的前提是两臂跑的是同一批样本。样本集不同就不是配对，不能靠取交集硬凑：
    # 交集会让「只在难样本上失败了的那一臂」看起来更好。
    only_baseline = baseline_covered - set(current)
    only_current = set(current) - baseline_covered
    outside = (baseline_covered | set(current)) - manifest_ids
    if outside:
        raise SystemExit(
            f"两臂存在 manifest 之外的视频 {len(outside)} 条，不能证明是同一份样本清单：{sorted(outside)[:5]}"
        )
    if only_baseline or only_current:
        raise SystemExit(
            "两臂样本集不相等，不是配对实验："
            f"仅基线有 {len(only_baseline)} 条、仅系统有 {len(only_current)} 条。"
            "请在同一份 manifest 上补齐两臂后重建，不要用交集代替配对。"
        )
    missing_baseline = manifest_ids - baseline_covered
    missing_current = manifest_ids - set(current)
    if missing_baseline or missing_current:
        raise SystemExit(
            "两臂未完整覆盖 manifest："
            f"基线缺 {len(missing_baseline)} 条 {sorted(missing_baseline)[:5]}，"
            f"系统缺 {len(missing_current)} 条 {sorted(missing_current)[:5]}。"
            "请补齐结果或显式记录失败后重建。"
        )

    # 模型不同就不是「同一任务的两个方案」对照，而是换了实验条件。
    b_model = str(baseline_meta.get("generation_model") or "")
    c_model = str(current_meta.get("generation_model") or "")
    model_mismatch = bool(b_model and c_model and b_model != c_model)
    if not b_model or not c_model:
        raise SystemExit(
            f"无法确认两臂模型（基线 {b_model or '未知'}／系统 {c_model or '未知'}）："
            "缺模型信息的批次只能做诊断，不能作为正式对照。"
        )
    if model_mismatch and not args.diagnostic_only:
        raise SystemExit(
            f"两臂模型不同（基线 {b_model}／系统 {c_model}）：这是换实验条件，不是对照。"
            "确认要做诊断批次再加 --diagnostic-only。"
        )
    rows = json.loads(TABLE.read_text(encoding="utf-8"))
    titles = {r["videoID"]: r.get("title") for r in (rows if isinstance(rows, list) else rows.get("videos") or [])}

    out_dir = Path(args.out)
    # 评审数据不可覆盖：即使目录中仅有旧反馈，也不能重建并让旧判定错配到新候选。
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(f"输出目录已存在，拒绝覆盖历史批次：{out_dir}")
    db_path = out_dir / "agent.db"

    rng = random.Random(args.seed)
    mapping: list[dict] = []
    skipped: list[dict] = []
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, config_json TEXT, state_json TEXT);"
    )

    for sample in manifest["samples"]:
        vid = sample["video_id"]
        system = current.get(vid) or {}
        arm_titles = []
        if baseline.get(vid):
            arm_titles.append(("baseline", baseline[vid]))
        if system.get("title"):
            arm_titles.append(("current", system["title"]))
        if len(arm_titles) < 2:
            skipped.append({
                "video_id": vid,
                "reason": "系统臂无选中标题（可能已有诊断候选）" if not system.get("title") else "基线臂无产出标题", 
                "arms": [a for a, _ in arm_titles],
                # 未产出也要留下可核对的系统身份：是 stop 掉的，还是候选机械检查没过，
                # 两者对交付率的含义完全不同。
                "system_task_status": system.get("status"),
                "system_route_action": (system.get("route") or {}).get("action"),
                "system_stop_reason": system.get("stop_reason"),
                "system_selection": system.get("selection"),
            })
        rng.shuffle(arm_titles)
        task_id = f"cmp-{vid}"
        candidates, frozen = [], []
        for i, (arm, title) in enumerate(arm_titles, 1):
            cid = f"cand-{i}"
            candidates.append({
                "candidate_id": cid, "rendered_title": title, "headline": title,
                "slot_id": "", "thesis_id": "", "prefix_labels": [],
                "generation_round": 0, "parent_candidate_id": None,
                "mechanical_checks": {}, "programmatic_g1_flags": [],
            })
            # 盲评期间两条候选必须同状态，否则界面一露状态盲评立刻失效；
            # 真实身份只写在 review_mapping.json 里，评完核对时用。
            frozen.append({"candidate_id": cid, "title_sha256": title_hash(title),
                           "headline": title, "status": "ready_for_human_review", "round": 0,
                           "arm": arm})
        state = {
            "video": {**system.get("video", {}), "video_id": vid, "title": titles.get(vid)},
            "candidates": candidates, "delivery_candidates": [], "pending_review_candidates": [],
            "candidate_decisions": [], "route": {}, "delivery_slots": {},
        }
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                     (task_id, vid, "pending_human", "{}", json.dumps(state, ensure_ascii=False)))
        mapping.append({
            "task_id": task_id, "blind_id": f"C{sample['index']:02d}", "category": sample["category"],
            "video_id": vid, "source_title": titles.get(vid),
            "input_quality": {},
            # 真实交付身份（评审台不读这些字段，只有本文件有）
            "task_status": system.get("status"), "route_action": (system.get("route") or {}).get("action"),
            "stop_reason": system.get("stop_reason"),
            "selection": system.get("selection"), "system_candidate_id": system.get("candidate_id"),
            "mechanical_status": system.get("mechanical_status"),
            "mechanical_errors": system.get("mechanical_errors") or [],
            "candidates": frozen,
        })
    conn.commit()
    conn.close()

    (out_dir / "review_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    selection_counts: dict[str, int] = {}
    for row in mapping:
        key = str(row.get("selection") or "unknown")
        selection_counts[key] = selection_counts.get(key, 0) + 1
    # 基线审核状态要单列：审核技术异常、审核拒绝、无审核证据（旧结果）三者对交付率
    # 的含义完全不同，混进「基线没写出来」会把技术故障读成能力不足。
    audit_status_counts: dict[str, int] = {}
    for status in (baseline_meta.get("audit_status_by_video") or {}).values():
        key = str(status)
        audit_status_counts[key] = audit_status_counts.get(key, 0) + 1
    legacy_audit = audit_status_counts.get("legacy_unknown", 0)
    note = {
        "purpose": "基线臂 vs 系统臂 · 盲评",
        "comparability": {
            "same_manifest": True,
            "videos_covered_both_arms": len(manifest_ids),
            "baseline_model": b_model,
            "current_model": c_model,
            "model_mismatch": model_mismatch,
            "baseline_meta": baseline_meta,
            "current_meta": current_meta,
            "baseline_audit_status_counts": audit_status_counts,
            # 只有样本、模型、判读方式都对得上，这批数才能当正式对照。
            # 旧基线结果没有明确审核状态（legacy_unknown），无法证明它被审核过，
            # 只能进诊断口径。
            "formal_comparison": not model_mismatch and not legacy_audit,
            "not_formal_reason": (
                "两臂模型不同" if model_mismatch else
                f"基线 {legacy_audit} 条缺少明确审核状态（旧结果），无法证明已审核" if legacy_audit else
                ""
            ),
        },
        "scoring": "此批仅用于诊断，必须按臂与真实 selection 分组；待核不等于推荐，未评和缺产须单列，不得直接使用混合候选统计。", 
        "blind": "两条候选来自不同臂，顺序已随机；系统状态在评完后公布",
        "videos": len(mapping), "incomplete_pairs": skipped,
        # 取样口径：进盲评的系统臂候选按交付面优先级挑（推荐→待核→可交付→兜底），
        # 每一类的条数都要能看到，否则「进盲评的其实全是兜底候选」不会被发现。
        "system_selection_counts": selection_counts,
        "system_status_counts": {
            str(row.get("task_status")): sum(1 for r in mapping if r.get("task_status") == row.get("task_status"))
            for row in mapping
        },
        "mechanical_status_counts": {
            str(row.get("mechanical_status")): sum(1 for r in mapping if r.get("mechanical_status") == row.get("mechanical_status"))
            for row in mapping
        },
    }
    (out_dir / "compare_note.json").write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"对照批次 {len(mapping)} 条视频，{sum(len(row['candidates']) for row in mapping)} 条候选 -> {out_dir}")
    print(f"系统臂取样口径：{json.dumps(selection_counts, ensure_ascii=False)}")
    if skipped:
        print(f"保留 {len(skipped)} 条不完整配对（不补造标题）：", json.dumps(skipped, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
