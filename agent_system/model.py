from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ModelError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True, raw: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        # 解析失败时保留模型原始输出：没有它就无法判断是截断、未转义引号还是结构缺失。
        self.raw = raw


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


def _close_truncated(body: str) -> str:
    """尽力修复被 max_tokens 截断的 JSON：补齐未闭合的字符串/对象/数组。
    只做结构补齐，不改内容；修不好仍由上层重试。"""
    out = body.rstrip()
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in out:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
    if in_str:
        out += '"'
    out = out.rstrip()
    if out.endswith(":"):
        # 截断常停在键的冒号后（模型的 {"subject":）：补 null 而不是删掉冒号，
        # 删掉会得到 {"subject"}，怎么补都无法解析。
        out += " null"
    elif out.endswith(","):
        out = out[:-1]
    out += "".join("}" if c == "{" else "]" for c in reversed(stack))
    return out


def _escape_stray_quotes(body: str) -> str:
    """把字符串值内部未转义的 ASCII 双引号转义掉。

    中文材料里模型常直接写 他说"我不同意" 这种内嵌引号，JSON 就此截断在那一处，
    报错永远是「Expecting ',' delimiter」，位置却离真正坏点很远。
    规则：字符串内部遇到的 " ，只有后面紧跟 , : } ] 或文本结束时才算收尾，否则是内嵌引号。
    """
    out: list[str] = []
    in_str = False
    esc = False
    index = 0
    length = len(body)
    while index < length:
        ch = body[index]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            index += 1
            continue
        if esc:
            out.append(ch)
            esc = False
            index += 1
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            index += 1
            continue
        if ch == '"':
            look = index + 1
            while look < length and body[look] in " \t\r\n":
                look += 1
            if look >= length or body[look] in ",:}]":
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(ch)
        index += 1
    return "".join(out)


# 模型偶尔把结构化输出再包一层信封返回：{"type":"json_object","content":{...}}，
# 或者按 schema 名包一层：{"BoundarySet": {...}}。这层信封对下游毫无意义，
# 不拆掉就会得到「理解节点 0 条 claim」「边界 0 条判定」这种静默零输出。
_ENVELOPE_KEYS = {"type", "content", "data", "result", "response", "output", "json", "schema", "json_object"}
_SCHEMA_WRAPPERS = {"understanding", "boundaryset", "highlightset", "reviewresult", "reviewresultbatch",
                    "generationresult", "candidatebatch", "thesisreview", "verificationresult"}


def _unwrap_envelope(data: Any, depth: int = 0) -> Any:
    if not isinstance(data, dict) or depth > 4:
        return data
    kind = data.get("type")
    if isinstance(kind, str) and kind.lower() == "json_object":
        inner = data.get("content", data.get("data"))
        if isinstance(inner, str):
            try:
                return _unwrap_envelope(loads_json_loose(inner), depth + 1)
            except json.JSONDecodeError:
                return data
        if isinstance(inner, dict):
            return _unwrap_envelope(inner, depth + 1)
    for key, value in data.items():
        if str(key).lower() in _SCHEMA_WRAPPERS and isinstance(value, dict):
            merged = dict(value)
            # 顶层若带同名非空数组，保留它（模型有时把数组放在信封层）
            for array_key in ("claims", "semantic_units", "decisions", "highlights", "candidates", "reviews"):
                if not merged.get(array_key) and isinstance(data.get(array_key), list) and data[array_key]:
                    merged[array_key] = data[array_key]
            return _unwrap_envelope(merged, depth + 1)
    rest = {k: v for k, v in data.items() if k not in _ENVELOPE_KEYS}
    if len(rest) == 1:
        only = next(iter(rest.values()))
        if isinstance(only, dict):
            return _unwrap_envelope(only, depth + 1)
    return data


def loads_json_loose(text: str) -> Any:
    """容忍模型最常见的 JSON 坏法：代码块包裹、前后杂质、尾逗号、行尾多余逗号。

    评审/生成节点的输出坏一次就是一次 10 秒 + 2 万 token 的重跑；宽松解析能救回大部分，
    救不回的由上层带着错误信息重试（那次才是真修复）。失败时把坏位置附近文本带进错误信息，
    方便定位是哪种坏法（未转义引号 / 注释 / 单引号 / 多个对象拼接）。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 去掉 JSON 主体前后的杂质（模型有时在前面写解释、后面留尾巴）
    start, end = raw.find("{"), raw.rfind("}")
    body = raw[start:end + 1] if start != -1 and end > start else raw
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r",(\s*[}\]])", r"\1", body)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # 内嵌未转义引号：中文材料里最常见的一种坏法，报错位置通常不在坏点上。
    quoted = _escape_stray_quotes(repaired)
    try:
        return json.loads(quoted)
    except json.JSONDecodeError:
        pass
    # 输出被 max_tokens 截断时，rfind("}") 会把正文截在最后一个完整对象处，丢掉尾部对象；
    # 所以先用「完整原文」补齐（保留尾部字段），失败才退回被截短的 body。
    try:
        return json.loads(_close_truncated(raw))
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_close_truncated(repaired))
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_close_truncated(quoted))
    except json.JSONDecodeError as exc:
        pos = getattr(exc, "pos", 0) or 0
        snippet = body[max(0, pos - 60):pos + 60]
        raise json.JSONDecodeError(f"{exc.msg} @char {pos} 附近: {snippet}", body, pos) from exc


class MockAdapter:
    """Deterministic adapter used by tests and offline development."""
    def complete(self, *, task: str, system: str, user: dict[str, Any], schema: str, attempt: int = 0) -> ModelReply:
        start = time.monotonic()
        video = user.get("video", {})
        title = video.get("title", "视频内容")
        evidence_ids = [x.get("evidence_id") for x in (video.get("evidence") or user.get("evidence", [])) if x.get("evidence_id")]
        claims = user.get("claims", [])
        if task == "understand":
            # 离线样例只引用原题时，陈述也必须来自原题，不能拿衍生描述冒充。
            text = title
            claims = [{"claim_id": "c1", "text": text[:100], "kind": "observation", "evidence_refs": evidence_ids[:1], "time_refs": []}]
            semantic_units = [{"unit_id": "u1", "meaning": text[:100], "kind": "support", "claim_ids": ["c1"], "evidence_ids": evidence_ids[:1], "time_range": None, "role_in_video": "主旨", "semantic_use": "content_summary", "grounding": "grounded", "corroboration": {"status": "consistent", "notes": "离线演示", "evidence_ids": evidence_ids[:1]}}]
            events = [{"event_id": "e1", "actor": "一方", "action": text[:40], "when": "现场", "evidence_ids": evidence_ids[:1], "confidence": "grounded"}]
            data = {"main_format": video.get("format", "未知"), "attributes": [video.get("domain", {}).get("一级", "未知")], "claims": claims, "semantic_units": semantic_units, "events": events, "turns": [], "event_times": [], "extracted_sources": [], "conflicts": []}
        elif task == "boundary":
            unit_decisions = [{"unit_id": f"u{i+1}", "decision": "usable", "required_limits": [], "forbidden_inferences": [], "attribution_required": False, "reason": "离线临时单元判断"} for i, _ in enumerate(user.get("semantic_units", []))]
            data = {"decisions": [{"claim_id": c.get("claim_id", "c1"), "supported_statement": c.get("text"), "decision": "usable", "evidence_refs": c.get("evidence_refs", []), "required_limits": [], "forbidden_inferences": ["不得添加材料未出现的事实"], "rule_ids": ["B-FACT"], "verification_required": False, "reason": "临时规则下可忠实表达"} for c in claims], "unit_decisions": unit_decisions, "unresolved_issues": []}
        elif task == "highlight":
            decisions = [x for x in user.get("boundary", {}).get("decisions", []) if x.get("decision") != "excluded"]
            if not decisions:
                decisions = [{"claim_id": c.get("claim_id", "c1"), "supported_statement": c.get("text"), "evidence_refs": c.get("evidence_refs", [])} for c in claims]
            semantic_units = user.get("semantic_units", [])
            hook_types = ["information", "suspense", "emotion"]
            highlights = [{
                "highlight_id": f"h{i+1}", "role": "main" if i == 0 else "backup",
                "content": d.get("supported_statement") or d.get("text") or title,
                "viewer_value": "了解视频中的具体内容", "claim_ids": [d.get("claim_id", "c1")],
                "evidence_ids": d.get("evidence_refs", []), "required_qualifiers": [],
                "selection_reason": "离线临时看点提炼",
                "unit_id": (semantic_units[i].get("unit_id") if i < len(semantic_units) else ""),
                "hook_type": hook_types[i % len(hook_types)],
                "relation": "core" if i == 0 else "support",
                "thesis_id": "t1",
            } for i, d in enumerate(decisions[:3])]
            first_unit = semantic_units[0].get("unit_id") if semantic_units else ""
            thesis_contract = {"thesis_id": "t1", "topic": str(title)[:40], "delivery_type": "展示", "core_promise": "了解视频中的具体看点", "required_semantics": [str(title)[:20] or "视频内容"], "core_unit_ids": [first_unit] if first_unit else ["u1"], "evidence_ids": evidence_ids[:1], "qualifiers": [], "pending_questions": []}
            data = {"highlights": highlights, "eliminated": [], "unusable_angles": [], "thesis_contract": thesis_contract}
        elif task == "thesis_review":
            data = {"thesis_contract": {"thesis_id": "t1", "topic": str(title)[:40], "delivery_type": "展示", "core_promise": "了解视频中的具体看点", "required_semantics": [str(title)[:20] or "视频内容"], "core_unit_ids": [], "evidence_ids": evidence_ids[:1], "qualifiers": [], "pending_questions": []}}
        elif task in {"generate", "rewrite"}:
            highlights = user.get("highlights", [{"highlight_id": "h1", "value": title}])
            base = (highlights[0].get("value") if highlights else title) or title
            count = 1 if task == "rewrite" else min(int(user.get("target_count", 3)), 3)
            # 遵守正文预算：平台前缀（剧情演绎/AI生成）会占用 max_chars 的一部分
            budget = int((user.get("display_config") or {}).get("max_chars_body") or 20)
            candidates = []
            slots = ["source_rewrite", "core_summary", "core_hook"]
            for i in range(count):
                h = highlights[i % max(len(highlights), 1)] if highlights else {"highlight_id": "h1", "value": title}
                strategies = ["直接描述", "问题方法", "前后变化"]
                # 重写必须真的换个说法：早期实现固定用 strategies[0]，输出与首轮第一条
                # 逐字相同。判重逻辑上线后那种输出会被正确识别成"没有新标题"直接停，
                # 于是所有依赖"重写产生了候选"的测试都失去了可观察对象。
                # 按父候选 + 轮次区分，保证每条父候选的重写结果互不相同，也不撞首轮三条。
                if task == "rewrite":
                    parent_tail = str(user.get("parent_candidate_id") or "").rsplit("-", 1)[-1] or "1"
                    tail = "问题" + str(max(1, int(user.get("rewrite_round", 0) or 1))) + parent_tail
                else:
                    tail = strategies[i % 3]
                head = (str(h.get("value", base))[: max(0, budget - len(tail))] + tail)[:budget]
                candidates.append({"candidate_id": f"a{int(user.get('rewrite_round', 0))+1}-{i+1}", "headline": head, "highlight_id": h.get("highlight_id", "h1"), "unit_id": h.get("unit_id", ""), "claim_ids": [c.get("claim_id", "c1") for c in claims[:1]], "evidence_ids": h.get("evidence_ids") or h.get("delivery_evidence_refs", []), "expression_strategy": strategies[i % 3], "qualifiers_used": [], "used_case_ids": [], "generation_round": user.get("rewrite_round", 0), "parent_candidate_id": user.get("parent_candidate_id"), "change_reason": "离线演示" if task == "rewrite" else "", "slot_id": (user.get("parent_slot_id") if task == "rewrite" else slots[i % len(slots)]), "thesis_id": (user.get("parent_thesis_id") if task == "rewrite" else "t1")})
            data = {"candidates": candidates, "memory_trace": [], "shortfall_reason": None}
        elif task == "review":
            candidates = user.get("candidate_titles", [])
            # 已批准业务政策存在时，G2 pass 必须引用一条适用规则（与 schemas 校验一致）
            policy_rules = [str(x.get("rule_id")) for x in ((user.get("business_policy") or {}).get("rules") or []) if isinstance(x, dict) and x.get("rule_id")]
            reviews = []
            for c in candidates:
                reviews.append({
                    "candidate_id": c.get("candidate_id"),
                    "expected_takeaway": {"subject": "材料中的主体", "content_nature": "视频内容", "viewing_promise": "了解视频中的具体看点"},
                    "gates": {
                        "G1": {"status": "pass", "reason": "离线临时评审", "evidence_ids": evidence_ids[:1], "rule_ids": []},
                        "G2": {"status": "pass", "reason": "离线临时评审（未触发禁止项）", "evidence_ids": [], "rule_ids": policy_rules[:1]},
                        "G3": {"status": "pass", "reason": "离线临时评审", "evidence_ids": evidence_ids[:1], "rule_ids": [], "delivery_supported": True, "core_covered": True},
                    },
                    "quality": {quality: {"score": 2, "reason": "离线临时评审"} for quality in ("Q1", "Q2", "Q3")},
                    "issues": [], "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
                    "memory_query": {"problem_types": [], "query": ""},
                })
            data = {"reviews": reviews, "rubric_version": "red-dot-review-v2-uncalibrated"}
        elif task == "baseline_generate":
            video = user.get("video", {}) or {}
            base = str(video.get("title") or "视频内容")[:18]
            data = {"title": base}
        elif task == "model_review":
            title = str(user.get("title") or "")
            ok = len(title) <= 20 and "?" not in title and "？" not in title
            data = {"verdict": "可用" if ok else "不可用", "reason": "离线 mock 评审：长度与问号检查"}
        elif task == "baseline_audit":
            title = str(user.get("title") or "")
            ok = len(title) <= 20 and "?" not in title and "？" not in title
            data = {"pass": ok, "reason": "离线 mock 审核：长度与问号检查"}
        elif task == "verify":
            # 离线适配器没有真实核查能力：一律返回 unverifiable，绝不凭空编造反驳证据。
            claim = user.get("claim", {})
            data = {
                "claim_id": claim.get("claim_id", ""),
                "verdict": "unverifiable",
                "sources": [],
                "confidence": 0.0,
                "query": claim.get("text", ""),
            }
        else:
            raise ModelError(f"mock task unsupported: {task}", retryable=False)
        return ModelReply("ok", data, json.dumps(data, ensure_ascii=False), {"prompt_tokens": 0, "completion_tokens": 0}, int((time.monotonic() - start) * 1000))


def _dump_bad_output(task: str, attempt: int, text: str) -> None:
    """把解析失败的原始输出落盘，否则只能说"大概是引号问题"，永远定位不到真坏点。

    只写 runtime/ 下的调试文件，不参与任何判定；写不进去也不影响主流程。
    """
    try:
        target = Path("runtime") / "model_debug"
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (target / f"{stamp}_{task}_a{attempt}.txt").write_text(text or "", encoding="utf-8")
    except Exception:
        pass


class OpenAICompatibleAdapter:
    def __init__(self, base_url: str, api_key_env: str, model: str, timeout: int = 300, reasoning_effort: str = "", structured_output: str = "off") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        # off = 只用 json_object；auto = 有登记 schema 就用 json_schema，网关拒绝则回退；
        # on = 强制用，拒绝即报错。见 RunConfig.structured_output 的说明。
        self.structured_output = structured_output

    def _response_format(self, schema: str) -> dict[str, Any] | None:
        """有登记 schema 且启用结构化输出时返回 json_schema 描述，否则 None。"""
        if self.structured_output == "off":
            return None
        from .review_schema import response_format as build_response_format
        return build_response_format(schema)

    def _post(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    def complete(self, *, task: str, system: str, user: dict[str, Any], schema: str, attempt: int = 0) -> ModelReply:
        key = os.getenv(self.api_key_env)
        if not key:
            raise ModelError(f"missing API key environment variable: {self.api_key_env}", retryable=False)
        structured = self._response_format(schema)
        # 评审要给三个候选各出 3 门 + 3 个质量分的理由，8192 会在中文长理由下被截断；
        # max_tokens 只是上限，不写满不额外计费，所以放宽到 16384。
        payload = {"model": self.model, "temperature": 0.2, "max_tokens": 16384, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}], "response_format": structured or {"type": "json_object"}}
        if self.reasoning_effort == "off":
            # 不同网关的非思考参数不同，不能把 EvoLink 的字段直接用于硅基流动。
            if urllib.parse.urlparse(self.base_url).hostname == "api.siliconflow.cn":
                payload["enable_thinking"] = False
            else:
                payload["thinking"] = {"type": "disabled"}
        elif self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        start = time.monotonic()
        try:
            try:
                body = self._post(payload, key)
            except urllib.error.HTTPError as exc:
                # auto 模式：网关不接受 json_schema 时退回 json_object 重发一次，
                # 否则一次参数不被支持就变成整批失败，比不用结构化输出还糟。
                if structured is not None and self.structured_output == "auto" and exc.code in {400, 404, 422}:
                    payload["response_format"] = {"type": "json_object"}
                    body = self._post(payload, key)
                else:
                    raise
            text = body["choices"][0]["message"]["content"]
            try:
                data = _unwrap_envelope(loads_json_loose(text))
            except json.JSONDecodeError as exc:
                _dump_bad_output(task, attempt, text)
                raise ModelError(str(exc), retryable=True, raw=text) from exc
            return ModelReply("ok", data, text, body.get("usage"), int((time.monotonic() - start) * 1000))
        except urllib.error.HTTPError as exc:
            # Authentication, request-shape and payment/quota failures do not
            # improve by repeating the same call. Network/server failures do.
            retryable = exc.code not in {400, 401, 402, 403, 404, 422}
            raise ModelError(f"HTTP {exc.code}: {exc.reason}", retryable=retryable) from exc
        except ModelError:
            # 解析失败已经带了 raw，再包一层会把断点原文弄丢，重试就没法把错误回给模型。
            raise
        except Exception as exc:
            raise ModelError(str(exc), retryable=True) from exc

