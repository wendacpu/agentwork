from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Protocol


class ModelError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ModelReply:
    status: str
    data: dict[str, Any] | None
    raw_text: str = ""
    usage: dict[str, Any] | None = None
    latency_ms: int = 0
    error: str | None = None


class ModelAdapter(Protocol):
    def complete(self, *, task: str, system: str, user: dict[str, Any], schema: str, attempt: int = 0) -> ModelReply: ...


class MockAdapter:
    """Deterministic adapter used by tests and offline development."""
    def complete(self, *, task: str, system: str, user: dict[str, Any], schema: str, attempt: int = 0) -> ModelReply:
        start = time.monotonic()
        video = user.get("video", {})
        title = video.get("title", "视频内容")
        evidence_ids = [x.get("evidence_id") for x in (video.get("evidence") or user.get("evidence", [])) if x.get("evidence_id")]
        claims = user.get("claims", [])
        if task == "understand":
            text = video.get("description") or title
            claims = [{"claim_id": "c1", "text": text[:100], "kind": "observation", "evidence_refs": evidence_ids[:1], "time_refs": []}]
            semantic_units = [{"unit_id": "u1", "meaning": text[:100], "kind": "support", "claim_ids": ["c1"], "evidence_ids": evidence_ids[:1], "time_range": None, "role_in_video": "主旨", "corroboration": {"source": "description", "status": "consistent", "notes": "离线演示", "evidence_ids": evidence_ids[:1]}}]
            data = {"main_format": video.get("format", "未知"), "attributes": [video.get("domain", {}).get("一级", "未知")], "claims": claims, "semantic_units": semantic_units, "event_times": [], "extracted_sources": [], "conflicts": []}
        elif task == "boundary":
            unit_decisions = [{"unit_id": f"u{i+1}", "decision": "usable", "required_limits": [], "forbidden_inferences": [], "attribution_required": False, "reason": "离线临时单元判断"} for i, _ in enumerate(user.get("semantic_units", []))]
            data = {"decisions": [{"claim_id": c.get("claim_id", "c1"), "supported_statement": c.get("text"), "decision": "usable", "evidence_refs": c.get("evidence_refs", []), "required_limits": [], "forbidden_inferences": ["不得添加材料未出现的事实"], "rule_ids": ["B-FACT"], "verification_required": False, "reason": "临时规则下可忠实表达"} for c in claims], "unit_decisions": unit_decisions, "unresolved_issues": []}
        elif task == "highlight":
            decisions = [x for x in user.get("boundary", {}).get("decisions", []) if x.get("decision") != "excluded"]
            if not decisions:
                decisions = [{"claim_id": c.get("claim_id", "c1"), "supported_statement": c.get("text"), "evidence_refs": c.get("evidence_refs", [])} for c in claims]
            semantic_units = user.get("semantic_units", [])
            highlights = [{
                "highlight_id": f"h{i+1}", "role": "main" if i == 0 else "backup",
                "content": d.get("supported_statement") or d.get("text") or title,
                "viewer_value": "了解视频中的具体内容", "claim_ids": [d.get("claim_id", "c1")],
                "evidence_ids": d.get("evidence_refs", []), "required_qualifiers": [],
                "selection_reason": "离线临时看点提炼",
                "unit_id": (semantic_units[i].get("unit_id") if i < len(semantic_units) else ""),
            } for i, d in enumerate(decisions[:3])]
            data = {"highlights": highlights, "eliminated": [], "unusable_angles": []}
        elif task in {"generate", "rewrite"}:
            highlights = user.get("highlights", [{"highlight_id": "h1", "value": title}])
            base = (highlights[0].get("value") if highlights else title) or title
            suffix = "｜视频看点" if task == "generate" else "｜重新聚焦"
            count = 1 if task == "rewrite" else min(int(user.get("target_count", 3)), 3)
            candidates = []
            for i in range(count):
                h = highlights[i % max(len(highlights), 1)] if highlights else {"highlight_id": "h1", "value": title}
                strategies = ["直接描述", "问题方法", "前后变化"]
                candidates.append({"candidate_id": f"a{int(user.get('rewrite_round', 0))+1}-{i+1}", "headline": (str(h.get("value", base))[:12] + strategies[i % 3])[:20], "highlight_id": h.get("highlight_id", "h1"), "unit_id": h.get("unit_id", ""), "claim_ids": [c.get("claim_id", "c1") for c in claims[:1]], "evidence_ids": h.get("evidence_ids") or h.get("delivery_evidence_refs", []), "expression_strategy": strategies[i % 3], "qualifiers_used": [], "used_case_ids": [], "generation_round": user.get("rewrite_round", 0), "parent_candidate_id": user.get("parent_candidate_id"), "change_reason": "离线演示" if task == "rewrite" else ""})
            data = {"candidates": candidates, "memory_trace": [], "shortfall_reason": None}
        elif task == "review":
            candidates = user.get("candidate_titles", [])
            reviews = []
            for c in candidates:
                reviews.append({
                    "candidate_id": c.get("candidate_id"),
                    "expected_takeaway": {"subject": "材料中的主体", "content_nature": "视频内容", "viewing_promise": "了解视频中的具体看点"},
                    "gates": {
                        "G1": {"status": "pass", "reason": "离线临时评审", "evidence_ids": evidence_ids[:1], "rule_ids": []},
                        "G2": {"status": "pass", "reason": "离线临时评审", "evidence_ids": [], "rule_ids": []},
                        "G3": {"status": "pass", "reason": "离线临时评审", "evidence_ids": evidence_ids[:1], "rule_ids": []},
                    },
                    "quality": {quality: {"score": 2, "reason": "离线临时评审"} for quality in ("Q1", "Q2", "Q3")},
                    "issues": [], "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
                    "memory_query": {"problem_types": [], "query": ""},
                })
            data = {"reviews": reviews, "rubric_version": "red-dot-review-v2-uncalibrated"}
        else:
            raise ModelError(f"mock task unsupported: {task}", retryable=False)
        return ModelReply("ok", data, json.dumps(data, ensure_ascii=False), {"prompt_tokens": 0, "completion_tokens": 0}, int((time.monotonic() - start) * 1000))


class OpenAICompatibleAdapter:
    def __init__(self, base_url: str, api_key_env: str, model: str, timeout: int = 300, reasoning_effort: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort

    def complete(self, *, task: str, system: str, user: dict[str, Any], schema: str, attempt: int = 0) -> ModelReply:
        key = os.getenv(self.api_key_env)
        if not key:
            raise ModelError(f"missing API key environment variable: {self.api_key_env}", retryable=False)
        payload = {"model": self.model, "temperature": 0.2, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}], "response_format": {"type": "json_object"}}
        if self.reasoning_effort == "off":
            # DeepSeek V4.1 默认开思考；关掉可把单节点从 ~68s 压到十几秒，代价是推理质量下降
            payload["thinking"] = {"type": "disabled"}
        elif self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
            text = body["choices"][0]["message"]["content"]
            return ModelReply("ok", json.loads(text), text, body.get("usage"), int((time.monotonic() - start) * 1000))
        except urllib.error.HTTPError as exc:
            # Authentication, request-shape and payment/quota failures do not
            # improve by repeating the same call. Network/server failures do.
            retryable = exc.code not in {400, 401, 402, 403, 404, 422}
            raise ModelError(f"HTTP {exc.code}: {exc.reason}", retryable=retryable) from exc
        except Exception as exc:
            raise ModelError(str(exc), retryable=True) from exc

