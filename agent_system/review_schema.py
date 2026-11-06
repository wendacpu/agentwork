"""R01 评审结果的 JSON Schema，用于 Structured Outputs 约束解码。

为什么需要它：`response_format={"type": "json_object"}` 只保证「是合法 JSON」，
不保证字段齐全、类型正确、枚举在范围内。于是「结构校验」实际上落在解码之后
手写的 `_review_batch_structure_error` 上——模型可以合法地输出一个缺字段的
JSON，整批评审随之作废，三个候选一起丢。

本模块把同一份约束前移到解码器：让不允许的 token 在采样前就被屏蔽，而不是
生成完再判死。

批量结构检查之外，新评审还逐候选检查 G3 的 `delivery_supported`、
`core_covered` 和 `promise_comparison`，并核验摘录确实来自引用证据。

注意：这份 schema 只约束**结构**。事实是否成立、证据有没有编造、该判 pass 还是
revise，仍然要靠规则与人工，不能因为结构合法就认为语义正确。
"""

from __future__ import annotations

from typing import Any

GATE_STATUS = ("pass", "revise", "needs_review", "reject")
CONSISTENCY_RELATIONS = ("aligned", "detail", "missing_point", "contradiction")
PROMISE_ALIGNMENT = ("matched", "mismatched", "uncertain")
REVIEW_DIMENSIONS = ("G1", "G2", "G3", "Q1", "Q2", "Q3")
ISSUE_SEVERITIES = ("low", "medium", "high", "critical")


def _enum(values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def _gate(*, extended: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": _enum(GATE_STATUS),
        "reason": {"type": "string"},
        "evidence_ids": _strings(),
        "rule_ids": _strings(),
    }
    if extended:
        properties["delivery_supported"] = {"type": "boolean"}
        properties["core_covered"] = {"type": "boolean"}
        properties["promise_comparison"] = {
            "type": "object",
            "properties": {
                "title_promise": {"type": "string"},
                "trigger_span": {"type": "string"},
                "video_delivery": {"type": "string"},
                "evidence_excerpt": {"type": "string"},
                "evidence_answered_question": {"type": "string"},
                "alignment": _enum(PROMISE_ALIGNMENT),
                "explanation": {"type": "string"},
            },
            "required": [
                "title_promise", "trigger_span", "video_delivery",
                "evidence_excerpt", "evidence_answered_question",
                "alignment", "explanation",
            ],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _quality_item() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "enum": [1, 2, 3]},
            "reason": {"type": "string"},
        },
        "required": ["score", "reason"],
        "additionalProperties": False,
    }


def _issue() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "primary_dimension": _enum(REVIEW_DIMENSIONS),
            "issue_code": {"type": "string"},
            "severity": _enum(ISSUE_SEVERITIES),
            "problem_span": {"type": "string"},
            "reason": {"type": "string"},
            "action": {"type": "string"},
            "repairable": {"type": "boolean"},
            "evidence_ids": _strings(),
            "rule_ids": _strings(),
        },
        "required": [
            "primary_dimension", "issue_code", "severity", "problem_span",
            "reason", "action", "repairable", "evidence_ids", "rule_ids",
        ],
        "additionalProperties": False,
    }


def _review() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "expected_takeaway": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content_nature": {"type": "string"},
                    "viewing_promise": {"type": "string"},
                },
                "required": ["subject", "content_nature", "viewing_promise"],
                "additionalProperties": False,
            },
            "gates": {
                "type": "object",
                "properties": {
                    "G1": _gate(extended=False),
                    "G2": _gate(extended=False),
                    "G3": _gate(extended=True),
                },
                "required": ["G1", "G2", "G3"],
                "additionalProperties": False,
            },
            "quality": {
                "type": "object",
                "properties": {
                    "Q1": _quality_item(),
                    "Q2": _quality_item(),
                    "Q3": _quality_item(),
                },
                "required": ["Q1", "Q2", "Q3"],
                "additionalProperties": False,
            },
            "issues": {"type": "array", "items": _issue()},
            "rewrite_guidance": {
                "type": "object",
                "properties": {
                    "preserve": _strings(),
                    "modify": _strings(),
                    "forbidden": _strings(),
                },
                "required": ["preserve", "modify", "forbidden"],
                "additionalProperties": False,
            },
            "memory_query": {
                "type": "object",
                "properties": {
                    "problem_types": _strings(),
                    "query": {"type": "string"},
                },
                "required": ["problem_types", "query"],
                "additionalProperties": False,
            },
            "limit_checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string"},
                        "limit": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["satisfied", "violated", "unclear"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["claim_id", "limit", "verdict", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "candidate_id", "expected_takeaway", "gates", "quality",
            "issues", "rewrite_guidance", "memory_query", "limit_checks",
        ],
        "additionalProperties": False,
    }


def review_result_schema() -> dict[str, Any]:
    """顶层对象：跨契约一致性判定 + 逐候选评审。"""
    return {
        "type": "object",
        "properties": {
            "thesis_consistency": {
                "type": "object",
                "properties": {
                    "relation": _enum(CONSISTENCY_RELATIONS),
                    "reason": {"type": "string"},
                    "contradiction_point": {"type": "string"},
                    "generated_missing": _strings(),
                    "review_missing": _strings(),
                },
                "required": [
                    "relation", "reason", "contradiction_point",
                    "generated_missing", "review_missing",
                ],
                "additionalProperties": False,
            },
            "reviews": {"type": "array", "items": _review()},
            "rubric_version": {"type": "string"},
        },
        "required": ["thesis_consistency", "reviews", "rubric_version"],
        "additionalProperties": False,
    }


def response_format(name: str = "ReviewResultBatch", *, strict: bool = True) -> dict[str, Any] | None:
    """给 chat/completions 的 response_format；未登记的结构名返回 None（退回 json_object）。"""
    if name != "ReviewResultBatch":
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ReviewResultBatch",
            "schema": review_result_schema(),
            "strict": strict,
        },
    }


__all__ = [
    "GATE_STATUS",
    "CONSISTENCY_RELATIONS",
    "PROMISE_ALIGNMENT",
    "REVIEW_DIMENSIONS",
    "ISSUE_SEVERITIES",
    "review_result_schema",
    "response_format",
]
