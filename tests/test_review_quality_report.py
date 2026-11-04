"""评审质量报告的口径测试。

这里锁的是**分母与配对**，因为它们是上一轮验收出错的地方：

1. 未产出的视频必须留在分母里（缩分母会让成功率凭空变高）；
2. 严格「可用」不含「勉强通过」（合并会把改判当成质量提升）；
3. 误拦与误放分开算（只报一头会掩盖另一头）。
"""
import unittest

from scripts.review_quality_report import bucketize, compute


def _video(blind_id, *candidates, basis="first_round"):
    return {"task_id": f"t-{blind_id}", "blind_id": blind_id, "category": "测试",
            "round_basis": basis, "candidates": list(candidates)}


def _cand(cid, verdict, status, system_reason="", legacy=False, mechanical=False):
    return {"candidate_id": cid, "title": f"标题{cid}", "status": status, "verdict": verdict,
            "legacy": legacy, "reason": "", "system_reason": system_reason,
            "gates": {}, "mechanical_blocked": mechanical}


class MetricTests(unittest.TestCase):
    def test_unproduced_video_stays_in_denominator(self):
        """没产出标题的视频计失败，不从分母里消失。"""
        rows = [
            _video("R01", _cand("c1", "approved", "recommended")),
            _video("R02", _cand("c2", "rejected", "rewrite")),
            _video("R03"),  # 未产出
        ]
        m = compute(rows)
        self.assertEqual(m["videos"], 3)
        self.assertEqual(m["videos_no_candidate"], 1)
        self.assertEqual(m["m1"], 1)
        self.assertAlmostEqual(m["m1"] / m["videos"], 1 / 3)

    def test_marginal_does_not_count_as_strict(self):
        rows = [_video("R01", _cand("c1", "marginal", "recommended"))]
        m = compute(rows)
        self.assertEqual(m["m1"], 0, "严格计数不含勉强通过")
        self.assertEqual(m["m1_loose"], 1)

    def test_misblock_and_misrelease_computed_separately(self):
        rows = [
            # 可用但没推荐 → 误拦
            _video("R01", _cand("c1", "approved", "needs_review"), _cand("c2", "approved", "recommended")),
            # 推荐但不可用 → 误放
            _video("R02", _cand("c3", "rejected", "recommended")),
        ]
        m = compute(rows)
        self.assertEqual(m["approved"], 2)
        self.assertEqual(m["recommended"], 2)
        self.assertEqual(len(m["misblocked"]), 1)
        self.assertEqual(len(m["misreleased"]), 1)
        self.assertEqual(m["delivered"], 1)

    def test_unjudged_candidates_do_not_enter_verdict_counts(self):
        rows = [_video("R01", _cand("c1", None, "unknown"), _cand("c2", "approved", "recommended"))]
        m = compute(rows)
        self.assertEqual(m["judged"], 1)
        self.assertEqual(m["unjudged"], 1)
        self.assertEqual(m["m1"], 1)

    def test_round_basis_split(self):
        rows = [
            _video("R01", _cand("c1", "approved", "recommended"), basis="first_round"),
            _video("R02", _cand("c2", "rejected", "rewrite"), basis="earliest_available"),
        ]
        m = compute(rows)
        self.assertEqual(m["by_basis"]["first_round"]["strict"], 1)
        self.assertEqual(m["by_basis"]["earliest_available"]["strict"], 0)

    def test_pending_review_counts_as_delivered_not_misblocked(self):
        """进了待核组也算交到人眼前，不能记成系统拦掉。"""
        cand = _cand("c1", "approved", "needs_review")
        cand.update(recommended=False, delivered=True)
        m = compute([_video("R01", cand)])
        self.assertEqual(len(m["misblocked"]), 0)
        self.assertEqual(m["delivered"], 1)

    def test_marginal_on_top_counts_as_misreleased(self):
        """首推是「勉强通过」也算放错——严格口径下它不能被当成可用。"""
        m = compute([_video("R01", _cand("c1", "marginal", "recommended"))])
        self.assertEqual(len(m["misreleased"]), 1)
        self.assertEqual(m["recommended_judged"], 1)

    def test_unjudged_top_does_not_dilute_misrelease_denominator(self):
        rows = [_video("R01", _cand("c1", "rejected", "recommended"), _cand("c2", None, "recommended"))]
        m = compute(rows)
        self.assertEqual(m["recommended"], 2)
        self.assertEqual(m["recommended_judged"], 1)
        self.assertEqual(m["recommended_unjudged"], 1)
        self.assertEqual(len(m["misreleased"]), 1)


class BucketTests(unittest.TestCase):
    def test_known_reasons_are_bucketed(self):
        self.assertEqual(bucketize("REVIEW_SCHEMA_INVALID"), "评审输出结构失败")
        self.assertEqual(bucketize("ALL_GATES_PASS_AND_QUALITY_MEETS_MINIMUM"), "系统判通过但路由未推荐")
        self.assertEqual(bucketize("G4_REPAIRABLE_WITH_INVALID_REVIEW"), "内容层要求改写")

    def test_empty_reason_means_route_produced_nothing(self):
        self.assertEqual(bucketize(""), "路由未产出结论")


if __name__ == "__main__":
    unittest.main()
