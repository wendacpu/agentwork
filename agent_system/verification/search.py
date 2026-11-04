"""搜索核查用的可插拔 Provider。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..model import ModelError


# 本机 TUN 代理下实测：同一域名连续握手约一半概率拿到 SSL UNEXPECTED_EOF，
# 重试一次即可显著提高成功率。核查是可选增强能力，不该因一次网络抖动打死整条任务。
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_BACKOFF_SECONDS = 1.5

# 可重试的 HTTP 状态：限流与服务端临时故障属于瞬时问题；
# 400/401/403/404/422 是请求或凭据问题，重试没有意义。
_RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


class SearchProvider(Protocol):
    def search(self, query: str, sites: list[str], limit: int) -> list[dict[str, str]]: ...
    def fetch(self, url: str) -> str: ...


class UnconfiguredSearchProvider:
    """默认 fail-closed provider：未配置后端时不可静默跳过风险检查。"""
    def search(self, query: str, sites: list[str], limit: int) -> list[dict[str, str]]:
        raise RuntimeError("verification search provider is not configured")

    def fetch(self, url: str) -> str:
        raise RuntimeError("verification search provider does not fetch pages")


class WsaSearchProvider:
    """腾讯云元宝搜索 SearchPro API，使用腾讯云 TC3-HMAC-SHA256 签名。"""
    endpoint = "https://wsa.tencentcloudapi.com/"
    service = "wsa"
    version = "2025-05-08"

    def __init__(
        self,
        secret_id: str | None = None,
        secret_key: str | None = None,
        *,
        secret_id_env: str = "WSA_SECRET_ID",
        secret_key_env: str = "WSA_SECRET_KEY",
        api_key: str | None = None,
        api_key_env: str = "WSA_API_KEY",
        timeout: float = 20.0,
        industry: str = "news",
        freshness: str | None = None,
        post_json=None,
    ) -> None:
        # Tencent Cloud v3 requires the SecretId/SecretKey pair, not a bearer API key.
        self.secret_id = secret_id or os.getenv(secret_id_env, "")
        self.secret_key = secret_key or os.getenv(secret_key_env, "")
        # Keep these fields only for backwards-compatible construction diagnostics; they are not sent.
        self.api_key_env = api_key_env
        self.api_key = api_key or os.getenv(api_key_env, "")
        self.timeout = timeout
        self.industry = industry
        self.freshness = freshness
        self._post_json = post_json or self._http_post_json

    @staticmethod
    def _http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        # Serialize identically to _tc3_headers: the exact bytes are part of the signature.
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data: bytes | None = None
        last_error = ""
        for attempt in range(_TRANSIENT_RETRY_ATTEMPTS):
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=timeout) as response:
                    data = response.read(2_000_000)
                break
            except HTTPError as exc:
                # 不附带请求头或密钥，避免把凭据写入日志/异常信息。
                detail = exc.read(1000).decode("utf-8", errors="replace")
                if exc.code not in _RETRYABLE_HTTP_STATUS:
                    # 请求或凭据问题：重试没有意义，也不该被上层当成瞬时故障。
                    raise ModelError(f"WSA HTTP {exc.code}: {detail}", retryable=False) from None
                last_error = f"WSA HTTP {exc.code}: {detail}"
            except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                # SSL UNEXPECTED_EOF / 连接重置 / 超时都属瞬时网络故障，重试有意义。
                reason = getattr(exc, "reason", None) or exc
                last_error = f"WSA request failed: {reason}"
            if attempt < _TRANSIENT_RETRY_ATTEMPTS - 1:
                # 退避带抖动，避免多个核查请求同时重试再撞在一起。
                time.sleep(_TRANSIENT_BACKOFF_SECONDS * (attempt + 1) + random.uniform(0, 0.4))
        if data is None:
            # 重试用尽仍失败：标成可重试，让上层决定是否续跑，而不是把整条任务判死。
            raise ModelError(
                f"{last_error}（已重试 {_TRANSIENT_RETRY_ATTEMPTS} 次，疑似本机网络或代理链路不稳定）",
                retryable=True,
            )
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("WSA returned invalid JSON") from exc

    @staticmethod
    def _tc3_headers(payload: dict[str, Any], secret_id: str, secret_key: str, now: datetime | None = None) -> dict[str, str]:
        """Build Tencent Cloud API v3 (TC3-HMAC-SHA256) headers."""
        host = "wsa.tencentcloudapi.com"
        content_type = "application/json; charset=utf-8"
        timestamp = int((now or datetime.now(timezone.utc)).timestamp())
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
        signed_headers = "content-type;host"
        canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{body_hash}"
        scope = f"{date}/{WsaSearchProvider.service}/tc3_request"
        string_to_sign = "TC3-HMAC-SHA256\n{}\n{}\n{}".format(timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
        def sign(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
        secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = sign(secret_date, WsaSearchProvider.service)
        secret_signing = sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        return {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": host,
            "X-TC-Action": "SearchPro",
            "X-TC-Version": WsaSearchProvider.version,
            "X-TC-Timestamp": str(timestamp),
        }

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.secret_id or not self.secret_key:
            raise RuntimeError("WSA_SECRET_ID and WSA_SECRET_KEY are required (Tencent Cloud TC3 credentials)")
        headers = self._tc3_headers(payload, self.secret_id, self.secret_key)
        raw = self._post_json(self.endpoint, headers, payload, self.timeout)
        if not isinstance(raw, dict):
            raise RuntimeError("WSA returned a non-object response")
        response = raw.get("Response", raw)
        if not isinstance(response, dict):
            raise RuntimeError("WSA returned an invalid Response object")
        err = response.get("Error")
        if err:
            if isinstance(err, dict):
                raise RuntimeError(f"WSA API error {err.get('Code', 'unknown')}: {err.get('Message', '')}")
            raise RuntimeError("WSA API returned an error")
        return response

    def search(self, query: str, sites: list[str] | None = None, limit: int = 10) -> list[dict[str, str]]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 50))
        requested_count = min(50, max(10, ((limit + 9) // 10) * 10))
        selected_sites = [s.strip().removeprefix("https://").removeprefix("http://").strip("/") for s in (sites or []) if s.strip()]
        search_sites = selected_sites or [""]
        merged: list[dict[str, str]] = []
        seen: set[str] = set()
        for site in search_sites:
            payload: dict[str, Any] = {"Query": query, "Mode": 0, "Cnt": requested_count, "Industry": self.industry}
            if self.freshness:
                payload["Freshness"] = self.freshness
            if site:
                payload["Site"] = site
            response = self._request(payload)
            pages = response.get("Pages", [])
            if not isinstance(pages, list):
                raise RuntimeError("WSA Response.Pages is not a list")
            for item in pages:
                if isinstance(item, str):
                    try:
                        item = json.loads(item)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or item.get("URL") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                merged.append({
                    "url": url,
                    "title": str(item.get("title") or item.get("Title") or ""),
                    "snippet": str(item.get("passage") or item.get("content") or item.get("snippet") or ""),
                    "date": str(item.get("date") or item.get("Date") or ""),
                    "site": str(item.get("site") or item.get("Site") or ""),
                    "score": str(item.get("score") or item.get("Score") or ""),
                    "provider": "wsa",
                })
                if len(merged) >= limit:
                    return merged
            if len(search_sites) > 1:
                time.sleep(0.05)
        return merged[:limit]

    def fetch(self, url: str) -> str:
        """WSA 提供搜索结果，不代抓网页正文；调用者应使用受限的白名单抓取器。"""
        raise NotImplementedError("WSA SearchPro does not fetch full page text")


def create_search_provider(name: str = "wsa", **kwargs) -> SearchProvider:
    normalized = name.strip().lower()
    if normalized in {"wsa", "tencent_wsa"}:
        return WsaSearchProvider(**kwargs)
    if normalized in {"", "none", "unconfigured"}:
        return UnconfiguredSearchProvider()
    raise ValueError(f"unsupported verification provider: {name}")
