#!/usr/bin/env python3
"""冻结不接触历史样本的独立 V2 验证 manifest。

口径：以原始 300 条池为全集，排除任何历史 agent.db 已接触的视频、已退休的 V2
样本，以及素材不完整/ASR 缺失的视频；剩余合规成员全部纳入，不为凑样本量回填。
这样 manifest 的成员集合、排除原因和来源快照都可复核，且不会把旧模型输出当成独立
验证证据。

用法：
    python3 scripts/build_independent_v2_manifest.py \
      --output runtime/validation_v2_independent_20261106/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POOL = Path("/Users/wanting/program/新项目/复杂agent/recent-portrait-300/三百条近期中文竖屏视频完整字段.json")
ASSET_ROOT = POOL.parent
RETIRED_MANIFESTS = (
    ROOT / "templates/validation_manifest.json",
    ROOT / "runtime/validation_v2_20261106/manifest.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def category_of(row: dict[str, Any]) -> str:
    domain = row.get("内容领域") or {}
    if isinstance(domain, dict):
        return f"{domain.get('一级', '')}-{domain.get('二级', '')}".strip("-")
    return str(domain)


def load_rows(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else list(raw.get("videos") or [])


def historical_contacts(runtime_root: Path, pool_ids: set[str]) -> tuple[set[str], dict[str, list[str]]]:
    contacted: set[str] = set()
    sources: dict[str, list[str]] = defaultdict(list)
    for db in sorted(runtime_root.rglob("agent.db")):
        try:
            conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
            ids = {row[0] for row in conn.execute("SELECT DISTINCT video_id FROM tasks") if row[0]}
            conn.close()
        except (OSError, sqlite3.Error):
            continue
        hits = sorted(ids & pool_ids)
        if hits:
            rel = str(db.relative_to(ROOT))
            sources[rel] = hits
            contacted.update(hits)
    return contacted, dict(sources)


def asset_reasons(video_id: str) -> list[str]:
    folder = ASSET_ROOT / video_id
    reasons: list[str] = []
    if not (ASSET_ROOT / f"{video_id}.mp4").exists():
        reasons.append("missing_mp4")
    if not (folder / "metadata.json").exists():
        reasons.append("missing_metadata")
    if not (folder / "ocr_raw.json").exists():
        reasons.append("missing_ocr")
    if not list(folder.glob("frame_*.jpg")):
        reasons.append("missing_frames")
    asr_path = folder / "asr_raw.json"
    if not asr_path.exists():
        reasons.append("missing_or_empty_asr")
    else:
        try:
            raw = json.loads(asr_path.read_text(encoding="utf-8"))
            segments = raw.get("segments", []) if isinstance(raw, dict) else []
            if not any(str(item.get("text", "")).strip() for item in segments if isinstance(item, dict)):
                reasons.append("missing_or_empty_asr")
        except (OSError, ValueError, TypeError):
            reasons.append("missing_or_empty_asr")
    return reasons


def sample_record(row: dict[str, Any], index: int) -> dict[str, Any]:
    video_id = str(row["videoID"])
    folder = ASSET_ROOT / video_id
    domain = row.get("内容领域") or {}
    return {
        "index": index,
        "video_id": video_id,
        "category": category_of(row),
        "domain_1": domain.get("一级") if isinstance(domain, dict) else None,
        "domain_2": domain.get("二级") if isinstance(domain, dict) else None,
        "form": row.get("内容形态", ""),
        "title": row.get("title", ""),
        "author_uid": row.get("作者UID"),
        "duration": row.get("duration"),
        "asset_dir": str(folder),
        "asr_sha256": sha256_file(folder / "asr_raw.json"),
        "ocr_sha256": sha256_file(folder / "ocr_raw.json"),
        "metadata_sha256": sha256_file(folder / "metadata.json"),
    }


def main() -> int:
    global POOL, ASSET_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", default=str(POOL))
    parser.add_argument("--asset-root", default=str(ASSET_ROOT))
    parser.add_argument("--runtime-root", default=str(ROOT / "runtime"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    POOL = Path(args.pool).expanduser().resolve()
    ASSET_ROOT = Path(args.asset_root).expanduser().resolve()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    rows = load_rows(POOL)
    pool_ids = {str(row.get("videoID")) for row in rows if row.get("videoID")}
    contacted, contact_sources = historical_contacts(runtime_root, pool_ids)

    retired: set[str] = set()
    retired_sources: list[dict[str, str]] = []
    for manifest in RETIRED_MANIFESTS:
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        ids = {str(item["video_id"]) for item in data.get("samples", []) if item.get("video_id")}
        retired.update(ids & pool_ids)
        retired_sources.append({
            "path": str(manifest.relative_to(ROOT)),
            "sha256": sha256_file(manifest),
            "sample_count": str(len(ids & pool_ids)),
        })

    rows_by_id = {str(row["videoID"]): row for row in rows if row.get("videoID")}
    selected_rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for video_id in sorted(pool_ids):
        reasons: list[str] = []
        if video_id in contacted:
            reasons.append("historically_contacted")
        if video_id in retired:
            reasons.append("retired_v2")
        reasons.extend(asset_reasons(video_id))
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            excluded.append({
                "video_id": video_id,
                "category": category_of(rows_by_id[video_id]),
                "reasons": reasons,
            })
            reason_counts.update(reasons)
        else:
            selected_rows.append(rows_by_id[video_id])

    selected_rows.sort(key=lambda row: (category_of(row), str(row["videoID"])))
    samples = [sample_record(row, index) for index, row in enumerate(selected_rows, 1)]
    member_text = "\n".join(item["video_id"] for item in samples) + "\n"
    member_hash = hashlib.sha256(member_text.encode("utf-8")).hexdigest()
    pool_hash = sha256_file(POOL)
    categories = Counter(item["category"] for item in samples)

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": "v2-independent-20261106",
        "created": "2026-11-06",
        "role": "独立 V2 验证集；SOURCE_TITLE_ONLY 修复后首次新鲜验证，旧任务/旧输出不计入本批证据",
        "pool": {
            "path": str(POOL),
            "total": len(rows),
            "sha256": pool_hash,
        },
        "selection": {
            "rule": "遍历原始 300 条池；排除历史 agent.db 接触、退休 V2 样本、缺 mp4/metadata/OCR/frame 或 ASR 为空者；剩余合规成员全部纳入，不为凑数回填新池",
            "member_count": len(samples),
            "member_sha256": member_hash,
            "category_counts": dict(sorted(categories.items())),
            "historical_contacted_count": len(contacted),
            "retired_v2_count": len(retired),
            "excluded_count": len(excluded),
            "reason_counts": dict(sorted(reason_counts.items())),
            "retired_manifests": retired_sources,
            "historical_source_databases": sorted(contact_sources),
        },
        "limitations": [
            "原始池不是平台随机样本，结论只代表本池覆盖范围。",
            "剩余合规成员为 184 条而非名义上的约 218 条，差额主要来自 ASR 缺失；不把缺 ASR 素材冒充可验证样本。",
            "历史接触检测依赖当前 runtime 下可读的 agent.db；manifest 冻结后不得向本批发起模型调用再改成员。",
            "本文件只冻结成员与素材指纹，不包含任何模型输出或人工质量结论。",
        ],
        "excluded": excluded,
        "samples": samples,
    }
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"原始池 {len(rows)}｜历史接触 {len(contacted)}｜退休 V2 {len(retired)}｜排除 {len(excluded)}｜冻结成员 {len(samples)}")
    print(f"成员 hash {member_hash}")
    print(f"已写入 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
