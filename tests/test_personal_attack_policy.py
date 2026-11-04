"""业务规则 P9：人身攻击用语不得直接进标题（2026-11-02 业务方新增）。

触发点是 BV1u9HA6JEdw（黄多多红毯采访）——视频顶部黑条写着「网友：她真的很欠扁样子」，
几轮生成里有候选把「网友称她欠扁」直接写进了标题。这条评价本身是视频的对照素材，
但不等于可以把攻击词复制进标题替它传播。

这组测试锁四件事：
1. 词表命中攻击词，但不误伤有正常义的词（垃圾、废物、白莲花、装备、装死）；
2. 转述结构（网友称她欠扁）照样命中，否定结构（她不欠扁）不命中；
3. 归属在业务安全底线 G2 这一关（与 P1–P8 同一张处置动作表），不是独立的 G4 评审项，
   所以它不出现在 G4 errors 里，处置动作由 business_policy 的 action 决定；
4. 规则文本同时落在 rulepacks/common.md（模型判断依据）和 business_policy（处置动作），
   且 U-V04 明确写了它是 U-W01「不把任何一个词当违规词」的例外。
"""
import json
import unittest
from pathlib import Path

from agent_system.abuse import attack_rewrite_hint, find_personal_attacks
from agent_system.pipeline import _decide_candidate, _mechanical_check

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "config.business-policy.json"
COMMON = ROOT / "rulepacks" / "common.md"


def _candidate(headline: str, prefix_labels: list[str] | None = None) -> dict:
    return {"candidate_id": "a1-1", "headline": headline, "prefix_labels": list(prefix_labels or []),
            "claim_ids": ["c1"], "highlight_id": "h1", "evidence_ids": ["e1"], "unit_id": None,
            "rendered_title": headline}


def _check(headline: str, prefix_labels: list[str] | None = None) -> dict:
    return _mechanical_check(_candidate(headline, prefix_labels), allowed_claims={"c1"},
                             allowed_highlights={"h1"}, evidence_ids={"e1"}, max_chars=20)


def _review(candidate_id: str = "a1-1") -> dict:
    return {"candidate_id": candidate_id,
            "gates": {key: {"status": "pass"} for key in ("G1", "G2", "G3")},
            "quality": {key: {"score": 3} for key in ("Q1", "Q2", "Q3")},
            "issues": []}


APPROVED = {"status": "approved", "version": "policy-v0.2", "rules": [{"rule_id": "P9", "action": "rewrite"}]}


class TermTableTests(unittest.TestCase):
    def test_plain_attack_term_is_caught(self):
        self.assertEqual(find_personal_attacks("黄多多很欠扁"), ["欠扁"])

    def test_relayed_quote_still_counts(self):
        """转述不改事实：读者在标题里看到的就是那个词。"""
        self.assertEqual(find_personal_attacks("网友称她欠扁"), ["欠扁"])
        self.assertEqual(find_personal_attacks("网友评她欠扁"), ["欠扁"])

    def test_negation_is_not_caught(self):
        self.assertEqual(find_personal_attacks("她不欠扁"), [])

    def test_words_with_normal_meanings_are_not_in_the_table(self):
        for title in ("垃圾分类教程", "废物利用手工", "白莲花怎么养", "装备开箱", "装死挑战",
                      "有病要就医", "猪头肉做法", "恶心是药物副作用"):
            self.assertEqual(find_personal_attacks(title), [], f"{title} 不该被词表命中")

    def test_english_terms_are_case_insensitive(self):
        self.assertEqual(find_personal_attacks("LOSER逆袭"), ["loser"])
        self.assertEqual(find_personal_attacks("loser逆袭"), ["loser"])

    def test_hint_tells_what_to_write_instead(self):
        hint = attack_rewrite_hint(["欠扁"])
        self.assertIn("网友恶评", hint)
        self.assertIn("P9", hint)


class MechanicalGateTests(unittest.TestCase):
    def test_attack_term_is_not_a_g4_item(self):
        """人身攻击不是一个独立的评审维度：它属于业务安全底线（G2），不该出现在 G4。"""
        result = _check("网友称她欠扁")
        self.assertEqual([e["code"] for e in result["errors"]], [], "G4 只管格式与引用，不含内容违规")
        self.assertEqual(result["status"], "pass")
        hits = result["policy_hits"]
        self.assertEqual([h["rule_id"] for h in hits], ["P9"])
        self.assertEqual(hits[0]["code"], "G2_POLICY_VIOLATION")
        self.assertEqual(hits[0]["terms"], ["欠扁"])

    def test_prefix_is_checked_too(self):
        result = _check("网友称她欠扁", prefix_labels=["剧情演绎"])
        self.assertEqual([h["rule_id"] for h in result["policy_hits"]], ["P9"])

    def test_rewritten_title_has_no_policy_hit(self):
        result = _check("黄多多红毯受访，网友恶评")
        self.assertEqual(result["policy_hits"], [])
        self.assertEqual(result["status"], "pass")

    def test_decision_is_rewrite_through_the_policy_action_table(self):
        """处置动作走 business_policy 的 action 表，和 P1–P8 同一条路。"""
        decision = _decide_candidate(_review(), _check("网友称她欠扁"), schema_errors=[], business_policy=APPROVED)
        self.assertEqual(decision["status"], "rewrite")
        self.assertEqual(decision["reason"], "POLICY_ACTION_REWRITE")
        self.assertEqual(decision["policy_action"], "rewrite")
        g2 = decision["gates"]["G2"]
        self.assertEqual(g2["status"], "revise", "模型可能因为「只是转述网友」放行，程序必须把它拉回非通过")
        self.assertIn("P9", g2["rule_ids"])
        self.assertEqual([h["rule_id"] for h in g2["program_hits"]], ["P9"])

    def test_model_saying_g2_pass_does_not_let_it_through(self):
        review = _review()
        review["gates"]["G2"] = {"status": "pass", "reason": "未发现违规", "rule_ids": []}
        decision = _decide_candidate(review, _check("网友称她欠扁"), schema_errors=[], business_policy=APPROVED)
        self.assertNotEqual(decision["status"], "ready_for_human_review")
        self.assertEqual(decision["gates"]["G2"]["status"], "revise")

    def test_checked_rule_list_does_not_decide_the_action(self):
        """G2=pass 时列出的 P1–P8 是「检查过哪些」，不是「违反了哪些」：
        拿它算最严动作会把一条 P9（rewrite）顶成 P1 的 reject。真实运行里就是这个形状。"""
        review = _review()
        review["gates"]["G2"] = {"status": "pass", "reason": "已逐条核对，未发现违规",
                                 "rule_ids": ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]}
        decision = _decide_candidate(review, _check("网友称她欠扁"), schema_errors=[], business_policy=APPROVED)
        self.assertEqual(decision["policy_action"], "rewrite", "只有 P9 被违反，动作应为 rewrite")
        self.assertEqual(decision["status"], "rewrite")
        self.assertEqual(decision["gates"]["G2"]["violated_rule_ids"], ["P9"])

    def test_policy_action_table_is_the_only_verdict_source(self):
        """P9 的 action 是这条链上唯一的裁决依据：改成 reject 就判毙，改成 review 就转人审。"""
        for action, expected, reason in (("reject", "reject", "POLICY_ACTION_REJECT"),
                                         ("review", "needs_review", "POLICY_ACTION_REVIEW")):
            policy = {"status": "approved", "rules": [{"rule_id": "P1", "action": "reject"}, {"rule_id": "P9", "action": action}]}
            decision = _decide_candidate(_review(), _check("网友称她欠扁"), schema_errors=[], business_policy=policy)
            self.assertEqual((decision["status"], decision["reason"]), (expected, reason), action)

    def test_stricter_model_status_is_kept(self):
        """模型已经判 reject 时不能被程序降级成 revise。"""
        review = _review()
        review["gates"]["G2"] = {"status": "reject", "reason": "明确违规", "rule_ids": ["P9"]}
        decision = _decide_candidate(review, _check("网友称她欠扁"), schema_errors=[], business_policy=APPROVED)
        self.assertEqual(decision["gates"]["G2"]["status"], "reject")
        self.assertEqual(decision["status"], "reject")

    def test_policy_with_reject_action_rejects(self):
        """动作由策略表决定：改成 reject 就该判毙，程序不写死结论文案。"""
        policy = {"status": "approved", "rules": [{"rule_id": "P9", "action": "reject"}]}
        decision = _decide_candidate(_review(), _check("网友称她欠扁"), schema_errors=[], business_policy=policy)
        self.assertEqual(decision["status"], "reject")
        self.assertEqual(decision["reason"], "POLICY_ACTION_REJECT")

    def test_missing_policy_still_holds_for_review(self):
        """策略没加载时既有契约是「转人审」（U-V03），这比自动改写更保守，不推翻它。"""
        decision = _decide_candidate(_review(), _check("网友称她欠扁"), schema_errors=[], business_policy={"status": "missing", "rules": []})
        self.assertEqual(decision["status"], "needs_review")
        self.assertNotEqual(decision["status"], "ready_for_human_review")

    def test_policy_without_p9_does_not_pass_either(self):
        """策略表里没有 P9（旧配置）也不能放行：退回可改写，绝不 ready。"""
        policy = {"status": "approved", "rules": [{"rule_id": "P1", "action": "reject"}]}
        decision = _decide_candidate(_review(), _check("网友称她欠扁"), schema_errors=[], business_policy=policy)
        self.assertNotEqual(decision["status"], "ready_for_human_review")
        self.assertIn("P9", decision["gates"]["G2"]["rule_ids"])

    def test_policy_hit_keeps_candidate_out_of_the_compression_channel(self):
        """压缩只砍字、不会换词，命中政策必须走语义重写。"""
        from agent_system.pipeline import _compressible_defects
        mechanical = _check("网友称她欠扁" * 4)
        self.assertTrue(any(e["code"] == "G4_LENGTH_EXCEEDED" for e in mechanical["errors"]))
        candidate = {"mechanical_checks": mechanical}
        decision = {"status": "rewrite"}
        self.assertEqual(_compressible_defects(candidate, decision, _review()), [])
        mechanical_clean = _check("网友称她欠扁" * 4)
        mechanical_clean["policy_hits"] = []
        self.assertTrue(_compressible_defects({"mechanical_checks": mechanical_clean}, decision, _review()))


class RuleTextTests(unittest.TestCase):
    def test_p9_is_in_the_approved_policy(self):
        policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))["extra"]["business_policy"]
        rule = next((r for r in policy["rules"] if r.get("rule_id") == "P9"), None)
        self.assertIsNotNone(rule, "业务规则 P9 必须写在权威策略文件里")
        self.assertEqual(rule["action"], "rewrite")
        self.assertIn("人身攻击", rule["name"])
        self.assertIn("转述", rule["text"], "规则正文必须写清转述也算出现")

    def test_common_rulepack_carries_the_judging_rule(self):
        text = COMMON.read_text(encoding="utf-8")
        self.assertIn("U-V04", text)
        self.assertIn("人身攻击", text)
        self.assertIn("转述", text, "规则正文要让模型知道转述也算")

    def test_common_rulepack_declares_the_exception_explicitly(self):
        """U-W01 说「不把任何一个词自动当作违规词」，P9 是例外——不写清就是自相矛盾。"""
        text = COMMON.read_text(encoding="utf-8")
        w01 = next(line for line in text.splitlines() if line.startswith("U-W01 "))
        self.assertIn("U-V04", w01, "U-W01 必须点名人身攻击用语是例外")
        v01 = next(line for line in text.splitlines() if line.startswith("U-V01 "))
        self.assertIn("例外", v01)

    def test_every_active_config_carries_p9(self):
        """策略内联在各运行配置里，漏同步就等于这次改动在那些配置上等于没做。"""
        missing = []
        for path in sorted(ROOT.glob("config*.json")):
            try:
                extra = json.loads(path.read_text(encoding="utf-8")).get("extra") or {}
            except Exception:
                continue
            rules = (extra.get("business_policy") or {}).get("rules") or []
            ids = {str(r.get("rule_id")) for r in rules if isinstance(r, dict)}
            # 只查带完整策略（P1-P8）的配置；policy-v0.1-draft 那几个验证实验配置本来就只有 3 条。
            if {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"} <= ids and "P9" not in ids:
                missing.append(path.name)
        self.assertEqual(missing, [], f"这些配置缺 P9：{missing}")


if __name__ == "__main__":
    unittest.main()
