"""路由交付面：候选不能在「降级/需要核实」时从人工视野里消失。

两起真实误拦（人工判可用、系统却没交给人工）：
- R16：同任务里 `ready_for_human_review` 与 `needs_review` 并存，handoff 只交前者；
- R08：主旨契约不一致把 handoff 降级成 needs_review 时，顺手把 eligible 清空，
  整组候选从交付面消失。

判据不是「是不是首推」——路由推荐的是**一条**，交付的是**一组**。
"""
from __future__ import annotations

from agent_system.pipeline import Pipeline


class _FakeStorage:
    def __init__(self):
        self.events = []

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class _FakeConfig:
    # 降级开关默认开（Pipeline 读的就是这个键）。
    extra = {"downgrade_on_contract_disagreement": True}


class _FakePipeline:
    def __init__(self):
        self.storage = _FakeStorage()
        self.config = _FakeConfig()

    def _cached_reconcile(self, task_id, state):
        """语义判官不可用 → 走保守降级分支。"""
        return None


def _disagreeing_state():
    """R01 提过异议 → 进入降级判断（词法判定与语义判官都留空则直接返回，见 pipeline 2916）。"""
    return {"thesis_consistency_history": [{"relation": "contradiction", "contradiction_point": "主体读反"}]}


def _decisions(*pairs):
    return [{"candidate_id": cid, "status": status, "quality_sum": 10 - i}
            for i, (cid, status) in enumerate(pairs)]


def test_handoff_keeps_needs_review_candidates_visible_to_humans():
    """R16：needs_review 的候选必须一起交付，否则人工不知道要核实哪条。"""
    route = Pipeline._route_candidates(
        _FakePipeline(), _decisions(("cand-3", "ready_for_human_review"), ("cand-1", "needs_review")), 0)

    assert route["action"] == "handoff"
    assert route["eligible_candidate_ids"] == ["cand-3"]
    assert route["pending_review_candidate_ids"] == ["cand-1"]


def test_handoff_without_needs_review_has_an_empty_pending_list():
    route = Pipeline._route_candidates(
        _FakePipeline(), _decisions(("cand-1", "ready_for_human_review")), 0)

    assert route["eligible_candidate_ids"] == ["cand-1"]
    assert route["pending_review_candidate_ids"] == []


def test_contract_disagreement_downgrades_instead_of_erasing_candidates():
    """R08：降级为待核时，候选要转到 pending_review，不能被清空。"""
    pipeline = _FakePipeline()
    route = {"action": "handoff", "recommended_candidate_id": "cand-1",
             "eligible_candidate_ids": ["cand-1", "cand-2", "cand-3"], "reason": "eligible_candidates_ranked_by_Q1_Q2_Q3"}
    out = Pipeline._downgrade_on_contract_disagreement(pipeline, "task-1", _disagreeing_state(), route)

    assert out["action"] == "needs_review"
    assert out["reason"] == "THESIS_CONTRACT_DISAGREEMENT"
    assert out["recommended_candidate_id"] is None
    assert out["eligible_candidate_ids"] == []
    assert out["pending_review_candidate_ids"] == ["cand-1", "cand-2", "cand-3"]


def test_contract_disagreement_leaves_a_non_handoff_route_alone():
    """本来就没在推荐的任务（待核/重写/拒绝）不应被改写 reason。"""
    pipeline = _FakePipeline()
    route = {"action": "rewrite", "recommended_candidate_id": None,
             "eligible_candidate_ids": [], "reason": "repairable_gate_or_quality_issues"}
    out = Pipeline._downgrade_on_contract_disagreement(pipeline, "task-1", _disagreeing_state(), route)

    assert out["action"] == "rewrite"
    assert out["reason"] == "repairable_gate_or_quality_issues"


def test_handoff_backfills_candidates_when_a_branch_forgot_them():
    """兜底：THESIS_CONTRACT_INVALID / SUBROUTE_ESCALATION / HIGHLIGHT_RESELECTION_LIMIT
    / REWRITE_BUDGET_EXHAUSTED 这些分支各自现拼 route，容易漏带候选清单。
    只要 state 里有未被业务拒绝的候选，就必须交给人工（转为待核实）。"""
    pipeline = _FakePipeline()
    pipeline.storage = _FakeStorage()
    pipeline.storage.checkpoint = lambda *a, **k: None
    state = {
        "candidate_decisions": [
            {"candidate_id": "cand-1", "status": "ready_for_human_review"},
            {"candidate_id": "cand-2", "status": "needs_review"},
            {"candidate_id": "cand-3", "status": "rejected"},
        ],
        "candidates": [{"candidate_id": "cand-1"}, {"candidate_id": "cand-2"}, {"candidate_id": "cand-3"}],
    }
    route = {"action": "stop", "recommended_candidate_id": None, "eligible_candidate_ids": [],
             "reason": "REWRITE_BUDGET_EXHAUSTED"}

    Pipeline._handoff(pipeline, "task-x", state, route)

    assert state["route"]["pending_review_candidate_ids"] == ["cand-1", "cand-2"]
    assert "cand-3" not in state["route"]["pending_review_candidate_ids"], "被业务拒绝的候选不该塞回人工"


def test_handoff_leaves_an_existing_surface_untouched():
    """清单本来就有时不重复改写。"""
    pipeline = _FakePipeline()
    pipeline.storage.checkpoint = lambda *a, **k: None
    state = {"candidate_decisions": [{"candidate_id": "cand-9", "status": "ready_for_human_review"}]}
    route = {"action": "handoff", "recommended_candidate_id": "cand-1",
             "eligible_candidate_ids": ["cand-1"], "reason": "ok"}
    Pipeline._handoff(pipeline, "task-x", state, route)
    assert "pending_review_candidate_ids" not in route


def test_handoff_does_not_invent_candidates_for_a_genuinely_empty_task():
    """真·零合规候选（全部 rejected）不该被塞进待核实清单。"""
    pipeline = _FakePipeline()
    pipeline.storage.checkpoint = lambda *a, **k: None
    state = {"candidate_decisions": [{"candidate_id": "cand-1", "status": "rejected"}]}
    route = {"action": "stop", "recommended_candidate_id": None,
             "eligible_candidate_ids": [], "reason": "NO_COMPLIANT_CANDIDATE"}
    Pipeline._handoff(pipeline, "task-x", state, route)
    assert "pending_review_candidate_ids" not in route


def test_handoff_backfills_rewrite_candidates_when_budget_is_exhausted():
    """重写预算耗尽时，rewrite 候选就是终态产物，必须交给人工。

    真实故障：BV1qBxE6JESv 九条候选全 rewrite、BV1uWnZ6vEvj 三条全 rewrite，
    因兜底只捞 ready/needs_review 而全部漏掉，人工看到的是空清单。
    """
    pipeline = _FakePipeline()
    pipeline.storage.checkpoint = lambda *a, **k: None
    state = {"candidate_decisions": [{"candidate_id": f"cand-{i}", "status": "rewrite"} for i in (1, 2, 3)]}
    route = {"action": "stop", "recommended_candidate_id": None,
             "eligible_candidate_ids": [], "reason": "REWRITE_BUDGET_EXHAUSTED"}

    Pipeline._handoff(pipeline, "task-x", state, route)

    assert state["route"]["pending_review_candidate_ids"] == ["cand-1", "cand-2", "cand-3"]
