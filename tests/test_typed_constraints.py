"""限定分类的回归：背景非义务，条件出现时仍须核实。"""
import pytest

from agent_system.pipeline import _mechanical_check, _normalize_boundary, _decide_candidate


def test_boundary_preserves_three_classes_and_date_scope():
    raw = {"decisions": [{"claim_id": "c1", "decision": "limited", "required_limits": [],
        "conditional_constraints": ["10月28日为报道日期，非事件发生日期"],
        "factual_context": ["南非开普敦纽兰兹球场"], "forbidden_inferences": []}]}
    boundary = _normalize_boundary(raw)
    assert boundary.decisions[0].factual_context == ["南非开普敦纽兰兹球场"]
    assert boundary.decisions[0].conditional_constraints == ["10月28日为报道日期，非事件发生日期"]
    def check(title):
        return _mechanical_check({"candidate_id":"c1", "headline":title, "claim_ids":["c1"],
                                  "evidence_ids":["e1"], "highlight_id":"h1"},
            allowed_claims={"c1"}, allowed_highlights={"h1"}, evidence_ids={"e1"},
            max_chars=40, claim_decisions=boundary.decisions)
    no_date = check("南非两员工偷看球赛被大屏抓包")
    assert not no_date["limit_pending"]
    assert not any(x["code"] == "G1_MISSING_LIMIT" for x in no_date["errors"])
    wrongly_dated = check("10月28日南非两员工上班偷看球赛")
    assert wrongly_dated["limit_pending"]
    result = _decide_candidate({"candidate_id":"c1"}, wrongly_dated,
                               schema_errors=["invalid"], business_policy={})
    assert result["status"] == "needs_review"
    for relative in ("前天", "周一", "上个月"):
        assert check(relative + "南非两员工偷看球赛")["limit_pending"]


@pytest.mark.parametrize("title,expected_pending", [
    ("博主演示白菜不爆炒：加蒜末盖盖焖", False),
    ("博主演示白菜盖盖焖三分钟", True),
    ("白菜焖约3分钟", True),
    ("白菜焖一会儿", True),
    ("白菜很快焖好", True),
    ("白菜十来分钟做好", True),
    ("白菜转眼就熟", True),
])
def test_conditional_duration_not_required_when_unexpressed(title, expected_pending):
    boundary = _normalize_boundary({"decisions": [{"claim_id": "c6", "decision": "limited",
        "required_limits": ["焖三分钟为口播所述步骤时长，ASR存在疑似误转，标题若写须保留「博主称」归属，不写成客观实测时长"],
        "conditional_constraints": ["若标题写具体时长，须归属为博主所述，不得写成系统确认的烹饪时长"]}]})
    result = _mechanical_check({"candidate_id": "x", "headline": title, "claim_ids": ["c6"],
        "evidence_ids": ["e1"], "highlight_id": "h1"}, allowed_claims={"c6"},
        allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=40, claim_decisions=boundary.decisions)
    assert bool(result["limit_pending"]) is expected_pending
    if not expected_pending:
        assert not any(error["code"] == "G1_MISSING_LIMIT" for error in result["errors"])


def test_independent_obligation_is_not_exempted_by_duration_condition():
    boundary = _normalize_boundary({"decisions": [{"claim_id": "c1", "decision": "limited",
        "conditional_constraints": ["若标题写具体时长须归属；同时须写明安全风险"]}]})
    result = _mechanical_check({"candidate_id": "x", "headline": "白菜盖盖焖", "claim_ids": ["c1"],
        "evidence_ids": ["e1"], "highlight_id": "h1"}, allowed_claims={"c1"},
        allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=40, claim_decisions=boundary.decisions)
    assert result["limit_pending"]


def test_comma_separated_independent_obligation_remains_pending():
    from agent_system.pipeline import _conditional_duration_absent
    assert not _conditional_duration_absent("标题若写时长须注明为估算，并须写明未经证实", "这道菜做好了")


def _cond_pending(title, conditional):
    from types import SimpleNamespace
    decision = SimpleNamespace(claim_id="c1", decision="limited", required_limits=[],
                               conditional_constraints=[conditional])
    return _mechanical_check({"candidate_id": "x", "headline": title, "claim_ids": ["c1"],
        "evidence_ids": ["e1"], "highlight_id": "h1"}, allowed_claims={"c1"},
        allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=40, claim_decisions=[decision])["limit_pending"]


def test_conditional_not_triggered_by_absent_quoted_phrase():
    assert not _cond_pending("二仙桥大爷再被拦，谭乔证实此次未罚款",
                             "若标题写“网络人物”，须以视频口播/字幕条所述名场面为背景，不得推断其职业身份")


def test_conditional_triggered_and_satisfied_anchor():
    assert not _cond_pending("二仙桥大爷再被拦，谭乔证实此次未罚款",
                             "若标题写“没有罚款”，须与“此次”限定同现，不得扩展为所有类似事件均不罚款")


def test_conditional_without_quoted_trigger_stays_pending():
    """「若标题涉及具体处置情况…」无法字面证伪，不能因为标题没写某个词就放行。"""
    assert _cond_pending("二仙桥大爷再被拦，随后交警未开罚单",
                         "若标题涉及具体处置情况，须与“还需进一步核实”同现，不得写成确定事实")


def test_conditional_with_unknown_trigger_phrase_stays_pending():
    assert _cond_pending("白菜少油焖三分钟更健康", "若标题写“少油少盐更健康”，须保留“博主称”归属")


def test_conditional_attribution_satisfied_by_real_forms():
    """真实复测里的三种写法：赛后称、博主冒号、以及触发说法根本没写。"""
    assert not _cond_pending("萨戈夫赛后称不认为自己强于德文",
                             "标题写到时须保留归属为萨戈夫本人的表态，不得写成系统确认萨戈夫强于或弱于德文")
    assert not _cond_pending("博主：财经快讯误导性强，投资需核实",
                             "标题写到时须保留观点归属，如「博主认为」或「博主称」")
    assert not _cond_pending("博主比对快讯与原文：报道避重就轻",
                             "若标题写「马斯克回复Chamath关于新云公司整合」，须保留「AI整理称」归属")


def test_legacy_missing_fields_preserve_positive_limit():
    boundary = _normalize_boundary({"decisions": [{"claim_id":"c1", "decision":"limited", "required_limits":["须写明时间范围"]}]})
    assert boundary.decisions[0].required_limits == ["须写明时间范围"]
    assert boundary.decisions[0].conditional_constraints == []


def test_nested_duplicate_claims_keep_constraints_from_both_layers():
    raw = {"decisions": [{"claim_id": "c1", "decision": "usable"}],
           "boundary_set": {"decisions": [{"claim_id": "c1", "decision": "limited",
               "conditional_constraints": ["不得把报道日期当作事发日期"],
               "factual_context": ["背景细节"]}]}}
    boundary = _normalize_boundary(raw)
    assert boundary.decisions[0].conditional_constraints == ["不得把报道日期当作事发日期"]
    assert boundary.decisions[0].factual_context == ["背景细节"]


def test_old_limit_alias_is_merged_across_layers():
    raw = {"decisions": [{"claim_id":"c1", "decision":"limited", "required_limits":["须注明范围"]}],
           "boundary_set": {"decisions": [{"claim_id":"c1", "decision":"limited", "limits":["须保留归属"]}]}}
    assert _normalize_boundary(raw).decisions[0].required_limits == ["须注明范围", "须保留归属"]
