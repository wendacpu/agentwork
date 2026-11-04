import io
import json
import unittest
from types import SimpleNamespace

from agent_system.config import RunConfig
from agent_system.types import Claim, VerificationResult
from agent_system.verification.judge import compute_risk, parse_verification
from agent_system.verification.router import build_verification_query, is_verifiable, pick_core_claims, whitelist_for
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

    def test_query_prefers_explicit_model_field(self):
        claim = Claim("c1", "绿字标题压屏，镜头跟随一名男子走过走廊", "observable", [], verification_query="挪威首相驳斥FT通话报道")
        self.assertEqual(build_verification_query(claim), "挪威首相驳斥FT通话报道")

    def test_query_extracts_assertion_from_visual_description(self):
        claim = Claim(
            "c1",
            "绿字标题压屏“挪威首相驳斥FT有关普京通话报道”；随后首相斯特勒系红领带在绿墙前抬手讲解，底部白色字幕条滚动",
            "observable",
            [],
        )
        query = build_verification_query(claim)
        self.assertIn("挪威首相驳斥", query)
        for noise in ("绿字", "压屏", "红领带", "字幕条", "镜头"):
            self.assertNotIn(noise, query)
        self.assertLessEqual(len(query), 40)

    def test_query_falls_back_to_truncated_text_when_all_segments_are_visual(self):
        claim = Claim("c1", "镜头推进，画面底部白色字幕条滚动，台标在右上角", "observable", [])
        query = build_verification_query(claim)
        self.assertTrue(query)
        self.assertLessEqual(len(query), 40)

    def test_query_is_empty_for_empty_text(self):
        self.assertEqual(build_verification_query(Claim("c1", "   ", "observable", [])), "")

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


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, _limit: int | None = None) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class WsaTransportRetryTests(unittest.TestCase):
    """瞬时网络故障要重试，且失败必须标成可重试。

    真实故障（2026-11-01）：本机 TUN 代理下同一域名连续握手约一半概率拿到
    SSL UNEXPECTED_EOF。旧实现一次失败就抛 `RuntimeError`（没有 retryable 属性），
    上层 `getattr(exc, "retryable", False)` 得到 False，于是把这条任务判成
    不可重试技术失败，`batch` 还会因此中断整批。核查是可选增强能力，
    不该被一次网络抖动打死。
    """

    def _run(self, side_effects, headers=None):
        from unittest.mock import patch
        from agent_system.verification import search as search_module
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            effect = side_effects[min(len(calls) - 1, len(side_effects) - 1)]
            if isinstance(effect, Exception):
                raise effect
            return _FakeResponse(effect)

        with patch.object(search_module, "urlopen", fake_urlopen), \
             patch.object(search_module.time, "sleep", lambda *_: None), \
             patch.object(search_module.random, "uniform", lambda *_: 0.0):
            try:
                result = search_module.WsaSearchProvider._http_post_json(
                    "https://wsa.tencentcloudapi.com/", headers or {}, {"q": 1}, 5.0)
                return result, calls, None
            except Exception as exc:  # noqa: BLE001 - 测试要断言异常属性
                return None, calls, exc

    def test_ssl_eof_is_retried_then_succeeds(self):
        from ssl import SSLEOFError
        payload = json.dumps({"Response": {"Pages": []}}).encode()
        result, calls, error = self._run([SSLEOFError("EOF occurred in violation of protocol"), payload])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result, {"Response": {"Pages": []}})

    def test_exhausted_retries_raise_retryable_model_error(self):
        from urllib.error import URLError
        from agent_system.model import ModelError
        _, calls, error = self._run([URLError("connection reset")])
        self.assertIsInstance(error, ModelError)
        self.assertTrue(error.retryable)
        self.assertEqual(len(calls), 3)
        self.assertIn("网络或代理链路不稳定", str(error))

    def test_client_error_is_not_retried_and_not_marked_retryable(self):
        from urllib.error import HTTPError
        from agent_system.model import ModelError
        error_body = io.BytesIO(b'{"Response":{"Error":{"Code":"AuthFailure"}}}')
        _, calls, raised = self._run([HTTPError("https://wsa/", 403, "Forbidden", {}, error_body)])
        self.assertIsInstance(raised, ModelError)
        self.assertFalse(raised.retryable)
        self.assertEqual(len(calls), 1, "凭据/请求错误不该重试")

    def test_server_error_is_retried(self):
        from urllib.error import HTTPError
        from agent_system.model import ModelError
        def make(code):
            return HTTPError("https://wsa/", code, "boom", {}, io.BytesIO(b"upstream down"))
        payload = json.dumps({"Response": {"Pages": []}}).encode()
        result, calls, error = self._run([make(503), payload])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result, {"Response": {"Pages": []}})

    def test_http_error_detail_never_contains_credentials(self):
        from urllib.error import HTTPError
        error, _, raised = self._run([HTTPError("https://wsa/", 401, "Unauthorized", {}, io.BytesIO(b"denied"))])
        self.assertIsNone(error)
        self.assertNotIn("secret", str(raised))
        self.assertIn("WSA HTTP 401", str(raised))


if __name__ == "__main__":
    unittest.main()
