"""规则 ID 解析回归：common.md 里的字母后缀规则必须进入合法 ID 表。

背景：U-B07a 等 5 条规则写在 rulepacks/common.md 里，但旧正则只认 U-[A-Z]+两位数字，
导致评审引用合法规则时被判 unknown，对题候选被误转 needs_review。
"""
from __future__ import annotations

from agent_system.rules import load_common_rule_ids


def test_common_rule_ids_include_letter_suffix_variants():
    ids = set(load_common_rule_ids())
    for rule_id in ("U-B03a", "U-B07a", "U-B07b", "U-B07c", "U-B07d"):
        assert rule_id in ids, f"{rule_id} 未进入合法规则 ID 表"


def test_common_rule_ids_keep_plain_base_ids():
    ids = set(load_common_rule_ids())
    for rule_id in ("U-B01", "U-S10", "U-W05"):
        assert rule_id in ids


def test_suffix_rule_coexists_with_base_rule():
    ids = load_common_rule_ids()
    # 后缀规则必须以其完整 ID 出现，同时保留同名基础规则，二者不互相吞掉
    assert "U-B07a" in ids and "U-B07" in ids
    assert len([x for x in ids if x == "U-B07"]) == 1
