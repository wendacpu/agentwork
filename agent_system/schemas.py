"""Runtime validation for structured candidate reviews and decisions.

Kept dependency-free so the workflow can validate model JSON before routing.
"""
from __future__ import annotations

from typing import Any

GATE_IDS = ("G1", "G2", "G3")
QUALITY_IDS = ("Q1", "Q2", "Q3")
GATE_STATUSES = {"pass", "revise", "needs_review", "reject"}
SEVERITIES = {"low", "medium", "high", "critical"}


def validate_review(item: dict[str, Any], *, evidence_ids: set[str], rule_ids: set[str], issue_codes: set[str], policy_rule_ids: set[str]) -> list[str]:
    errors: list[str] = [str(x) for x in item.get("_normalization_errors", [])]
    if not item.get("candidate_id"):
        errors.append("candidate_id missing")
    takeaway = item.get("expected_takeaway")
    if not (isinstance(takeaway, str) and takeaway.strip()) and (not isinstance(takeaway, dict) or any(not isinstance(takeaway.get(k), str) for k in ("subject", "content_nature", "viewing_promise"))):
        errors.append("expected_takeaway missing or invalid")
    gates = item.get("gates")
    if not isinstance(gates, dict):
        errors.append("gates missing")
    else:
        for gate_id in GATE_IDS:
            gate = gates.get(gate_id)
            if not isinstance(gate, dict) or gate.get("status") not in GATE_STATUSES:
                errors.append(f"{gate_id} missing or invalid")
                continue
            if not str(gate.get("reason") or "").strip():
                errors.append(f"{gate_id} reason missing")
            gate_evidence = gate.get("evidence_ids", [])
            gate_rules = gate.get("rule_ids", [])
            if not isinstance(gate_evidence, list) or not isinstance(gate_rules, list):
                errors.append(f"{gate_id} evidence_ids/rule_ids must be lists")
                gate_evidence, gate_rules = [], []
            if gate_id in {"G1", "G3"} and gate.get("status") == "pass" and not gate_evidence:
                errors.append(f"{gate_id} pass requires evidence_ids")
            # G2=pass 的语义是「检查过且都没命中」，此时不要求引用具体规则——要求引用会把
            # "没违规"变成结构错误，真实模型大面积返回 pass + 空 rule_ids，全被判 schema 失败。
            # 只有判定违规（reject/revise）时才必须引用到具体规则，否则驳回无从追溯（由程序侧
            # REJECT_WITHOUT_POLICY_RULE 兜底）。
            if gate_id == "G2" and gate.get("status") in {"reject", "revise"} and policy_rule_ids and not gate_rules:
                errors.append("G2 reject/revise requires at least one approved policy rule_id")
            for ref in gate_evidence:
                if ref not in evidence_ids:
                    errors.append(f"{gate_id} invalid evidence_id: {ref}")
            for ref in gate_rules:
                if ref not in rule_ids:
                    errors.append(f"{gate_id} unknown rule_id: {ref}")
    quality = item.get("quality")
    if not isinstance(quality, dict):
        errors.append("quality missing")
    else:
        for quality_id in QUALITY_IDS:
            score = quality.get(quality_id)
            if not isinstance(score, dict):
                errors.append(f"{quality_id} missing")
                continue
            value = score.get("score")
            if value not in (1, 2, 3, None):
                errors.append(f"{quality_id} score must be 1, 2, 3 or null")
            if not str(score.get("reason") or "").strip():
                errors.append(f"{quality_id} reason missing")
    issues = item.get("issues")
    if not isinstance(issues, list):
        errors.append("issues missing or not a list")
    else:
        seen: set[tuple[str, str, str]] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                errors.append("issue must be an object")
                continue
            key = (str(issue.get("primary_dimension")), str(issue.get("issue_code")), str(issue.get("problem_span")))
            if key in seen:
                errors.append(f"duplicate issue: {key[1]}")
            seen.add(key)
            if not issue.get("issue_id") or not str(issue.get("problem_span") or "").strip() or not str(issue.get("reason") or "").strip() or not str(issue.get("action") or "").strip():
                errors.append("issue required fields missing")
            if issue.get("primary_dimension") not in {*GATE_IDS, *QUALITY_IDS}:
                errors.append("issue primary_dimension invalid")
            if not issue.get("issue_code") or issue.get("severity") not in SEVERITIES:
                errors.append("issue code or severity invalid")
            elif issue.get("issue_code") not in issue_codes:
                errors.append(f"unknown issue_code: {issue.get('issue_code')}")
            elif str(issue.get("issue_code")).split("_", 1)[0] != str(issue.get("primary_dimension")):
                errors.append("issue primary_dimension must match issue_code prefix")
            if not isinstance(issue.get("repairable"), bool):
                errors.append("issue repairable missing")
            issue_evidence = issue.get("evidence_ids", [])
            issue_rules = issue.get("rule_ids", [])
            if not isinstance(issue_evidence, list) or not isinstance(issue_rules, list):
                errors.append("issue evidence_ids/rule_ids must be lists")
                issue_evidence, issue_rules = [], []
            for ref in issue_evidence:
                if ref not in evidence_ids:
                    errors.append(f"issue invalid evidence_id: {ref}")
            for ref in issue_rules:
                if ref not in rule_ids:
                    errors.append(f"issue unknown rule_id: {ref}")
    guidance = item.get("rewrite_guidance")
    if not isinstance(guidance, dict) or any(not isinstance(guidance.get(k), list) for k in ("preserve", "modify", "forbidden")):
        errors.append("rewrite_guidance missing or invalid")
    memory_query = item.get("memory_query")
    if not isinstance(memory_query, dict) or not isinstance(memory_query.get("problem_types"), list) or not isinstance(memory_query.get("query"), str):
        errors.append("memory_query missing or invalid")
    return errors
