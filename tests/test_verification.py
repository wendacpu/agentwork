import json
import unittest
from types import SimpleNamespace

from agent_system.config import RunConfig
from agent_system.types import Claim, VerificationResult
from agent_system.verification.judge import compute_risk, parse_verification
from agent_system.verification.router import is_verifiable, pick_core_claims, whitelist_for
from agent_system.verification.search import WsaSearchProvider, create_search_provider


class VerificationTests(unittest.TestCase):
    def test_wsa_provider_builds_tc3_request_and_parses_official_response(self):
        calls = []
        def fake_post(url, headers, payload, timeout):
            calls.append((url, headers, payload, timeout))
            return {"Response": {"Pages": [
                json.dumps({"url": "https://news.cn/a", "title": "新闻标题", "passage": "摘要", "date": "2026-10-31", "site": "news.cn", "score": 0.9}),
                json.dumps({"url": "https://news.cn/b", "title": "第二条", "content": "动态摘要"}),
            ]}}
        provider = WsaSearchProvider(secret_id="test-id", secret_key="test-secret", freshness="d7", post_json=fake_post)
        results = provider.search("某项政策已发布", ["https://news.cn/"], 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["snippet"], "摘要")
        self.assertEqual(results[1]["snippet"], "动态摘要")
        url, headers, payload, _ = calls[0]
        self.assertEqual(url, "https://wsa.tencentcloudapi.com/")
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=test-id/"))
        self.assertEqual(headers["X-TC-Action"], "SearchPro")
        self.assertEqual(headers["X-TC-Version"], "2025-05-08")
        self.assertEqual(payload["Cnt"], 10)
        self.assertEqual(payload["Site"], "news.cn")
        self.assertEqual(payload["Industry"], "news")
        self.assertEqual(payload["Freshness"], "d7")

    def test_wsa_provider_fans_out_sites_and_deduplicates(self):
        calls = []
        def fake_post(url, headers, payload, timeout):
            calls.append(payload["Site"])
            return {"Response": {"Pages": [json.dumps({"url": "https://same.cn/story", "title": payload["Site"]})]}}
        provider = WsaSearchProvider(secret_id="test-id", secret_key="test-secret", post_json=fake_post)
        results = provider.search("query", ["news.cn", "cctv.com"], 10)
        self.assertEqual(calls, ["news.cn", "cctv.com"])
        self.assertEqual(len(results), 1)

    def test_wsa_requires_secret_pair_and_rejects_nested_api_error(self):
        with self.assertRaisesRegex(RuntimeError, "WSA_SECRET_ID and WSA_SECRET_KEY"):
            WsaSearchProvider(post_json=lambda *args: {}).search("query")
        provider = WsaSearchProvider(secret_id="id", secret_key="secret", post_json=lambda *args: {"Response": {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}}})
        with self.assertRaisesRegex(RuntimeError, "UnauthorizedOperation"):
            provider.search("query")

    def test_provider_factory(self):
        self.assertIsInstance(create_search_provider("wsa", secret_id="id", secret_key="secret"), WsaSearchProvider)

    def test_wsa_uses_configured_secret_environment_variables(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"CUSTOM_WSA_ID": "id-test", "CUSTOM_WSA_SECRET": "secret-test"}):
            provider = create_search_provider("wsa", secret_id_env="CUSTOM_WSA_ID", secret_key_env="CUSTOM_WSA_SECRET")
        self.assertEqual(provider.secret_id, "id-test")
        self.assertEqual(provider.secret_key, "secret-test")

    def test_stage_one_route_includes_all_news_domains_and_skips_other_domains(self):
        config = RunConfig(verification_enabled=True)
        for second in ("时事社会", "体育赛事", "文娱热点", "财经商业", "科技数码"):
            self.assertTrue(is_verifiable({"一级": "资讯", "二级": second}, config))
        self.assertFalse(is_verifiable({"一级": "美食", "二级": "饮食文化"}, config))
        self.assertFalse(is_verifiable({"一级": "知识科普", "二级": "自然科学"}, config))

    def test_provider_absence_does_not_enable_unsupported_route_by_itself(self):
        config = RunConfig(verification_enabled=False)
        self.assertFalse(is_verifiable({"一级": "资讯", "二级": "时事社会"}, config))

    def test_whitelist_and_core_claim_filter(self):
        self.assertIn("news.cn", whitelist_for({"一级": "资讯", "二级": "时事社会"}))
        claims = [
            Claim("c1", "观察事实", "observable", []),
            Claim("c2", "观点", "opinion", []),
            Claim("c3", "来源陈述", "source_claim", []),
        ]
        self.assertEqual([x.claim_id for x in pick_core_claims(claims, RunConfig(max_verification_claims=1))], ["c1"])

    def test_parse_invalid_result_fails_to_unverifiable(self):
        result = parse_verification({"verdict": "false", "confidence": "bad"}, "c1")
        self.assertEqual(result.verdict, "unverifiable")
        self.assertEqual(result.confidence, 0.0)

    def test_refuted_risk_requires_distinct_sources_and_confidence_threshold(self):
        claim = Claim("c1", "断言", "observable", [])
        claim.verification = VerificationResult("c1", "refuted", ["https://a", "https://a"], 0.99)
        self.assertEqual(compute_risk([claim], 2, 0.8)["level"], "low")
        claim.verification.sources.append("https://b")
        claim.verification.confidence = 0.79
        self.assertEqual(compute_risk([claim], 2, 0.8)["level"], "low")
        claim.verification.confidence = 0.8
        result = compute_risk([claim], 2, 0.8)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["refuted_claim_ids"], ["c1"])


if __name__ == "__main__":
    unittest.main()
