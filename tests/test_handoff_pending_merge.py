"""人工可见清单的补齐不改变自动推荐、交付或风险判定。"""
from copy import deepcopy

from agent_system.pipeline import Pipeline


class Storage:
    def __init__(self):
        self.checkpoints = []
        self.events = []

    def checkpoint(self, *args):
        self.checkpoints.append(args)

    def event(self, *args):
        self.events.append(args)


class PipelineStub:
    def __init__(self):
        self.storage = Storage()


def test_existing_surface_merges_only_missing_reviewable_ids():
    pipeline = PipelineStub()
    route = {"action": "handoff", "recommended_candidate_id": "ready",
             "eligible_candidate_ids": ["ready"], "candidate_ids": ["listed"],
             "pending_review_candidate_ids": ["pending"], "reason": "original"}
    delivery = [{"candidate_id": "ready"}]
    decisions = [
        {"candidate_id": "ready", "status": "rewrite"},
        {"candidate_id": "listed", "status": "needs_review"},
        {"candidate_id": "pending", "status": "needs_review"},
        {"candidate_id": "rewrite", "status": "rewrite"},
        {"candidate_id": "rewrite", "status": "rewrite"},
        {"candidate_id": "uncertain", "status": "needs_review"},
        {"candidate_id": "risk", "status": "reject"},
        {"candidate_id": "other-ready", "status": "ready_for_human_review"},
    ]
    state = {"route": route, "delivery_candidates": delivery, "candidate_decisions": decisions}
    original_route, original_delivery = deepcopy(route), deepcopy(delivery)

    assert Pipeline._handoff(pipeline, "t", state, route) == "t"
    final = state["route"]
    assert final["pending_review_candidate_ids"] == ["pending", "rewrite", "uncertain"]
    assert {k: v for k, v in final.items() if k != "pending_review_candidate_ids"} == {
        k: v for k, v in original_route.items() if k != "pending_review_candidate_ids"}
    assert state["delivery_candidates"] == original_delivery
    assert state["candidate_decisions"] == decisions
    assert pipeline.storage.checkpoints[0][1] == "pending_human"
    assert pipeline.storage.checkpoints[0][-1]["route"] == final
    assert pipeline.storage.events[0][-1]["route"] == final


def test_existing_candidate_ids_do_not_hide_rewrite_and_stop_stays_stopped():
    pipeline = PipelineStub()
    route = {"action": "stop", "candidate_ids": ["existing"], "reason": "risk"}
    state = {"route": route, "candidate_decisions": [
        {"candidate_id": "existing", "status": "needs_review"},
        {"candidate_id": "rewrite", "status": "rewrite"},
        {"candidate_id": "rejected", "status": "reject"},
    ]}
    Pipeline._handoff(pipeline, "t", state, route)
    assert state["route"]["pending_review_candidate_ids"] == ["rewrite"]
    assert state["route"]["action"] == "stop"
    assert pipeline.storage.checkpoints[0][1] == "stopped"


def test_empty_surface_backfill_deduplicates_and_keeps_reject_out():
    pipeline = PipelineStub()
    route = {"action": "needs_review", "eligible_candidate_ids": []}
    state = {"route": route, "candidate_decisions": [
        {"candidate_id": "ready", "status": "ready_for_human_review"},
        {"candidate_id": "ready", "status": "ready_for_human_review"},
        {"candidate_id": "rewrite", "status": "rewrite"},
        {"candidate_id": "risk", "status": "reject"},
    ]}
    Pipeline._handoff(pipeline, "t", state, route)
    assert state["route"]["pending_review_candidate_ids"] == ["ready", "rewrite"]
    assert state["route"]["action"] == "needs_review"
