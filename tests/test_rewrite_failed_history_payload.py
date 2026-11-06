"""重写载荷携带同任务历史失败标题，阻断换皮重复。"""
import hashlib
import json

import pytest
from agent_system.pipeline import Pipeline


def test_rewrite_payload_includes_failed_sibling_titles(monkeypatch):
    """BV1Bgxw6eEMn 教训：12 条变体重写全带「封神」——重写看不到兄弟候选被拒记录。"""
    calls = []
    p = Pipeline.__new__(Pipeline)
    p.config = type('C', (), {'memory_call_limit': 0, 'max_chars': 20, 'extra': {},
                              'rewrite_candidate_count': 2, 'target_count': 3})()
    p.storage = type('S', (), {'event': lambda *a, **k: None, 'events': lambda *a, **k: []})()
    def fake_call(task_id, node, name, user, schema, adapter=None, rules=None):
        calls.append(user)
        return {"candidates": [], "shortfall_reason": "无法在保留主旨的同时修复该缺陷"}
    p._call = fake_call
    video = type('V', (), {'title': 't', 'author': 'a', 'format': 'f', 'domain': {}, 'video_id': 'v',
                           'evidence': [], 'description': '', 'caption': '', 'asr_segments': [], 'ocr_segments': [],
                           'provenance': {}, 'attributes': {}})()
    understanding = type('U', (), {'claims': [], 'semantic_units': [], 'events': [], 'turns': [], 'attributes': []})()
    boundary = type('B', (), {'decisions': [], 'unit_decisions': [], 'unresolved_issues': []})()
    source = {"candidate_id": "cand-1", "headline": "博主自评间奏封神", "rendered_title": "博主自评间奏封神",
              "slot_id": "core_summary", "thesis_id": "t1", "highlight_id": "h1", "claim_ids": []}
    state = {
        "reviews": [
            {"candidate_id": "cand-2", "issues": [{"issue_code": "G1_UNSUPPORTED_FACT", "issue_id": "i1"}]},
            {"candidate_id": "cand-1", "issues": [{"issue_code": "G1_UNSUPPORTED_FACT", "issue_id": "i0"}]},
        ],
        "business_policy": {"status": "approved", "rules": [{"rule_id": "P1"}]},
        "run_manifest": {}, "prompt_hashes": {}, "rewrite_round": 0,
    }
    known = [
        {"candidate_id": "cand-1", "rendered_title": "博主自评间奏封神"},
        {"candidate_id": "cand-2", "rendered_title": "博主弹钢琴自评封神"},
    ]
    highlights = [type('H', (), {'highlight_id': 'h1', 'role': 'main', 'unit_id': ''})()]
    feedback = {'task_id': 'task-1', 'candidate_id': 'cand-1', 'title_hash': hashlib.sha256(source['rendered_title'].encode()).hexdigest(), 'reason': '不能自称封神'}
    state['targeted_rewrite_feedback'] = feedback
    result = p._generate_and_review("task-1", state, video, understanding, boundary, highlights,
                                    1, source, known_candidates=known)
    payload = calls[-1]
    assert payload['human_rewrite_feedback'] == feedback
    monkeypatch.setattr('agent_system.pipeline.to_dict', lambda x: [] if isinstance(x, list) else x if isinstance(x, (dict, str, int, float, bool, type(None))) else {})
    p._generate_and_review('task-1', state, video, understanding, boundary, highlights, 1, source, repair_mode='压缩', known_candidates=known)
    assert calls[-1]['human_rewrite_feedback'] == feedback
    state['targeted_rewrite_feedback'] = {**feedback, 'title_hash': 'wrong'}
    with pytest.raises(ValueError, match='身份不一致'):
        p._generate_and_review('task-1', state, video, understanding, boundary, highlights, 1, source, known_candidates=known)
    failed = payload.get("failed_sibling_titles") or []
    assert any(x["title"] == "博主弹钢琴自评封神" and "G1_UNSUPPORTED_FACT" in x["issues"] for x in failed)
    assert not any(x.get("title", "").startswith("博主自评间奏封神") for x in failed), "父候选自身不应混入兄弟清单"
