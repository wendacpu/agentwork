from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import ModelAdapter, ModelReply
from .types import VideoInput


BASELINE_SYSTEM = """你是短视频标题写手。根据给定的视频材料（标题、描述、字幕、画面文字、领域、形态），写一条中文标题，要求：
1. 不超过 20 个字；
2. 不加书名号，不以问号结尾；
3. 直接给标题，不要解释。
只输出 JSON：{"title": "..."}"""

AUDIT_SYSTEM = """你是短视频标题的基础审核员。检查给定标题能否用于推荐：
1. 长度不超过 20 字；
2. 不以问号结尾；
3. 与材料相比没有明显的事实错误。
只输出 JSON：{"pass": true 或 false, "reason": "一句话说明"}"""


def video_payload(video: VideoInput) -> dict[str, Any]:
    """把视频的全部输入项摊平给「裸模型」——这是基线的定义：不做理解/边界/看点/评审。"""
    return {
        "video": {
            "title": video.title,
            "description": video.description,
            "caption": video.caption,
            "asr": [str(seg.get("text", "")) for seg in (video.asr_segments or [])][:40],
            "ocr": [str(seg.get("text", "")) for seg in (video.ocr_segments or [])][:40],
            "domain": video.domain,
            "format": video.format,
        }
    }


def run_baseline(video: VideoInput, adapter: ModelAdapter, audit_adapter: ModelAdapter | None = None) -> dict[str, Any]:
    """基线：大模型直出 1 条标题 → 基础审核模型。返回可落盘的结果。"""
    payload = video_payload(video)
    gen = adapter.complete(task="baseline_generate", system=BASELINE_SYSTEM, user=payload, schema="baseline")
    title = str((gen.data if isinstance(gen.data, dict) else {}).get("title") or "").strip()
    result = {
        "video_id": video.video_id, "title": title,
        "generate_status": "ok" if gen.status == "ok" and not gen.error and title else "error",
        "audit_status": "not_run", "audit_pass": False, "audit_reason": "",
        "generate_usage": gen.usage or {}, "audit_usage": {},
        "latency_ms": int(gen.latency_ms or 0), "error": gen.error,
    }
    if result["generate_status"] != "ok":
        result["error"] = gen.error or "生成响应无效或标题为空"
        return result
    auditor = audit_adapter or adapter
    try:
        audit = auditor.complete(task="baseline_audit", system=AUDIT_SYSTEM, user={"video": payload["video"], "title": title}, schema="audit")
    except Exception as exc:
        result.update(audit_status="error", error=f"审核调用异常：{type(exc).__name__}")
        return result
    result["audit_usage"] = audit.usage or {}
    result["latency_ms"] += int(audit.latency_ms or 0)
    audit_data = audit.data if isinstance(audit.data, dict) else {}
    result["audit_reason"] = str(audit_data.get("reason") or "")
    verdict = audit_data.get("pass")
    if audit.status != "ok" or audit.error or not isinstance(verdict, bool):
        result.update(audit_status="error", error=audit.error or "审核响应无效")
    else:
        result.update(audit_status="passed" if verdict else "rejected", audit_pass=verdict)
    return result
