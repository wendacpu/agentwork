"""R01 评审载荷裁剪的回归测试。

背景：R01 拿到的是上游所有节点的完整输出转储（实测 72,236 字符），其中
`evidence` 68 条里 36 条没被任何结论引用，`risk_signals` 65 条每条重复同一组
常量字段。裁剪只去掉没进入判断链路的部分，被引用的一律保留。
"""

from __future__ import annotations

import json
import unittest

from agent_system.pipeline import _compact_review_payload


def evidence(eid: str, text: str = "材料", quality: str = "raw_asr") -> dict:
    return {"evidence_id": eid, "field": "asr", "text": text, "quality": quality,
            "locator": "第 1 秒", "derived_from": []}


def signal(sid: str, eid: str) -> dict:
    return {"signal_id": sid, "signal_type": "number", "value": "5", "source_field": "asr",
            "evidence_id": eid, "span": [1, 2], "quality": "raw_ocr", "polarity": "positive",
            "target": "unknown", "status": "observation", "context": "很长的推断文本",
            "extractor_version": "risk-signals-v1"}


def payload(ev_list: list[dict], sig_list: list[dict], *, cite: list[str], source_quality: str = "source") -> dict:
    return {
        "evidence": ev_list,
        "risk_signals": {"signals": sig_list, "extractor_version": "risk-signals-v1"},
        "candidate_titles": [{"candidate_id": "cand-1", "evidence_ids": cite}],
    }


class CompactReviewPayloadTests(unittest.TestCase):
    def test_unreferenced_evidence_is_dropped(self) -> None:
        ev = [evidence("e-asr-0000"), evidence("e-asr-0001"), evidence("e-asr-0002")]
        out = _compact_review_payload(payload(ev, [], cite=["e-asr-0000"]))
        self.assertEqual([e["evidence_id"] for e in out["evidence"]], ["e-asr-0000"])
        self.assertEqual(out["_compaction"]["evidence"], [3, 1])

    def test_primary_material_is_kept_without_reference(self) -> None:
        """一手材料（原题/简介/字幕原文）是事实底座，不因没人引用就丢。"""
        ev = [evidence("e-title", quality="source"), evidence("e-asr-0000"), evidence("e-asr-0009")]
        out = _compact_review_payload(payload(ev, [], cite=["e-asr-0000"]))
        ids = {e["evidence_id"] for e in out["evidence"]}
        self.assertEqual(ids, {"e-title", "e-asr-0000"})

    def test_evidence_fields_are_trimmed_to_what_review_reads(self) -> None:
        ev = [evidence("e-asr-0000")]
        out = _compact_review_payload(payload(ev, [], cite=["e-asr-0000"]))
        self.assertEqual(set(out["evidence"][0]), {"evidence_id", "field", "text", "quality", "locator"})

    def test_signals_keep_only_decision_fields(self) -> None:
        ev = [evidence("e-asr-0000")]
        out = _compact_review_payload(payload(ev, [signal("s1", "e-asr-0000")], cite=["e-asr-0000"]))
        kept = out["risk_signals"]["signals"][0]
        self.assertEqual(set(kept), {"signal_id", "signal_type", "value", "quality", "evidence_id"})
        self.assertNotIn("extractor_version", out["risk_signals"])

    def test_signals_are_not_dropped_merely_because_evidence_is_filtered(self) -> None:
        """信号是判断「标题里的数字有没有出处」的线索，保留全部条目，只删冗余字段。"""
        ev = [evidence("e-asr-0000"), evidence("e-asr-0009")]
        sigs = [signal("s1", "e-asr-0000"), signal("s2", "e-asr-0009")]
        out = _compact_review_payload(payload(ev, sigs, cite=["e-asr-0000"]))
        self.assertEqual(len(out["risk_signals"]["signals"]), 2)

    def test_payload_without_references_is_returned_intact(self) -> None:
        """闭包为空时不裁成空壳——宁可多传也不让评审看不见证据。"""
        ev = [evidence("e-asr-0000"), evidence("e-asr-0001")]
        original = payload(ev, [], cite=[])
        self.assertIs(_compact_review_payload(original), original)

    def test_empty_evidence_is_returned_intact(self) -> None:
        original = {"evidence": [], "risk_signals": {"signals": []}, "candidate_titles": []}
        self.assertIs(_compact_review_payload(original), original)

    def test_malformed_items_do_not_crash(self) -> None:
        original = {"evidence": [{"no_id": 1}, "字符串", None],
                    "risk_signals": {"signals": [{"signal_id": "s1"}]}, "candidate_titles": []}
        out = _compact_review_payload(original)
        self.assertEqual(out, original)

    def test_compaction_shrinks_a_realistic_payload(self) -> None:
        ev = [evidence(f"e-asr-{i:04d}") for i in range(20)]
        sigs = [signal(f"s{i}", f"e-asr-{i:04d}") for i in range(20)]
        original = payload(ev, sigs, cite=["e-asr-0000", "e-asr-0001"])
        before = len(json.dumps(original, ensure_ascii=False))
        after = len(json.dumps(_compact_review_payload(original), ensure_ascii=False))
        self.assertLess(after, before)

    def test_closure_catches_references_outside_candidate_list(self) -> None:
        """boundary/highlights/claims 里引用的证据同样要保留，不能只看候选。"""
        ev = [evidence("e-asr-0000"), evidence("e-asr-0005"), evidence("e-asr-0009")]
        p = payload(ev, [], cite=[])
        p["boundary"] = {"decisions": [{"evidence_ids": ["e-asr-0005"]}]}
        out = _compact_review_payload(p)
        self.assertIn("e-asr-0005", {e["evidence_id"] for e in out["evidence"]})


if __name__ == "__main__":
    unittest.main()
