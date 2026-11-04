"""两个验收复现脚本的正式回归：确定性安全契约（全内存，不联网、不读凭据）。

来源（只读参考，未修改）：
- runtime/acceptance_deepseek_20261101_114430/reproduce_generation_gaps.py
- runtime/acceptance_deepseek_20261101_114430/reproduce_review_gaps.py

分工：
- 已在当前源码修复的行为 => 正常断言（防止回退）。
- 仍可复现的缺陷 => pytest.mark.xfail(strict=True)，docstring 写明缺陷与证据位置。

全内存约定：SQLite :memory:、CSV StringIO、无 socket 的替身 HTTP handler、
回放式模型桩；不创建数据库文件、不监听端口、不调用模型/网络、不读 .secrets.env。
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import runpy
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_system import debugger, goldsheet
from agent_system.config import RunConfig
from agent_system.pipeline import Pipeline, _structural_candidates
from agent_system.storage import Storage
from agent_system.thesis import RELATIONS, SLOTS, normalize_thesis_contract, validate_highlight_alignment
from agent_system.types import (
    BoundarySet,
    Claim,
    ClaimDecision,
    EventNode,
    EventTurn,
    Evidence,
    SemanticUnit,
    Understanding,
    VideoInput,
    to_dict,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 生成链路回放桩（照 reproduce_generation_gaps.py，补齐当前调用图里的 thesis_review）
# ---------------------------------------------------------------------------


class _RAMStorage:
    """只留内存状态，不落盘。"""

    def __init__(self):
        self.final_state = None
        self.status = None
        self.event_log = []

    def set_state(self, task, status, state):
        self.status, self.final_state = status, state

    def event(self, task, node, event_type, payload=None):
        self.event_log.append((node, event_type, payload))

    def checkpoint(self, task, status, state, *args, **kwargs):
        self.set_state(task, status, state)

    def events(self, *args):
        return []

    def save_candidates(self, *args):
        pass

    def save_reviews(self, *args):
        pass


def _review_payload(candidate_id: str, critical_issue: bool = False) -> dict:
    result = {
        "candidate_id": candidate_id,
        "expected_takeaway": {"subject": "一方", "content_nature": "事件", "viewing_promise": "看事件"},
        "gates": {
            gate: {"status": "pass", "reason": "已核对", "evidence_ids": ["raw"] if gate in {"G1", "G3"} else [], "rule_ids": []}
            for gate in ("G1", "G2", "G3")
        },
        "quality": {q: {"score": 2, "reason": "基本合格"} for q in ("Q1", "Q2", "Q3")},
        "issues": [],
        "rewrite_guidance": {"preserve": [], "modify": [], "forbidden": []},
        "memory_query": {"problem_types": [], "query": ""},
    }
    if critical_issue:
        result["issues"] = [{
            "issue_id": "i1", "primary_dimension": "G1", "issue_code": "G1_UNSUPPORTED_FACT",
            "severity": "critical", "repairable": False, "problem_span": "小店开业",
            "evidence_ids": ["raw"], "rule_ids": [], "reason": "这件事没有证据", "action": "不可使用",
        }]
    return result


def _thesis_contract(core_unit_ids, thesis_id: str = "t1", topic: str = "小店开业") -> dict:
    return {
        "thesis_id": thesis_id, "topic": topic, "delivery_type": "故事", "core_promise": topic,
        "required_semantics": [topic], "core_unit_ids": list(core_unit_ids), "evidence_ids": ["raw"],
    }


def _video(case: str) -> VideoInput:
    evidence = [
        Evidence("raw", "asr", "小店开业", quality="raw_asr"),
        Evidence("desc", "description", "一方迟到，另一方送饭", quality="model_derived_uncalibrated"),
    ]
    if case == "rewrite_pending_signal":
        evidence.append(Evidence("caption", "caption", "本视频为AI生成", quality="model_derived_uncalibrated"))
    if case == "source_only":
        # 只有原题可用：这条陈述可读，但不能当作视频内容已被核实。
        evidence.append(Evidence("src", "title", "小店开业", quality="source"))
    return VideoInput(
        "offline", "小店开业", "用户", None, "", {"一级": "生活"}, "现场记录",
        None, 10, {}, "", "本视频为AI生成" if case == "rewrite_pending_signal" else "",
        [{"text": "小店开业"}], [], evidence, {}, {},
    )


def run_generation_case(case: str, highlight_thesis_id: str = "t1") -> dict:
    """跑真实 Pipeline._execute，模型调用用手写 JSON 回放；返回落库状态快照。"""
    video = _video(case)
    claims = [{"claim_id": "c1", "text": "小店开业", "claim_type": "observable", "evidence_refs": ["raw"]}]
    units = [{"unit_id": "u1", "meaning": "小店开业", "kind": "thesis", "role_in_video": "主旨",
              "grounding": "grounded", "claim_ids": ["c1"], "evidence_ids": ["raw"]}]
    decisions = [{"claim_id": "c1", "decision": "usable", "supported_statement": "小店开业", "evidence_refs": ["raw"]}]
    unit_decisions = [{"unit_id": "u1", "decision": "excluded" if case == "excluded_unit" else "usable"}]
    understand = {"claims": claims, "semantic_units": units, "attributes": []}
    headline, candidate_claims, unit_id, core_units = "小店开业", ["c1"], "u1", ["u1"]
    if case == "source_only":
        claims[0]["evidence_refs"] = ["src"]
        units[0]["evidence_ids"] = ["src"]
        decisions[0]["evidence_refs"] = ["src"]
        evidence_ref = "src"
    else:
        evidence_ref = "raw"

    if case == "structure":
        claims.append({"claim_id": "c_bad", "text": "一方迟到", "claim_type": "observable", "evidence_refs": ["desc"]})
        decisions.append({"claim_id": "c_bad", "decision": "excluded", "evidence_refs": ["desc"]})
        units[0].update(meaning="迟到反被送饭", claim_ids=["c1", "c_bad"], evidence_ids=["raw", "desc"])
        understand.update(
            events=[
                {"event_id": "ev1", "actor": "一方", "action": "迟到", "confidence": "speculative", "evidence_ids": ["desc"]},
                {"event_id": "ev2", "actor": "另一方", "action": "送饭", "confidence": "speculative", "evidence_ids": ["raw"]},
            ],
            turns=[{"from_event": "ev1", "to_event": "ev2", "type": "contrast", "expectation": "本应生气", "note": "一方迟到反被送饭"}],
        )
        unit_decisions[0]["decision"] = "excluded"
        headline, unit_id, core_units = "一方迟到反被送饭", "turn-1", []
    elif case == "pending_claim":
        claims.append({"claim_id": "c2", "text": "小店开业", "claim_type": "observable", "evidence_refs": ["raw"]})
        decisions.append({"claim_id": "c2", "decision": "usable", "verification_required": True, "evidence_refs": ["raw"]})
        units[0]["claim_ids"] = ["c1", "c2"]
        candidate_claims = ["c2"]
    elif case == "no_attribution":
        decisions[0].update(attribution_required=True, required_source_phrase="据店主介绍")
    elif case == "visual_only":
        # C22：只引用模型画面描述的陈述，界面上看起来「可用」，程序必须降到待核。
        claims[0]["evidence_refs"] = ["desc"]
        units[0]["evidence_ids"] = ["desc"]
        decisions[0]["evidence_refs"] = ["desc"]
        evidence_ref = "desc"

    elif case == "dangling_unit_ref":
        # 单元引用了模型从未给出的 claim：只该剪掉这一处引用，不该让整条视频零产出。
        units[0]["claim_ids"] = list(units[0].get("claim_ids") or []) + ["c99", "c100"]

    elif case == "optimistic_label":
        # 模型自报乐观：只有画面描述的单元，却标成可观察事实 + 已证实。
        # 陈述本身引用 ASR（机械检查能过），标签由程序改掉后才该被拦在自动交付外。
        units[0]["evidence_ids"] = ["desc"]
        units[0]["semantic_use"] = "observable_fact"
        units[0]["grounding"] = "grounded"

    calls, child_routes = [], []

    def replay(task, node, name, user, schema, *args, **kwargs):
        calls.append(name)
        if name == "understand":
            return deepcopy(understand)
        if name == "boundary":
            if case == "grouped_boundary":
                # 模型按决策分组给 ID，而不是逐条给决策对象：曾经被读成 0 条 → NO_USABLE_CLAIM
                grouped = {"claims_total": len(decisions),
                           "usable_claims": [d["claim_id"] for d in decisions if d.get("decision") == "usable"],
                           "limited_claims": [d["claim_id"] for d in decisions if d.get("decision") == "limited"],
                           "excluded_claims": [d["claim_id"] for d in decisions if d.get("decision") == "excluded"],
                           "unit_decisions": {u["unit_id"]: u["decision"] for u in deepcopy(unit_decisions)}}
                return grouped
            return {"decisions": deepcopy(decisions), "unit_decisions": deepcopy(unit_decisions)}
        if name == "highlight":
            if case == "structure":
                return {"highlights": [], "abandon": True, "reason": "所有单元不可用"}
            return {
                "thesis_contract": _thesis_contract(core_units),
                "highlights": [{
                    "highlight_id": "h1", "unit_id": unit_id, "claim_ids": ["c1"], "evidence_ids": [evidence_ref],
                    "value": headline, "role": "main", "relation": "core", "thesis_id": highlight_thesis_id,
                }],
            }
        if name == "thesis_review":
            return {"thesis_contract": _thesis_contract(core_units)}
        if name in {"generate", "rewrite"}:
            text = headline
            if case == "rewrite_pending_signal" and name == "generate":
                text = "这是一条为了触发机械长度检查而刻意超过二十个字符的标题"
            return {"candidates": [{
                "candidate_id": "a2" if name == "rewrite" else "a1",
                "headline": text, "highlight_id": "h1", "unit_id": unit_id,
                "claim_ids": candidate_claims, "evidence_ids": [evidence_ref], "strategy": "直接描述",
                "thesis_id": "t1", "slot_id": "source_rewrite",
            }]}
        if name == "review":
            result = [_review_payload(c["candidate_id"], case == "issue_vs_pass") for c in user["candidate_titles"]]
            if case == "policy_overrides_unknown":
                for item in result:
                    item["gates"]["G1"]["status"] = "needs_review"
                    item["gates"]["G2"].update(status="revise", rule_ids=["P1"])
            return {"reviews": result}
        raise AssertionError(name)

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(memory_call_limit=0, max_rewrite_rounds=1 if case == "rewrite_pending_signal" else 0)
    pipeline.storage = _RAMStorage()
    pipeline.review_adapter = None
    pipeline._call = replay
    actual = pipeline._generate_and_review

    def traced(*args, **kwargs):
        result = actual(*args, **kwargs)
        child_routes.append(deepcopy(result["route"]))
        return result

    pipeline._generate_and_review = traced
    state = {
        "video": to_dict(video), "input_quality": {"level": "high"}, "prompt_hashes": {},
        "reviews": [], "candidates": [],
        "business_policy": {"status": "approved", "version": "v1", "rules": [{"rule_id": "P1", "action": "rewrite"}]},
    }
    pipeline._execute("offline", video, state)
    final = pipeline.storage.final_state
    return {
        "status": pipeline.storage.status,
        "error": final.get("error"),
        "final": final,
        "route": final.get("route") or {},
        "candidates": final.get("candidates") or [],
        "reviews": final.get("reviews") or [],
        "decisions": final.get("candidate_decisions") or [],
        "thesis_errors": final.get("thesis_contract_errors") or [],
        "alignment_errors": final.get("thesis_alignment_errors") or [],
        "pending_signals": (final.get("risk_signals") or {}).get("pending_signal_ids") or [],
        "structural_fallback": final.get("structural_fallback"),
        "inner_routes": [r.get("action") for r in child_routes],
        "replayed": calls,
    }


def _mechanical_codes(candidate: dict) -> list[str]:
    return [str(e.get("code")) for e in (candidate.get("mechanical_checks") or {}).get("errors", [])]


# ---------------------------------------------------------------------------
# 生成链路：已修复行为（正常断言）
# ---------------------------------------------------------------------------


def test_pending_verification_claim_cannot_enter_mechanical_whitelist():
    """已修复：verification_required 的 claim 不得进机械白名单。

    证据：agent_system/pipeline.py 的 _generate_and_review 里
    allowed_claims = {... if d.decision != "excluded" and not d.verification_required}。
    回归点：只有 c1 正常、c2 待核验，候选只引用 c2 时不得 handoff。

    2026-11-04 契约更新：引用真实存在的待核验 claim 不再是 G4_INVALID_REFERENCE
    （V 集人工评审 6 条这种引用 5 条判可用——「绑哪条 claim」是元数据，标题是否把
    说法写成断言才是语义问题）。改为登记 blocked_claim_refs + G1 压 needs_review
    交人工；安全语义不变（不得自动交付），代价从重写循环（烧 2 次调用）降为人工核实。
    """
    result = run_generation_case("pending_claim")
    assert result["error"] is None, result["error"]
    candidate = result["candidates"][0]
    checks = candidate.get("mechanical_checks") or {}
    assert "G4_INVALID_REFERENCE" not in {e["code"] for e in checks.get("errors", [])}
    assert checks.get("blocked_claim_refs") == ["c2"], checks
    assert result["route"].get("action") != "handoff", result["route"]
    g1 = ((result.get("reviews") or [{}])[0].get("gates") or {}).get("G1") or {}
    assert g1.get("status") == "needs_review", g1
    assert g1.get("reason") == "标题绑定了暂缓核验的陈述（c2），需人工确认是否写成断言"


def test_fully_excluded_units_are_not_revived_by_empty_allowed_set():
    """已修复：显式全排除是闭合结果，不得因 allowed_units 为空而复活全部单元。

    证据：agent_system/pipeline.py 里 if boundary.unit_decisions: ... else: 全部单元。
    回归点：唯一单元 u1 被判 excluded，候选仍引用 u1 时必须机械失败、不得 handoff。
    """
    result = run_generation_case("excluded_unit")
    assert result["error"] is None, result["error"]
    candidate = result["candidates"][0]
    assert "G4_INVALID_REFERENCE" in _mechanical_codes(candidate), candidate.get("mechanical_checks")
    assert result["route"].get("action") != "handoff", result["route"]


def test_structural_fallback_requires_allowed_claims_on_both_endpoints():
    """已修复：结构断言两端必须各自有「被允许」的 claim，不能用一端证据覆盖另一端。

    证据：agent_system/pipeline.py 的 _structural_candidates 中
    before_claims / after_claims 都要非空才产出 turn。
    """
    understanding = Understanding(
        main_format="story", attributes=[], event_times=[], extracted_sources=[],
        claims=[
            Claim("c1", "小店开业", "observable", ["raw"]),
            Claim("c_bad", "一方迟到", "observable", ["desc"]),
        ],
        semantic_units=[SemanticUnit("u1", "迟到反被送饭", ["c1", "c_bad"], ["raw", "desc"])],
        events=[EventNode("ev1", "一方", "迟到", evidence_ids=["desc"], confidence="speculative"),
                EventNode("ev2", "另一方", "送饭", evidence_ids=["raw"], confidence="speculative")],
        turns=[EventTurn("ev1", "ev2", "contrast", note="一方迟到反被送饭", expectation="本应生气")],
    )
    boundary = BoundarySet(decisions=[
        ClaimDecision("c1", "小店开业", "usable", ["raw"], [], [], [], "有据"),
        ClaimDecision("c_bad", "一方迟到", "excluded", ["desc"], [], [], [], "派生描述无一手证据"),
    ])
    structures = _structural_candidates(understanding, boundary)
    assert [s["struct_id"] for s in structures] == [], structures


def test_rewrite_subroute_escalation_is_not_dropped():
    """已修复：重写子结果把内容性质待审改成 needs_review 后，外层不得重新聚合回 handoff。

    证据：agent_system/pipeline.py 的 escalations 分支（SUBROUTE_ESCALATION）。
    """
    result = run_generation_case("rewrite_pending_signal")
    assert result["error"] is None, result["error"]
    assert "needs_review" in result["inner_routes"], result["inner_routes"]
    route = result["route"]
    assert route.get("action") == "needs_review", route
    assert route.get("pending_signal_ids"), route


def test_critical_issue_conflicting_with_pass_gate_cannot_handoff():
    """已修复：Gate=pass 与同维度 high/critical issue 矛盾时，schema 校验必须拦住。

    证据：agent_system/schemas.py 的矛盾校验（pass conflicts with high/critical issue(s)）。
    """
    result = run_generation_case("issue_vs_pass")
    assert result["error"] is None, result["error"]
    assert result["route"].get("action") != "handoff", result["route"]
    errors = [e for review in result["reviews"] for e in (review.get("validation_errors") or [])]
    assert any("conflict" in str(e) for e in errors), errors


def test_unresolved_core_fact_beats_policy_rewrite():
    """已修复：G1 未决必须人审，不能被业务政策 action=rewrite 抢先降级为自动重写。

    证据：agent_system/pipeline.py 的 _decide_candidate 中 needs_review 分支在
    policy_action == "rewrite" 之前。
    """
    result = run_generation_case("policy_overrides_unknown")
    assert result["error"] is None, result["error"]
    decision = result["decisions"][0]
    assert decision.get("status") == "needs_review", decision
    assert decision.get("reason") != "POLICY_ACTION_REWRITE", decision
    assert result["route"].get("action") != "handoff", result["route"]


def test_attribution_required_flag_blocks_title_without_source_relation():
    """已修复：attribution_required 时标题必须出现归属表达，不再只查「通报」特例。

    证据：agent_system/pipeline.py 的 _has_explicit_source_relation 与 _ATTRIBUTION_MARKERS，
    以及 _generate_and_review 里的 programmatic_g1_flags。
    """
    result = run_generation_case("no_attribution")
    assert result["error"] is None, result["error"]
    candidate = result["candidates"][0]
    flags = [str(f.get("code")) for f in (candidate.get("programmatic_g1_flags") or [])]
    assert "G1_SOURCE_ATTRIBUTION_MISSING" in flags, candidate.get("programmatic_g1_flags")
    assert result["route"].get("action") != "handoff", result["route"]


# ---------------------------------------------------------------------------
# 主旨契约（thesis.py）：已实现的结构校验
# ---------------------------------------------------------------------------


def test_thesis_contract_normalization_reports_missing_fields_and_excluded_core_unit():
    contract, errors = normalize_thesis_contract({"thesis_contract": {
        "thesis_id": "t1", "topic": "小店开业", "delivery_type": "故事",
        "core_promise": "小店开业", "required_semantics": [],
        "core_unit_ids": ["u1"],
    }}, {"semantic_units": [{"unit_id": "u1"}]}, {"unit_decisions": [{"unit_id": "u1", "decision": "excluded"}]})
    assert contract["thesis_id"] == "t1"
    assert any("required_semantics" in e for e in errors), errors
    assert any("已排除单元" in e for e in errors), errors


def test_thesis_contract_rejects_dangling_core_unit_and_empty_contract():
    _, errors = normalize_thesis_contract(
        {"thesis_contract": _thesis_contract(["u-missing"])},
        {"semantic_units": [{"unit_id": "u1"}]},
        {"unit_decisions": [{"unit_id": "u1", "decision": "usable"}]},
    )
    assert any("不存在的单元" in e for e in errors), errors
    _, empty_errors = normalize_thesis_contract({}, {"semantic_units": []}, {"unit_decisions": []})
    assert any("缺少字段" in e for e in empty_errors), empty_errors
    assert any("core_unit_ids 不能为空" in e for e in empty_errors), empty_errors


def test_highlight_alignment_requires_core_main_and_same_thesis():
    contract = _thesis_contract(["u1"])
    errors = validate_highlight_alignment(
        [{"highlight_id": "h1", "role": "main", "relation": "support", "thesis_id": "t1"}], contract)
    assert any("relation 必须是 core" in e for e in errors), errors
    mismatch = validate_highlight_alignment(
        [{"highlight_id": "h1", "role": "main", "relation": "core", "thesis_id": "t9"}], contract)
    assert any("不一致" in e for e in mismatch), mismatch
    assert RELATIONS == ("core", "support", "peripheral")
    assert SLOTS == ("source_rewrite", "core_summary", "core_hook")


def test_pipeline_detects_highlight_thesis_mismatch():
    """缺陷：pipeline 在调用对齐校验前把每条看点的 thesis_id 覆盖成主旨卡的 ID，
    于是 validate_highlight_alignment 里「主看点 thesis_id 与主旨卡不一致」的分支
    永远不可能触发——守卫被自己写死。

    证据：agent_system/pipeline.py 的 _generate_and_review：
        for h in highlights: h.thesis_id = thesis_contract.get("thesis_id", "")
        紧接 validate_highlight_alignment(...)；
    agent_system/thesis.py：
        if thesis_id and main.get("thesis_id") and main.get("thesis_id") != thesis_id:

    复现：看点自报 thesis_id="t9"、主旨卡 thesis_id="t1"，
    state["thesis_alignment_errors"] 应为非空，实际为空。
    """
    result = run_generation_case("pending_claim", highlight_thesis_id="t9")
    assert result["error"] is None, result["error"]
    assert result["alignment_errors"], result["final"].get("thesis_alignment_errors")


# ---------------------------------------------------------------------------
# 重新评审：机械白名单与初次生成必须一致
# ---------------------------------------------------------------------------


class _TaskStorage:
    """rereview 只需要 get_task / set_state / save_reviews / event。"""

    def __init__(self, state: dict):
        self._state = state
        self.final = None
        self.status = None
        self.events = []

    def get_task(self, task_id):
        return {"task_id": task_id, "video_id": "v", "status": "pending_human",
                "state_json": json.dumps(self._state, ensure_ascii=False)}

    def set_state(self, task, status, state):
        self.status, self.final = status, state

    def save_reviews(self, *args):
        pass

    def event(self, *args, **kwargs):
        self.events.append(args)


class _OneVideoDataset:
    def get(self, video_id):
        return VideoInput("v", "小店开业", "用户", None, "", {"一级": "生活"}, "现场记录", None, 10, {},
                          "", "", [{"text": "小店开业"}], [],
                          [Evidence("raw", "asr", "小店开业", quality="raw_asr")], {}, {})


def _rereview_state() -> dict:
    return {
        "video": to_dict(_OneVideoDataset().get("v")),
        "rules": {"version": "r"},
        "understanding": {
            "claims": [
                {"claim_id": "c1", "text": "小店开业", "claim_type": "observable", "evidence_refs": ["raw"]},
            ],
            "semantic_units": [{"unit_id": "u1", "meaning": "小店开业", "claim_ids": ["c1"], "evidence_ids": ["raw"]}],
            "events": [], "turns": [],
        },
        "boundary": {
            "decisions": [{"claim_id": "c1", "decision": "usable", "evidence_refs": ["raw"]}],
            "unit_decisions": [{"unit_id": "u1", "decision": "excluded"}],
        },
        "highlights": [{"highlight_id": "h1"}],
        "risk_signals": {}, "required_prefixes": [], "prompt_hashes": {},
        "candidates": [{"candidate_id": "a1", "headline": "小店开业", "claim_ids": ["c1"],
                        "evidence_ids": ["raw"], "highlight_id": "h1", "unit_id": "u1"}],
        "business_policy": {"status": "approved", "version": "v1", "rules": []},
    }


def _run_rereview(tmp_path) -> dict:
    storage = Storage(tmp_path / "original.db")
    task_id = storage.create_task("v", {})
    storage.set_state(task_id, "pending_human", _rereview_state())
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.dataset = _OneVideoDataset()
    pipeline.review_adapter = None
    pipeline._call = lambda *args, **kwargs: {"reviews": [_review_payload("a1")]}
    try:
        isolated_path = pipeline.rereview(task_id)
        isolated = Storage(isolated_path)
        try:
            return json.loads(isolated.get_task(task_id)["state_json"])
        finally:
            isolated.conn.close()
    finally:
        storage.conn.close()


def test_rereview_enforces_excluded_units(tmp_path):
    """缺陷：rereview 的机械检查没有传 allowed_units，与初次生成（传了 allowed_units）口径不一致：
    候选引用已排除单元 u1 时，初次生成判 G4_INVALID_REFERENCE，重评却 pass。

    证据：agent_system/pipeline.py 的 rereview 里 _mechanical_check(...) 只传
    allowed_claims / allowed_highlights / evidence_ids / max_chars / required_prefixes /
    claim_decisions，缺 allowed_units=...；对照 _generate_and_review 的同名调用。
    """
    final = _run_rereview(tmp_path)
    candidate = final["candidates"][0]
    assert "G4_INVALID_REFERENCE" in _mechanical_codes(candidate), candidate.get("mechanical_checks")


def test_rereview_isolates_all_writes_and_preserves_frozen_identity(tmp_path):
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("v", {})
    storage.set_state(task_id, "pending_human", _rereview_state())
    storage.save_human(task_id, {"candidate_id": "a1", "decision": "rejected"})
    before = list(storage.conn.iterdump())
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.review_adapter = None
    # 在真实 _call 的写入位置模拟事件，确保使用隔离实例而非原实例。
    from unittest.mock import patch as mock_patch
    def review_call(self, *args, **kwargs):
        self.storage.event(task_id, "R01", "test_model_call", {})
        return {"reviews": [_review_payload("a1")]}
    try:
        with mock_patch.object(Pipeline, "_call", review_call):
            path = pipeline.rereview(task_id)
        assert list(storage.conn.iterdump()) == before
        isolated = Storage(path)
        try:
            snapshot = json.loads(isolated.conn.execute("SELECT candidates_json FROM rereview_snapshot").fetchone()[0])
            assert snapshot[0]["candidate_id"] == "a1"
            assert len(snapshot[0]["headline_sha256"]) == 64
            # 快照里本来没有完整标题时只冻结正文，不拿重评重算出来的标题冒充原值。
            assert snapshot[0]["title_sha256"] is None
            assert any(e["event_type"] == "test_model_call" for e in isolated.events(task_id))
            assert isolated.conn.execute("SELECT count(*) FROM human_decisions").fetchone()[0] == 0
        finally:
            isolated.conn.close()
    finally:
        storage.conn.close()


def test_rereview_missing_or_changed_snapshot_fails_before_model(tmp_path):
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("v", {})
    state = _rereview_state()
    state.pop("video")
    storage.set_state(task_id, "pending_human", state)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline._call = lambda *args: (_ for _ in ()).throw(AssertionError("不得调用模型"))
    before = list(storage.conn.iterdump())
    try:
        with pytest.raises(ValueError, match="快照"):
            pipeline.rereview(task_id)
        assert list(storage.conn.iterdump()) == before

        # 隔离必须是结构性的：光凭「原库里有 rereview_snapshot 表」不能证明是隔离库，
        # 否则手动插一张表就能让重评把事件与结果写回原库。
        from agent_system.pipeline import _freeze_rereview_candidates
        frozen = _freeze_rereview_candidates(_rereview_state()["candidates"])
        storage.conn.execute("CREATE TABLE rereview_snapshot(task_id TEXT PRIMARY KEY, candidates_json TEXT NOT NULL)")
        storage.conn.execute("INSERT INTO rereview_snapshot VALUES(?,?)", (task_id, json.dumps(frozen)))
        storage.conn.commit()
        forged = list(storage.conn.iterdump())
        with pytest.raises(ValueError, match="隔离库"):
            pipeline._rereview_isolated(task_id)
        assert list(storage.conn.iterdump()) == forged

        # 隔离库内的哈希不一致才轮到「候选被改写」这条拦截。
        iso_dir = tmp_path / "rereviews" / "iso"
        iso_dir.mkdir(parents=True)
        iso = Storage(iso_dir / "agent.db")
        iso_pipeline = Pipeline.__new__(Pipeline)
        iso_pipeline.config = pipeline.config
        iso_pipeline.storage = iso
        iso_pipeline._call = pipeline._call
        try:
            iso_task = iso.create_task("v", {})
            tampered = _rereview_state()
            tampered["candidates"][0]["headline"] = "篡改后的标题"
            iso.conn.execute("CREATE TABLE rereview_snapshot(task_id TEXT PRIMARY KEY, candidates_json TEXT NOT NULL)")
            iso.conn.execute("INSERT INTO rereview_snapshot VALUES(?,?)", (iso_task, json.dumps(frozen)))
            iso.set_state(iso_task, "pending_human", tampered)
            with pytest.raises(ValueError, match="哈希"):
                iso_pipeline._rereview_isolated(iso_task)
        finally:
            iso.conn.close()
    finally:
        storage.conn.close()


def test_rereview_failure_leaves_no_partial_database(tmp_path):
    """重评崩在半路不能留下无主库：否则 runtime/rereviews 会堆积分不清状态的残留。"""
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("v", {})
    storage.set_state(task_id, "pending_human", _rereview_state())
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.review_adapter = None
    pipeline._call = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("模型崩了"))
    try:
        with pytest.raises(RuntimeError, match="模型崩了"):
            pipeline.rereview(task_id)
        root = tmp_path / "rereviews"
        assert not root.exists() or list(root.iterdir()) == []
    finally:
        storage.conn.close()


# ---------------------------------------------------------------------------
# 人工判定 / 金标准 / 案例库（存储层）
# ---------------------------------------------------------------------------


def _memory_store(separate_bank: bool = False) -> Storage:
    # Storage 默认会建父目录；屏蔽它，数据库只在内存里。
    with patch.object(Path, "mkdir"):
        store = Storage(":memory:")
    if separate_bank:
        store.case_conn = sqlite3.connect(":memory:")
        for table in ("cases", "feedback"):
            sql = store.conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            store.case_conn.execute(sql)
        store.case_conn.commit()
    return store


def _seed_task(store: Storage, prefix: bool = False) -> str:
    task = store.create_task("v", {})
    state = {
        "video": {"video_id": "v"}, "rules": {"version": "r"},
        "candidates": [
            {"candidate_id": "a", "headline": "原标题",
             "rendered_title": "【AI生成】原标题" if prefix else "原标题"},
            {"candidate_id": "b", "headline": "另一个标题"},
        ],
        "candidate_decisions": [
            {"candidate_id": "a", "status": "pass"}, {"candidate_id": "b", "status": "pass"},
        ],
        "route": {"action": "handoff", "recommended_candidate_id": "a", "eligible_candidate_ids": ["a", "b"]},
    }
    store.set_state(task, "pending_human", state)
    return task


def _state_of(store: Storage, task: str) -> dict:
    return json.loads(store.get_task(task)["state_json"])


def test_approval_without_edit_keeps_rendered_prefix():
    """已修复：原样批准存的是渲染后标题（含平台前缀），不是裸 headline。

    证据：agent_system/storage.py 的 record_human_decision 使用 rendered_title。
    """
    store = _memory_store()
    try:
        task = _seed_task(store, prefix=True)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "approved"})
        assert _state_of(store, task)["final_headline"] == "【AI生成】原标题"
    finally:
        store.conn.close()


def test_reject_after_approve_clears_stale_approval():
    """已修复：approved -> rejected 改判必须清掉残留的 final_headline/human_decision。

    证据：agent_system/storage.py 的 record_human_decision 驳回分支里的
    state.pop("human_decision") / state.pop("final_headline")。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "approved"})
        store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "复核不合格"})
        state = _state_of(store, task)
        assert "human_decision" not in state
        assert "final_headline" not in state
        assert store.get_task(task)["status"] in {"pending_human", "stopped"}
    finally:
        store.conn.close()


def test_all_rejected_keeps_immutable_original_system_route():
    """已修复：人工全驳回会关掉 live route，但系统原推荐必须留在不可变快照里。

    证据：agent_system/storage.py 的 state.setdefault("original_system_route", ...)；
    scripts/eval.py 用 original_system_route 作为推荐准确率分母来源。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "不合格"})
        store.record_human_decision(task, {"candidate_id": "b", "decision": "rejected", "reason": "也不合格"})
        state = _state_of(store, task)
        assert state["route"]["action"] == "stop"
        assert state["original_system_route"]["recommended_candidate_id"] == "a"
    finally:
        store.conn.close()


def test_reopen_restores_original_recommendation():
    """已修复：撤回人工判定必须从快照恢复系统原推荐，而不是把它抹掉。

    证据：agent_system/storage.py 的 reopen_human_decision 从
    original_system_route.recommended_candidate_id 恢复。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "不合格"})
        store.reopen_human_decision(task, "a")
        state = _state_of(store, task)
        assert state["route"]["recommended_candidate_id"] == "a"
        assert state["route"]["action"] == "handoff"
    finally:
        store.conn.close()


def test_separate_case_bank_reopen_is_committed():
    """已修复：独立案例库的撤回必须自己 commit，回滚不能把已撤回的案例复活。

    证据：agent_system/storage.py 的 reopen_human_decision 中 case_conn 提交路径。
    """
    store = _memory_store(separate_bank=True)
    try:
        task = _seed_task(store)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "错误"})
        store.reopen_human_decision(task, "a")
        store.case_conn.rollback()
        assert store.case_conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    finally:
        store.case_conn.close()
        store.conn.close()


def test_main_rollback_does_not_confirm_case():
    """已修复：主库事务失败时，独立案例库不得已入册（案例只在主库提交后提交）。

    证据：agent_system/storage.py 的 record_human_decision 末尾
    if self.case_conn is not self.conn: self.case_conn.commit()（在 self.conn.commit() 之后）。
    """
    store = _memory_store(separate_bank=True)
    try:
        task = _seed_task(store)
        store.conn.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON events WHEN NEW.node='H01' "
            "BEGIN SELECT RAISE(ABORT, 'fault injection'); END")
        store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "错误"})
        assert store.conn.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0] == 0
        assert store.case_conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    finally:
        store.case_conn.close()
        store.conn.close()


class _MemoryCSV:
    """只实现 goldsheet 用到的 Path 子集，不落盘。"""

    def __init__(self):
        self.data = ""
        self.parent = self

    def mkdir(self, **kwargs):
        pass

    def exists(self):
        return bool(self.data)

    @contextlib.contextmanager
    def open(self, mode="r", **kwargs):
        stream = io.StringIO("" if mode == "w" else self.data)
        yield stream
        if mode == "w":
            self.data = stream.getvalue()


def _record_gold(target, task, reviewer):
    goldsheet.record(video_id="v", task_id=task, candidate_id="a", headline=task, verdict="可用",
                     final_headline=None, criteria_id="", fallback_node="", reason="",
                     reviewer=reviewer, path=target)


def test_goldsheet_identity_includes_task_so_cross_task_labels_survive():
    """已修复：金标准身份是「任务 + 候选」，跨任务同名候选不再互相覆盖。

    证据：agent_system/goldsheet.py 的 upsert 里 (key_video, key_task, key_cand) 匹配。
    """
    target = _MemoryCSV()
    _record_gold(target, "t1", "A")
    _record_gold(target, "t2", "B")
    rows = list(csv.DictReader(io.StringIO(target.data)))
    assert [(r["task_id"], r["判定人"]) for r in rows] == [("t1", "A"), ("t2", "B")]


def test_goldsheet_remove_is_scoped_by_task():
    """已修复：撤回金标准可以限定 task，不会误删别的实验臂的行。

    证据：agent_system/goldsheet.py 的 remove(..., task_id=...)；
    未给 task 时只撤回任务列为空的历史行。
    """
    target = _MemoryCSV()
    _record_gold(target, "t1", "A")
    _record_gold(target, "t2", "B")
    goldsheet.remove(video_id="v", candidate_id="a", path=target)
    assert len(list(csv.DictReader(io.StringIO(target.data)))) == 2
    goldsheet.remove(video_id="v", candidate_id="a", task_id="t1", path=target)
    assert [r["task_id"] for r in csv.DictReader(io.StringIO(target.data))] == ["t2"]


def test_reopen_drops_stale_feedback_rows():
    """缺陷：reopen_human_decision 删了 cases/labels/human_decisions/events，
    但独立案例库里由同一次判定写入的 feedback 行没有删——撤回后旧驳回理由仍可被检索到。

    证据：agent_system/storage.py 的 reopen_human_decision（只 DELETE cases/labels/events）；
    写入侧在 _auto_case 里 INSERT INTO feedback(..., case_id ...)。

    复现：a 通过、a 驳回、b 驳回后撤回 b，feedback 里仍留着 auto-<task>-b 那一行。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        store.record_human_decision(task, {"candidate_id": "a", "decision": "approved"})
        store.record_human_decision(task, {"candidate_id": "a", "decision": "rejected", "reason": "不合格"})
        store.record_human_decision(task, {"candidate_id": "b", "decision": "rejected", "reason": "也不合格"})
        store.reopen_human_decision(task, "b")
        remaining = [row[0] for row in store.conn.execute("SELECT case_id FROM feedback").fetchall()]
        assert f"auto-{task}-b" not in remaining, remaining
    finally:
        store.conn.close()


# ---------------------------------------------------------------------------
# 评审台 Handler（无 socket 替身）
# ---------------------------------------------------------------------------


def _invoke_decision_handler(store: Storage, task: str, body: dict) -> list:
    """截获真实嵌套 Handler 并调用 do_POST；服务器是替身，不监听端口。"""
    actual_conn = store.conn
    captured, gold = [], []

    class _KeepOpen:
        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(actual_conn, name)

    class _FakeServer:
        def __init__(self, address, handler):
            self.server_port = address[1]
            captured.append(handler)

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    store.conn = _KeepOpen()
    store.case_conn = store.conn
    try:
        with patch.object(debugger, "Storage", lambda *a, **k: store), \
                patch.object(debugger, "ThreadingHTTPServer", _FakeServer), \
                patch.object(debugger, "record_gold", lambda **kwargs: gold.append(kwargs)), \
                contextlib.redirect_stdout(io.StringIO()):
            debugger.serve(type("Config", (), {})(), None, "memory-only", None)
            handler = captured[0].__new__(captured[0])
            handler.path = "/api/decision"
            handler.read_body = lambda: {**body, "task_id": task}
            handler.send_json = lambda *args: None
            handler.do_POST()
    finally:
        store.conn = actual_conn
        store.case_conn = actual_conn
    return gold


def test_handler_keeps_marginal_edited_title_in_db():
    """已修复：marginal 判定提交的改后标题必须落库（此前只有 approved 才保留）。

    证据：agent_system/debugger.py 的 HumanDecision(... final_headline=_clean(...)
    if decision in {"approved","marginal"} ...)。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        _invoke_decision_handler(store, task, {
            "candidate_id": "a", "decision": "marginal", "reason": "缩写不清",
            "final_headline": "修正后的标题", "criteria_id": "C19",
        })
        decision = _state_of(store, task)["human_decisions"][0]
        assert decision["final_headline"] == "修正后的标题"
    finally:
        store.conn.close()


def test_handler_persists_marginal_criteria_id_in_db():
    """缺陷：Handler 只把 criteria_id 塞进 rejected 的 payload；marginal 判定即使前端给了
    判据 ID，DB 的 human_decision 与 gold.csv 也不一致（CSV 有、DB 没有），事后无法从库回溯。

    证据：agent_system/debugger.py 的 payload.update(...) 仅在
    decision.decision == "rejected" 时写入 reason_type / criteria_id；
    而 record_gold(..., criteria_id=...) 是无条件写入。
    """
    store = _memory_store()
    try:
        task = _seed_task(store)
        gold = _invoke_decision_handler(store, task, {
            "candidate_id": "a", "decision": "marginal", "reason": "缩写不清",
            "final_headline": "修正后的标题", "criteria_id": "C19",
        })
        decision = _state_of(store, task)["human_decisions"][0]
        assert gold[0]["criteria_id"] == "C19"
        assert decision.get("criteria_id") == "C19", decision
    finally:
        store.conn.close()


# ---------------------------------------------------------------------------
# 评测口径（scripts/eval.py / scripts/eval_compare.py）
# ---------------------------------------------------------------------------

_EVAL_LABELS = {
    "v": [{
        "task_id": "t", "status": "pending_human", "provider": "mock", "rule_version": "r", "created_at": "0",
        "state": {
            "candidates": [{"candidate_id": x, "headline": x} for x in ("a", "b", "c")],
            "route": {"recommended_candidate_id": "a"},
        },
    }]
}


def _run_eval(labels: list[dict]) -> list[str]:
    main = runpy.run_path(str(ROOT / "scripts" / "eval.py"))["main"]
    output = io.StringIO()
    with patch.dict(main.__globals__, {"load_gold": lambda path: labels, "index_tasks": lambda dbs: _EVAL_LABELS}), \
            patch.object(Path, "exists", return_value=True), patch.object(Path, "mkdir"), \
            patch.object(Path, "write_text", return_value=0), \
            patch.object(sys, "argv", ["eval", "--gold", "memory-only", "--db", "memory-only"]), \
            contextlib.redirect_stdout(output):
        main()
    return [line for line in output.getvalue().splitlines()
            if line.startswith(("| 一次通过率", "| 人工一致率", "- 有效判定"))]


def _label(candidate: str, verdict: str) -> dict:
    return {"video_id": "v", "task_id": "t", "candidate_id": candidate, "人工判定": verdict}


def test_strict_pass_rate_excludes_marginal_verdict():
    """已修复：严格通过率只数「可用」；「勉强通过」单独出现在灰度过率里。

    证据：scripts/eval.py 的 strict_videos（verdict == "可用"）与
    loose_videos（{"可用","勉强通过"}）。
    """
    lines = _run_eval([_label("a", "勉强通过")])
    assert any("一次通过率（严格）" in line and "0.0%" in line for line in lines), lines
    assert any("一次通过率（含勉强通过）" in line and "100.0%" in line for line in lines), lines


def test_recommendation_agreement_denominator_counts_only_recommended():
    """已修复：人工一致率分母只数「系统推荐的那条」，不是该任务下所有人工标签。

    证据：scripts/eval.py 的
    judged = [r for r in rows if r["system_recommended"] and r["candidate_id"] == r["system_recommended"]]。
    """
    lines = _run_eval([_label(x, "可用") for x in ("a", "b", "c")])
    assert any("人工一致率" in line and "100.0%" in line and "1 条可比" in line for line in lines), lines


def test_label_for_missing_candidate_is_not_counted():
    """已修复：金标准里的候选必须真的存在于指定任务，否则按身份错配丢弃，不计成功率分母。

    证据：scripts/eval.py 里 if chosen and chosen not in by_id: identity_errors.append(...); continue。
    """
    lines = _run_eval([_label("NOT_EXISTS", "可用")])
    assert any(line.startswith("- 有效判定：0 条候选") for line in lines), lines
    assert any("一次通过率（严格）" in line and "0.0%" in line for line in lines), lines
    assert any("一次通过率（严格）" in line and "0 条视频" in line for line in lines), lines


def test_inter_rater_separates_same_candidate_across_tasks():
    """缺陷：kappa 的「双标注」分组键是 (video_id, candidate_id)，没有 task_id。
    同一视频、不同任务（不同实验臂、不同标题）的同名候选被合并，产生不存在的「两人判过同一候选」，
    进而污染 kappa 与 multi_judged 样本量。

    证据：scripts/eval.py 的 inter_rater：
        grouped: dict[tuple[str, str], dict[str, str]]
        grouped.setdefault((vid, cid), {})[reviewer] = verdict   # 无 task_id
    """
    module = runpy.run_path(str(ROOT / "scripts" / "eval.py"))
    raters = module["inter_rater"]([
        {"video_id": "v", "task_id": "t1", "candidate_id": "a", "判定人": "A", "人工判定": "可用"},
        {"video_id": "v", "task_id": "t2", "candidate_id": "a", "判定人": "B", "人工判定": "不可用"},
    ])
    assert raters["multi_judged"] == 0, raters


def _summarize():
    return runpy.run_path(str(ROOT / "scripts" / "eval_compare.py"))["summarize"]


def test_eval_compare_paired_denominator_counts_only_paired_videos():
    """已修复（基线行为）：给了 run map 交集时，视频数按配对集合算，配对集合外的视频不进分母。"""
    summarize = _summarize()
    one = [{"video_id": "v1", "title": "x", "verdict": "可用"}]
    result = summarize(one + [{"video_id": "outside", "title": "x", "verdict": "可用"}], 20, {"v1"})
    assert result["videos"] == 1
    assert result["strict_rate"] == 1.0
    # 修复后：候选条数与判定分布也只统计配对集合内的标签
    assert result["titles"] == 1


def test_eval_compare_ignores_labels_outside_paired_denominator():
    """缺陷：summarize 只用 denom 计算视频数与通过率，long_titles/total_titles/dist
    仍然遍历全部 labels。配对集合外（别的批次/别的臂）的超长标题会污染超长流入率与判定分布。

    证据：scripts/eval_compare.py 的 summarize：
        long_titles = sum(1 for l in labels if len(...) > max_chars)
        dist = Counter(l.get("verdict") or "?" for l in labels)      # 未按 denom 过滤
    """
    summarize = _summarize()
    one = [{"video_id": "v1", "title": "x", "verdict": "可用"}]
    outside = summarize(one + [{"video_id": "outside", "title": "x" * 21, "verdict": "不可用"}], 20, {"v1"})
    assert outside["long"] == 0 and outside["titles"] == 1, outside
    assert outside["dist"] == {"可用": 1}, outside["dist"]


def test_eval_compare_empty_intersection_does_not_fall_back():
    """缺陷：summarize 用 sorted(denom) if denom else sorted(by_video)——
    显式传入的空配对集合（两臂 run map 交集为空）被当成「没给分母」，回退到本臂标签里的视频，
    于是不配对的批次也能算出「配对视频 N 条」的通过率。

    证据：scripts/eval_compare.py 的 videos = sorted(denom) if denom else sorted(by_video)；
    调用侧 denom = (b_videos & c_videos) if (b_videos and c_videos) else None。
    """
    summarize = _summarize()
    one = [{"video_id": "v1", "title": "x", "verdict": "可用"}]
    empty = summarize(one, 20, set())
    assert empty["videos"] == 0, empty


# ---------------------------------------------------------------------------
# 复现脚本保留为只读参考
# ---------------------------------------------------------------------------


def test_reproduce_scripts_still_exist_as_reference():
    """两个复现脚本保留为只读参考；本文件是它们的正式回归替身。"""
    base = ROOT / "runtime" / "acceptance_deepseek_20261101_114430"
    assert (base / "reproduce_generation_gaps.py").is_file()
    assert (base / "reproduce_review_gaps.py").is_file()
