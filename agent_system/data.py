from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import Evidence, VideoInput


def _norm_text(text: str | None) -> str:
    return re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _date(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ocr_segments(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        text = _norm_text(item.get("text"))
        if text:
            out.append({"segment_id": f"ocr-{i:04d}", "second": item.get("second"), "text": text, "source": "ocr_raw"})
    return out


def _asr_segments(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, item in enumerate(raw.get("segments", []) if isinstance(raw, dict) else []):
        text = _norm_text(item.get("text"))
        if text:
            out.append({"segment_id": f"asr-{i:04d}", "start": item.get("start"), "end": item.get("end"), "text": text, "source": "asr_raw"})
    return out


def _dedupe_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in segments:
        key = re.sub(r"\W+", "", item["text"]).lower()
        item = dict(item)
        item["duplicate"] = bool(key and key in seen)
        if key:
            seen.add(key)
        result.append(item)
    return result


class Dataset:
    def __init__(self, rows: list[dict[str, Any]], asset_root: str | Path, ocr_mode: str = "filtered") -> None:
        self.rows = rows
        self.asset_root = Path(asset_root)
        self.ocr_mode = ocr_mode

    @classmethod
    def from_paths(cls, table_path: str | Path, asset_root: str | Path, ocr_mode: str = "filtered") -> "Dataset":
        rows = json.loads(Path(table_path).read_text(encoding="utf-8"))
        return cls(rows, asset_root, ocr_mode)

    def get(self, video_id: str) -> VideoInput:
        row = next((r for r in self.rows if r["videoID"] == video_id), None)
        if row is None:
            raise KeyError(f"unknown video_id: {video_id}")
        folder = self.asset_root / video_id
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8")) if (folder / "metadata.json").exists() else {}
        src = metadata.get("source", {})
        asr = _dedupe_segments(_asr_segments(folder / "asr_raw.json")) if (folder / "asr_raw.json").exists() else []
        ocr = _dedupe_segments(_ocr_segments(folder / "ocr_raw.json")) if (folder / "ocr_raw.json").exists() else []
        if self.ocr_mode == "off":
            used_ocr = []
        elif self.ocr_mode == "raw":
            used_ocr = ocr
        else:
            used_ocr = [x for x in ocr if not x["duplicate"] and len(x["text"]) >= 2]
        evidence: list[Evidence] = []
        evidence.append(Evidence("e-title", "title", row.get("title", ""), "title", quality="source"))
        evidence.append(Evidence("e-description", "description", row.get("description", ""), "description", quality="model_derived_uncalibrated"))
        evidence.append(Evidence("e-caption", "caption", row.get("caption", ""), "caption", quality="model_derived_uncalibrated"))
        source_description = src.get("searchDesc")
        if source_description:
            evidence.append(Evidence("e-source-description", "source_description", str(source_description), "metadata.source.searchDesc", quality="unverified_source_metadata"))
        for s in asr:
            evidence.append(Evidence(f"e-{s['segment_id']}", "asr", s["text"], f"asr:{s.get('start')}-{s.get('end')}", quality="raw_asr"))
        for s in used_ocr:
            evidence.append(Evidence(f"e-{s['segment_id']}", "ocr", s["text"], f"ocr:{s.get('second')}s", quality="raw_ocr"))
        for i, frame in enumerate(sorted(folder.glob("frame_*.jpg"))):
            evidence.append(Evidence(f"e-frame-{i:03d}", "frame", str(frame), str(frame), quality="raw_frame"))
        paths = {
            "video": str(self.asset_root / f"{video_id}.mp4"),
            "folder": str(folder),
            "frames": [str(x) for x in sorted(folder.glob("frame_*.jpg"))],
            "metadata": str(folder / "metadata.json"),
            "asr": str(folder / "asr_raw.json"),
            "ocr": str(folder / "ocr_raw.json"),
        }
        hashes = {k: _hash(Path(v)) for k, v in paths.items() if k in {"video", "metadata", "asr", "ocr"} and Path(v).exists()}
        return VideoInput(
            video_id=video_id, title=row.get("title", ""), author=row.get("作者昵称", src.get("author", "")),
            author_uid=row.get("作者UID", src.get("authorMid")), url=row.get("videoURL", ""),
            domain=row.get("内容领域", {}), format=row.get("内容形态", ""),
            published_at=_date(row.get("发布时间", src.get("pubdate"))), duration=row.get("duration"),
            dimensions={"width": row.get("width"), "height": row.get("height")}, description=row.get("description", ""),
            caption=row.get("caption", ""), asr_segments=asr, ocr_segments=used_ocr, evidence=evidence,
            asset_paths=paths, provenance={"hashes": hashes, "description_source": "model_derived_uncalibrated", "caption_source": "model_derived_uncalibrated", "ocr_mode": self.ocr_mode, "metadata": metadata},
        )

    def audit(self) -> dict[str, Any]:
        out = {"count": len(self.rows), "missing_assets": [], "missing_asr": 0, "missing_ocr": 0, "empty_asr": 0, "warnings": []}
        for row in self.rows:
            vid = row["videoID"]
            folder = self.asset_root / vid
            required = [self.asset_root / f"{vid}.mp4", folder / "metadata.json", folder / "asr_raw.json", folder / "ocr_raw.json"]
            missing = [str(x) for x in required if not x.exists()]
            if missing:
                out["missing_assets"].append({"video_id": vid, "paths": missing})
            if not (folder / "asr_raw.json").exists(): out["missing_asr"] += 1
            if not (folder / "ocr_raw.json").exists(): out["missing_ocr"] += 1
            elif not _asr_segments(folder / "asr_raw.json"): out["empty_asr"] += 1
        return out


def _chinese_ratio(text: str) -> float:
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return 0.0
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff") / len(text)


def assess_material(video: VideoInput) -> dict[str, Any]:
    """输入质量闸：这堆材料够不够支撑一次"提炼看点"。

    已知事实：100 条样本里 18 条 ASR 为空；OCR 常有整屏乱码（诊断 D1 那条视频
    ASR 0 段、OCR 全是「RT 令日俄罗斯 Bi」这类碎片）。这类输入继续跑，模型只能靠
    description/caption（本身也是模型派生）反推，产出的标题人工也判不出错在哪。

    分级不看"文件在不在"，看"一手信息够不够"：
    - high：ASR >= 5 段（有连续话语链，语义单元能落地）
    - medium：有 ASR 但 < 5 段，或 ASR 空但有可用 OCR
    - low：ASR 空 且 OCR 中文占比 < 0.5（只剩标题与模型派生描述）
    """
    asr_count = len([x for x in video.asr_segments if _norm_text(x.get("text"))])
    ocr_text = " ".join(_norm_text(x.get("text")) for x in video.ocr_segments)
    ocr_count = len(video.ocr_segments)
    ocr_zh = round(_chinese_ratio(ocr_text), 3)
    usable_ocr = ocr_count if ocr_zh >= 0.5 else 0
    if asr_count >= 5:
        level = "high"
    elif asr_count >= 1 or usable_ocr >= 1:
        level = "medium"
    else:
        level = "low"
    reasons: list[str] = []
    if asr_count == 0:
        reasons.append("asr_empty")
    if ocr_count and ocr_zh < 0.5:
        reasons.append("ocr_low_chinese_ratio")
    if not video.title.strip():
        reasons.append("title_empty")
    return {
        "level": level,
        "asr_segments": asr_count,
        "ocr_segments": ocr_count,
        "ocr_chinese_ratio": ocr_zh,
        "usable_ocr_segments": usable_ocr,
        "has_source_title": bool(video.title.strip()),
        "reasons": reasons,
        "advice": {
            "high": "材料充足，正常提炼看点",
            "medium": "一手证据偏薄，语义单元必须以标记 corroboration=unverified 的谨慎口径输出",
            "low": "一手证据不足：只能以原视频标题为基线改写，标题须限制在 source_title 支持的范围，并强制人审",
        }[level],
    }

