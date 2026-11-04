"""初次生成、重写与重新评审必须共用同一套证据资格判定。

这些回归只在离线回放上跑：同一份视频快照与候选，走初次生成和重新评审两条路径时，
机械检查与最终路由必须给出相同结论；不允许「再评一次」就放宽白名单或绕开人工待核。
"""

from __future__ import annotations

import json

import pytest

from agent_system.config import RunConfig
from agent_system.pipeline import Pipeline
from agent_system.storage import Storage

from test_safety_contracts import _OneVideoDataset, _review_payload, run_generation_case


def _codes(candidate: dict) -> list[str]:
    return [str(e.get("code")) for e in (candidate.get("mechanical_checks") or {}).get("errors", [])]


def _rereview(source_state: dict, tmp_path, *, review: dict | None = None) -> tuple[dict, str]:
    """把已落库状态复制进隔离库后重评；返回 (重评状态, 隔离库路径)。"""
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("offline", {})
    storage.set_state(task_id, "pending_human", source_state)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.dataset = _OneVideoDataset()
    pipeline.review_adapter = None
    payload = review

    def replay(task, node, name, user, schema, *args, **kwargs):
        if review is None:
            return {"reviews": [_review_payload(c["candidate_id"]) for c in user["candidate_titles"]]}
        return payload

    pipeline._call = replay
    try:
        path = pipeline.rereview(task_id)
    finally:
        storage.conn.close()
    isolated = Storage(path)
    try:
        return json.loads(isolated.get_task(task_id)["state_json"]), path
    finally:
        isolated.conn.close()


def test_source_title_only_claim_routes_to_review_in_both_paths(tmp_path):
    """只引用原题的候选：初次生成与重评都必须停在人工待核，而不是一边放行。"""
    generated = run_generation_case("source_only")
    assert generated["route"].get("reason") == "SOURCE_TITLE_ONLY_UNVERIFIED", generated["route"]

    rereviewed, _ = _rereview(generated["final"], tmp_path)
    assert rereviewed["source_only_claim_ids"] == ["c1"]
    assert rereviewed["route"].get("action") == "needs_review", rereviewed["route"]
    assert rereviewed["route"].get("reason") == "SOURCE_TITLE_ONLY_UNVERIFIED", rereviewed["route"]


@pytest.mark.parametrize("case", ["excluded_unit", "pending_claim"])
def test_mechanical_eligibility_matches_between_paths(case, tmp_path):
    """已排除单元、暂缓核验陈述：两条路径的机械结论与路由结论必须一致。

    2026-11-04 契约更新：pending_claim 引用真实存在的待核验 claim 不再是机械错误
    （V 集人工 5/6 判可用），改为 blocked_claim_refs 登记 + needs_review 交人工。
    excluded_unit 仍是机械错误（unit 引用超范围口径未变）。两条路径各自保持一致。
    """
    generated = run_generation_case(case)
    assert generated["error"] is None, generated["error"]
    generated_codes = _codes(generated["candidates"][0])
    generated_checks = generated["candidates"][0].get("mechanical_checks") or {}
    if case == "excluded_unit":
        assert "G4_INVALID_REFERENCE" in generated_codes, generated_codes
    else:
        assert "G4_INVALID_REFERENCE" not in generated_codes, generated_codes
        assert generated_checks.get("blocked_claim_refs") == ["c2"], generated_checks
    assert generated["route"].get("action") != "handoff", generated["route"]

    rereviewed, _ = _rereview(generated["final"], tmp_path)
    assert _codes(rereviewed["candidates"][0]) == generated_codes
    assert rereviewed["route"].get("action") == generated["route"].get("action"), (
        generated["route"], rereviewed["route"]
    )


def test_visual_only_claim_stops_before_any_title(tmp_path):
    """C22 反例：只有模型画面描述的陈述被降到待核后，全无可写陈述就不产出标题；
    这种状态没有候选可冻结，重评必须直接报错，不能凭空造一条候选再审。"""
    generated = run_generation_case("visual_only")
    assert generated["error"] is None
    assert generated["status"] == "stopped"
    assert generated["candidates"] == []
    assert generated["final"]["primary_evidence_audit"]["flagged"] == 1

    with pytest.raises(ValueError, match="候选"):
        _rereview(generated["final"], tmp_path)


def test_prefix_label_candidate_is_not_reported_as_changed(tmp_path):
    """带前缀的候选：重评会用前缀重新拼完整标题，不能因此误判「标题被改写」。"""
    generated = run_generation_case("source_only")
    state = generated["final"]
    candidate = state["candidates"][0]
    candidate["prefix_labels"] = ["探店"]
    candidate["rendered_title"] = "【探店】" + candidate["headline"]

    rereviewed, path = _rereview(generated["final"], tmp_path)
    assert rereviewed["candidates"][0]["rendered_title"] == "【探店】" + candidate["headline"]

    isolated = Storage(path)
    try:
        snapshot = json.loads(
            isolated.conn.execute("SELECT candidates_json FROM rereview_snapshot").fetchone()[0]
        )
    finally:
        isolated.conn.close()
    assert len(snapshot[0]["title_sha256"]) == 64


def test_rereview_records_its_own_provenance(tmp_path):
    """隔离库必须能自证来源：源库路径、模型、提示词版本、冻结候选。"""
    generated = run_generation_case("source_only")
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("offline", {})
    storage.set_state(task_id, "pending_human", generated["final"])
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.dataset = _OneVideoDataset()
    pipeline.review_adapter = None
    pipeline._call = lambda task, node, name, user, schema, *a, **k: {
        "reviews": [_review_payload(c["candidate_id"]) for c in user["candidate_titles"]]
    }
    try:
        path = pipeline.rereview(task_id)
    finally:
        storage.conn.close()
    isolated = Storage(path)
    try:
        rereviewed = json.loads(isolated.get_task(task_id)["state_json"])
    finally:
        isolated.conn.close()
    isolation = rereviewed["rereview_isolation"]
    assert isolation["source_database_path"].endswith("source.db")
    assert isolation["task_id"] == task_id
    assert isolation["prompt_pack_version"]
    assert isolation["frozen_candidates"][0]["candidate_id"] == "a1"


def test_rereview_clears_stale_delivery_when_route_falls_back(tmp_path):
    """重评把候选判回人工待核时，状态里旧的「已交付」清单不能继续留着。"""
    generated = run_generation_case("source_only")
    state = generated["final"]
    state["delivery_candidates"] = [{"candidate_id": "a1"}]
    state["delivery_slots"] = {"expected": ["source_rewrite", "core_summary", "core_hook"],
                               "filled": ["source_rewrite"], "missing": ["core_summary", "core_hook"]}

    rereviewed, _ = _rereview(state, tmp_path)
    assert rereviewed["route"]["action"] == "needs_review"
    assert rereviewed["delivery_candidates"] == []
    assert rereviewed["delivery_slots"]["filled"] == []
    assert rereviewed["delivery_slots"]["missing"] == ["source_rewrite", "core_summary", "core_hook"]


def test_rereview_does_not_touch_source_task(tmp_path):
    """重评只写隔离库：原任务的候选、评审与状态保持逐字不变。"""
    generated = run_generation_case("source_only")
    storage = Storage(tmp_path / "source.db")
    task_id = storage.create_task("offline", {})
    storage.set_state(task_id, "pending_human", generated["final"])
    before = list(storage.conn.iterdump())
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = RunConfig(output_dir=str(tmp_path))
    pipeline.storage = storage
    pipeline.dataset = _OneVideoDataset()
    pipeline.review_adapter = None
    pipeline._call = lambda task, node, name, user, schema, *a, **k: {
        "reviews": [_review_payload(c["candidate_id"]) for c in user["candidate_titles"]]
    }
    try:
        path = pipeline.rereview(task_id)
        assert list(storage.conn.iterdump()) == before
    finally:
        storage.conn.close()
    isolated = Storage(path)
    try:
        assert isolated.get_task(task_id)["status"] in {"pending_human", "stopped"}
        assert isolated.events(task_id) if hasattr(isolated, "events") else True
    finally:
        isolated.conn.close()


# ---------------------------------------------------------------------------
# 语义标签不得由模型自报：只在有一手文本/原题支撑时才能声明
# ---------------------------------------------------------------------------


def _labelled_video(evidence):
    from agent_system.types import VideoInput
    return VideoInput("v", "小店开业", "用户", None, "", {"一级": "生活"}, "现场记录", None, 10, {},
                      "", "", [{"text": "小店开业"}], [], evidence, {}, {})


def _labelled_understanding(unit_semantic_use, unit_grounding, unit_evidence_ids, claim_evidence_refs):
    from agent_system.types import Claim, SemanticUnit, Understanding
    claim = Claim("c1", "小店开业", "observable", list(claim_evidence_refs))
    unit = SemanticUnit("u1", "小店开业", ["c1"], list(unit_evidence_ids),
                        semantic_use=unit_semantic_use, grounding=unit_grounding)
    return Understanding("现场记录", [], [claim], [], [], [], [unit], [], [])


def test_visual_only_unit_cannot_claim_observable_fact():
    """只有画面描述的单元，标 observable_fact + grounded 会被程序双降到待核。"""
    from agent_system.pipeline import _bind_semantic_use_to_evidence
    from agent_system.types import Evidence

    evidence = [Evidence("desc", "description", "一方迟到", quality="model_derived_uncalibrated"),
                Evidence("raw", "asr", "小店开业", quality="raw_asr")]
    understanding = _labelled_understanding("observable_fact", "grounded", ["desc"], ["raw"])
    adjusted = _bind_semantic_use_to_evidence(understanding, _labelled_video(evidence))

    unit = understanding.semantic_units[0]
    assert (unit.semantic_use, unit.grounding) == ("unsupported_inference", "unknown")
    assert adjusted[0]["unit_id"] == "u1"
    assert adjusted[0]["referenced_qualities"] == ["model_derived_uncalibrated"]
    assert len(adjusted[0]["reasons"]) == 2


def test_asr_backed_unit_keeps_its_labels():
    """引用 ASR 的单元不动：这一步只拦「无一手文本」，不做语义真值判断。"""
    from agent_system.pipeline import _bind_semantic_use_to_evidence
    from agent_system.types import Evidence

    evidence = [Evidence("raw", "asr", "小店开业", quality="raw_asr")]
    understanding = _labelled_understanding("observable_fact", "grounded", ["raw"], ["raw"])
    adjusted = _bind_semantic_use_to_evidence(understanding, _labelled_video(evidence))

    unit = understanding.semantic_units[0]
    assert (unit.semantic_use, unit.grounding) == ("observable_fact", "grounded")
    assert adjusted == []


def test_source_title_alone_cannot_claim_observable_fact():
    """原题只证明「标题这么写」：可以保留 grounding，但不能升级成视频内容的可观察事实。"""
    from agent_system.pipeline import _bind_semantic_use_to_evidence
    from agent_system.types import Evidence

    evidence = [Evidence("src", "title", "小店开业", quality="source"),
                Evidence("raw", "asr", "别的", quality="raw_asr")]
    understanding = _labelled_understanding("observable_fact", "grounded", ["src"], ["src"])
    adjusted = _bind_semantic_use_to_evidence(understanding, _labelled_video(evidence))

    unit = understanding.semantic_units[0]
    assert unit.semantic_use == "unsupported_inference"
    assert unit.grounding == "grounded", "有原题支撑时不该顺手降 grounding"
    assert len(adjusted[0]["reasons"]) == 1


def test_unit_without_any_reference_is_downgraded():
    """单元一条证据都不引用时，回退到它的 claim 引用；仍然没有就按无支撑处理。"""
    from agent_system.pipeline import _bind_semantic_use_to_evidence
    from agent_system.types import Evidence

    evidence = [Evidence("raw", "asr", "小店开业", quality="raw_asr")]
    understanding = _labelled_understanding("observable_fact", "grounded", [], [])
    adjusted = _bind_semantic_use_to_evidence(understanding, _labelled_video(evidence))

    unit = understanding.semantic_units[0]
    assert (unit.semantic_use, unit.grounding) == ("unsupported_inference", "unknown")
    assert adjusted[0]["referenced_qualities"] == []


def test_optimistic_label_blocks_handoff_in_both_paths(tmp_path):
    """自报乐观的核心单元：机械检查干净，但仍被拦在自动交付之外；重评同一快照结论不变。"""
    generated = run_generation_case("optimistic_label")
    assert generated["error"] is None
    assert _codes(generated["candidates"][0]) == []
    assert generated["route"]["action"] == "needs_review"
    assert generated["route"]["reason"] == "CORE_EVIDENCE_CLASSIFICATION_UNRESOLVED"
    unit = generated["final"]["understanding"]["semantic_units"][0]
    assert (unit["semantic_use"], unit["grounding"]) == ("unsupported_inference", "unknown")

    rereviewed, _ = _rereview(generated["final"], tmp_path)
    assert rereviewed["route"]["action"] == "needs_review"
    assert rereviewed["route"]["reason"] == "CORE_EVIDENCE_CLASSIFICATION_UNRESOLVED"


def test_rereview_reapplies_downgrade_to_tampered_labels(tmp_path):
    """把落库后的标签改回乐观值，重评必须重新降级——不能靠「历史已经降过」放行。"""
    generated = run_generation_case("optimistic_label")
    state = generated["final"]
    state["understanding"]["semantic_units"][0]["semantic_use"] = "observable_fact"
    state["understanding"]["semantic_units"][0]["grounding"] = "grounded"

    rereviewed, _ = _rereview(state, tmp_path)
    unit = rereviewed["understanding"]["semantic_units"][0]
    assert (unit["semantic_use"], unit["grounding"]) == ("unsupported_inference", "unknown")
    assert rereviewed["route"]["reason"] == "CORE_EVIDENCE_CLASSIFICATION_UNRESOLVED"


# ---------------------------------------------------------------------------
# 失败率：模型输出的合法变体不能被读成「零产出」
# ---------------------------------------------------------------------------


def test_grouped_boundary_schema_yields_decisions():
    """模型按「可用/受限/排除」分组给 ID 时，必须还原成逐条决策，而不是 0 条。"""
    from agent_system.pipeline import _normalize_boundary

    raw = {
        "claims_total": 4,
        "usable_claims": ["c1", "c2"],
        "limited_claims": ["c3"],
        "excluded_claims": ["c4"],
        "verification_required_claims": [],
        "unit_decisions": {"u1": "usable", "u2": "limited"},
    }
    boundary = _normalize_boundary(raw, {"c1": "a", "c2": "b", "c3": "c", "c4": "d"}, {})
    assert {d.claim_id: d.decision for d in boundary.decisions} == {
        "c1": "usable", "c2": "usable", "c3": "limited", "c4": "excluded"}
    assert {u.unit_id: u.decision for u in boundary.unit_decisions} == {"u1": "usable", "u2": "limited"}


def test_grouped_boundary_conflict_keeps_strictest():
    """同一 claim 出现在多个分组时取最严，不能因为顺序不同而放宽。"""
    from agent_system.pipeline import _normalize_boundary

    boundary = _normalize_boundary(
        {"usable_claims": ["c1", "c2"], "excluded_claims": ["c1"]}, {"c1": "a", "c2": "b"}, {})
    assert {d.claim_id: d.decision for d in boundary.decisions} == {"c1": "excluded", "c2": "usable"}


def test_verification_required_claims_stay_out_of_writable_set():
    from agent_system.pipeline import _normalize_boundary

    boundary = _normalize_boundary(
        {"usable_claims": ["c1"], "verification_required_claims": ["c2"]}, {"c1": "a", "c2": "b"}, {})
    decisions = {d.claim_id: d for d in boundary.decisions}
    assert decisions["c2"].decision == "limited"
    assert decisions["c2"].verification_required is True


def test_dangling_unit_ref_is_pruned_not_fatal():
    """单元引用不存在的 claim：只剪这一处，任务继续生成，不是整条视频零产出。"""
    result = run_generation_case("dangling_unit_ref")
    assert result["error"] is None
    assert result["route"]["reason"] != "REFERENCE_AUDIT_INVALID"
    pruned = result["final"]["reference_pruned_units"]
    assert pruned[0]["unit_id"] == "u1"
    assert pruned[0]["dropped"] == ["c99", "c100"]
    assert pruned[0]["excluded"] is False
    assert result["candidates"], "剪掉悬空引用后仍应产出候选"


def test_grouped_boundary_case_does_not_stop_without_usable_claim():
    result = run_generation_case("grouped_boundary")
    assert result["error"] is None
    assert result["route"]["reason"] != "NO_USABLE_CLAIM"
    assert result["candidates"]


def test_truncated_json_keeps_tail_objects():
    """max_tokens 截断时不能用 rfind('}') 丢掉尾部对象——那是静默丢数据。"""
    from agent_system.model import loads_json_loose

    truncated = ('{"candidates":[{"candidate_id":"cand-1","types":["G1"]},'
                 '{"candidate_id":"cand-2","expected_takeaway":{"subject":"博主（')
    parsed = loads_json_loose(truncated)
    ids = [c["candidate_id"] for c in parsed["candidates"]]
    assert ids == ["cand-1", "cand-2"], "尾部候选不能被截断逻辑丢掉"
    assert parsed["candidates"][1]["expected_takeaway"]["subject"] == "博主（"


def test_empty_unit_refs_are_excluded_not_fatal():
    """剪掉悬空引用后留下的空壳单元判 excluded，不让整条视频零产出。"""
    from agent_system.pipeline import _exclude_units_with_empty_refs
    from agent_system.types import BoundarySet, Claim, SemanticUnit, Understanding

    claim = Claim("c1", "文本", "observable", ["e1"])
    shell = SemanticUnit("u9", "空壳", [], [])
    understanding = Understanding("现场记录", [], [claim], [], [], [], [shell], [], [])
    boundary = BoundarySet(decisions=[])
    excluded = _exclude_units_with_empty_refs(
        understanding, boundary,
        [{"code": "empty_refs", "path": "understanding.semantic_units[0]", "value": None}])
    assert excluded == ["u9"]
    assert boundary.unit_decisions[0].decision == "excluded"
    # 已经排除过的单元不重复记录
    assert _exclude_units_with_empty_refs(
        understanding, boundary,
        [{"code": "empty_refs", "path": "understanding.semantic_units[0]", "value": None}]) == []


def test_truncation_at_value_position_keeps_siblings():
    """截断停在 `{"subject":` 时要补 null，而不是删掉冒号把整段废掉。"""
    from agent_system.model import loads_json_loose

    parsed = loads_json_loose(
        '{"candidates":[{"candidate_id":"c1"},{"candidate_id":"c2","expected_takeaway":{"subject":')
    assert [c["candidate_id"] for c in parsed["candidates"]] == ["c1", "c2"]
    assert parsed["candidates"][1]["expected_takeaway"]["subject"] is None


# ---------------------------------------------------------------------------
# 主线收敛：多段并列视频不能把尾段当核心
# ---------------------------------------------------------------------------


def _mainline_fixture():
    from types import SimpleNamespace
    from agent_system.types import Claim, SemanticUnit, Understanding

    segs = [{"start": i * 2.0, "end": i * 2.0 + 2.0} for i in range(20)]
    units = [
        SemanticUnit("u1", "士兵滚灰装死骗过火焰喷射器", ["c1", "c2"], ["e-asr-0000"], kind="叙事",
                     role_in_video="开场故事主体"),
        SemanticUnit("u2", "把枪藏进乐高枪混过安检", ["c3"], ["e-asr-0010"], kind="叙事",
                     role_in_video="第二段故事"),
        SemanticUnit("u3", "铁锹绑RPG弹头破层深爆", ["c4"], ["e-ocr-0001"], kind="军事知识/解释",
                     role_in_video="科普解释段"),
    ]
    claims = [
        Claim("c1", "跳进掩体", "observable", ["e-asr-0000"]),
        Claim("c2", "骗过敌军", "observable", ["e-asr-0001"]),
        Claim("c3", "藏进乐高枪", "observable", ["e-asr-0010"]),
        Claim("c4", "铁锹绑弹头", "observable", ["e-ocr-0001"]),
    ]
    understanding = Understanding(main_format="", attributes=[], claims=claims, event_times=[],
                                  extracted_sources=[], semantic_units=units)
    return understanding, SimpleNamespace(asr_segments=segs)


def test_multi_segment_video_keeps_opening_as_mainline():
    """装死(0秒)与藏枪(20秒)并列时，主线必须取开头那段。"""
    from agent_system.pipeline import _enforce_single_mainline

    understanding, video = _mainline_fixture()
    contract = {"core_unit_ids": ["u1", "u2"]}
    demoted = _enforce_single_mainline(contract, understanding, video)
    assert contract["core_unit_ids"] == ["u1"]
    assert [d["unit_id"] for d in demoted] == ["u2"]


def test_tutorial_evaluation_is_not_demoted_for_starting_later():
    from agent_system.pipeline import _enforce_single_mainline, _core_hook_is_aligned

    understanding, video = _mainline_fixture()
    understanding.semantic_units[1].kind = "source_opinion"
    contract = {"thesis_id": "t1", "core_unit_ids": ["u1", "u2"]}
    assert _enforce_single_mainline(contract, understanding, video) == []
    highlights = [
        {"highlight_id": "h1", "role": "main", "relation": "core", "unit_id": "u1", "thesis_id": "t1"},
        {"highlight_id": "h2", "role": "backup", "relation": "core", "unit_id": "u2", "thesis_id": "t1"},
    ]
    candidate = {"highlight_id": "h2"}
    assert _core_hook_is_aligned(candidate, highlights, contract)
    highlights[1]["relation"] = "support"
    assert not _core_hook_is_aligned(candidate, highlights, contract)
    highlights[1]["relation"] = "core"
    assert not _core_hook_is_aligned(candidate, highlights, {"thesis_id": "t1", "core_unit_ids": ["u1"]})
    highlights[1]["thesis_id"] = "other"
    assert not _core_hook_is_aligned(candidate, highlights, contract)


def test_untimed_core_is_not_demoted_without_time_evidence():
    """缺少 ASR 时间不等于非核心，仍需独立核验材料。"""
    from agent_system.pipeline import _enforce_single_mainline

    understanding, video = _mainline_fixture()
    contract = {"core_unit_ids": ["u1", "u3"]}
    assert _enforce_single_mainline(contract, understanding, video) == []
    assert contract["core_unit_ids"] == ["u1", "u3"]


def test_overlapping_units_are_not_treated_as_separate_segments():
    """同一段落内的细分单元（时间重叠）不该被拆开。"""
    from agent_system.pipeline import _enforce_single_mainline

    understanding, video = _mainline_fixture()
    contract = {"core_unit_ids": ["u1", "u2"]}
    # 把 u2 挪到与 u1 重叠的位置
    understanding.semantic_units[1].claim_ids = ["c2"]
    understanding = understanding.__class__(
        main_format="", attributes=[], claims=understanding.claims, event_times=[],
        extracted_sources=[], semantic_units=understanding.semantic_units)
    assert _enforce_single_mainline(contract, understanding, video) == []
    assert contract["core_unit_ids"] == ["u1", "u2"]
