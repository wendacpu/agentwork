"""各族调查任务独立验收：真实定位、条件改动、测试规格与发布边界。"""
import ast
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_system.repair_playbooks import FAMILIES, build_work_items, playbook


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_has_complete_actionable_playbook(family):
    result = playbook(family)
    assert result["family"] == family
    assert result["objective"]
    assert result["status"] == "hypothesis"
    assert result["first_failure"] == result["escaped_defense"] == "unconfirmed"
    assert result["execution_mode"] == "manual_plan_only"
    assert result["release_eligible"] is False
    assert len(result["target_locations"]) >= 3
    assert {step["phase"] for step in result["diagnostic_steps"]} == {"first_failure", "escaped_defense"}
    for step in result["diagnostic_steps"]:
        assert step["action"] and step["required_evidence"]
        assert step["conclusion_status"] == "unconfirmed"
    for change in result["change_options"]:
        assert all(change[key] for key in ("trigger", "change", "do_not"))
        assert change["trigger_status"] == "unconfirmed"
    assert {case["kind"] for case in result["test_cases"]} >= {"positive", "negative", "variant"}
    for case in result["test_cases"]:
        assert case["scenario"] and case["expected"]
        assert case["truth_status"] == "synthetic_test_spec_not_observed_result"
    acceptance = result["acceptance"]
    assert set(acceptance) >= {"functional", "evidence", "regression", "false_positive", "human_burden", "measurement", "release"}
    assert "样本数" in acceptance["false_positive"]
    assert "分钟" in acceptance["human_burden"]
    assert "分母" in acceptance["measurement"]
    json.dumps(result, ensure_ascii=False)


def _symbols(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                names.update(f"{node.name}.{child.name}" for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


@pytest.mark.parametrize("family", FAMILIES)
def test_each_target_names_existing_file_and_symbol(family):
    locations = playbook(family)["target_locations"]
    files = {location["file"] for location in locations}
    assert {"agent_system/pipeline.py", "agent_system/prompts.py"} <= files
    if family in {"content_nature", "numeric", "constraint", "unresolved"}:
        assert "agent_system/signals.py" in files
    for location in locations:
        target = ROOT / location["file"]
        assert target.is_file()
        assert location["symbol"] in _symbols(ast.parse(target.read_text(encoding="utf-8")))
        assert location["role"]


@pytest.mark.parametrize("family,needles", [
    ("content_nature", ["否定", "讨论", "pending_signal_ids", "前缀", "重审"]),
    ("numeric", ["单位", "总价", "单价", "问句", "原题", "ASR", "实体"]),
    ("selection", ["主旨", "core", "边角", "fallback", "观看承诺"]),
    ("constraint", ["同义", "限定", "归属", "日期", "qualifiers_used"]),
    ("expression", ["名词性", "自然省略", "真实信源", "主谓宾", "因果"]),
    ("unresolved", ["补证", "历史", "未知", "不自动", "final_headline"]),
])
def test_each_family_contains_its_specific_investigation_and_controls(family, needles):
    text = json.dumps(playbook(family), ensure_ascii=False)
    assert all(needle in text for needle in needles)


@pytest.mark.parametrize("family", ["unknown", "numeric_typo", "", None, 42, ["numeric"]])
def test_unknown_family_never_has_release_eligibility(family):
    result = playbook(family)
    assert result["family"] == "unresolved"
    assert result["release_eligible"] is False
    assert result["first_failure"] == "unconfirmed"
    assert result["escaped_defense"] == "unconfirmed"
    for item in build_work_items(family, {}):
        assert item["family"] == "unresolved"
        assert item["release_eligible"] is False


def payload():
    return {
        "task_id": "task-88",
        "candidate": {"candidate_id": "candidate-3", "headline": "顾客花872元", "evidence_ids": ["e1", "absent"], "claim_ids": ["c1"]},
        "evidence_refs": ["direct"],
        "decision": {"decision": "rejected", "reason": "数字待核实", "final_headline": "人工改写不是事实真值"},
        "snapshot": {
            "video": {"evidence": [{"evidence_id": "e1", "text": "总价800元"}, {"evidence_id": "e2", "text": "800元吗"}, {"evidence_id": "other", "text": "无关材料"}]},
            "understanding": {"claims": [{"claim_id": "c1", "evidence_refs": ["e2", "e1"]}, {"claim_id": "other", "evidence_refs": ["other"]}]},
        },
    }


@pytest.mark.parametrize("family", FAMILIES)
def test_work_items_embed_concrete_task_candidate_and_claim_evidence(family):
    source = payload()
    before = deepcopy(source)
    result = build_work_items(family, source)
    assert source == before
    assert {item["kind"] for item in result} == {"diagnostic", "conditional_change", "regression", "acceptance"}
    assert len({item["work_item_id"] for item in result}) == len(result)
    for item in result:
        assert item["task_id"] == "task-88"
        assert item["candidate_id"] == "candidate-3"
        assert item["candidate"] == source["candidate"]
        assert item["evidence_refs"] == ["direct", "e1", "absent", "e2"]
        assert item["unresolved_evidence_refs"] == ["direct", "absent"]
        assert [e["evidence_id"] for e in item["evidence"]] == ["e1", "e2"]
        assert item["human_feedback"] == source["decision"]
        assert item["expected_revision_is_ground_truth"] is False
        assert item["release_eligible"] is False
        assert item["missing_context"] == []
    json.dumps(result, ensure_ascii=False)


def test_missing_snapshot_and_reference_aliases_are_explicit():
    source = {"candidate": {"evidence_refs": "e-one", "claim_ids": "c-one"}, "snapshot": {"understanding": {"candidate_claims": [{"claim_id": "c-one", "evidence_refs": "e-two"}]}}}
    item = build_work_items("numeric", source)[0]
    assert item["evidence_refs"] == ["e-one", "e-two"]
    assert item["unresolved_evidence_refs"] == ["e-one", "e-two"]
    assert item["evidence"] == []
    assert "snapshot.video.evidence" in item["missing_context"]
    assert "task_id" in item["missing_context"]
    assert build_work_items("unresolved", None)[0]["missing_context"]


def test_results_are_independent_and_input_cannot_grant_release():
    first = playbook("numeric")
    first["change_options"][0]["trigger"] = "污染模板"
    assert playbook("numeric")["change_options"][0]["trigger"] != "污染模板"
    source = payload()
    source.update(release_eligible=True, first_failure="confirmed", status="approved")
    result = build_work_items("numeric", source)
    result[0]["candidate"]["headline"] = "污染候选"
    result[0]["target_locations"][0]["symbol"] = "污染定位"
    assert result[1]["candidate"] == source["candidate"]
    assert result[1]["target_locations"] == playbook("numeric")["target_locations"]
    assert all(item["release_eligible"] is False and item["first_failure"] == "unconfirmed" for item in result)


def test_building_plan_does_not_execute_commands_or_network():
    source = payload()
    source["candidate"]["headline"] = "__import__('os').system('must-not-run')"
    with patch("subprocess.run", side_effect=AssertionError("不运行命令")), \
         patch("os.system", side_effect=AssertionError("不运行代码")), \
         patch("socket.create_connection", side_effect=AssertionError("不访问网络")), \
         patch("builtins.open", side_effect=AssertionError("不访问文件")):
        result = build_work_items("content_nature", source)
    assert result[0]["candidate"]["headline"] == source["candidate"]["headline"]
