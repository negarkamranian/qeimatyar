from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger(__name__)

REQUIRED_OAUTH_SCOPES = {
    "customer.profile.read",
    "vendor.profile.read",
    "vendor.product.read",
}


class BasalamError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        error_kind: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_kind = error_kind


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


class BasalamClient:
    api_base = "https://openapi.basalam.com/v1"
    auth_base = "https://auth.basalam.com"

    def authorization_url(self, state: str) -> str:
        scopes = settings.scopes.split()
        unsafe_scopes = [scope for scope in scopes if not scope.endswith(".read")]
        if unsafe_scopes:
            raise ValueError(
                "Basalam OAuth is read-only; remove these scopes: "
                + ", ".join(unsafe_scopes)
            )
        missing_scopes = REQUIRED_OAUTH_SCOPES.difference(scopes)
        if missing_scopes:
            raise ValueError(
                "Basalam OAuth is missing required scopes: "
                + ", ".join(sorted(missing_scopes))
            )
        query = urlencode(
            {
                "client_id": settings.client_id,
                "scope": " ".join(scopes),
                "redirect_uri": settings.redirect_uri,
                "state": state,
            }
        )
        return f"https://basalam.com/accounts/sso?{query}"

    async def exchange_code(
        self, code: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{self.auth_base}/oauth/token",
            trace_id=trace_id,
            operation="oauth_token_exchange",
            json={
                "grant_type": "authorization_code",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "redirect_uri": settings.redirect_uri,
                "code": code,
            },
        )

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{self.auth_base}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "refresh_token": refresh_token,
            },
        )

    async def me(
        self, token: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{self.api_base}/users/me",
            token=token,
            trace_id=trace_id,
            operation="oauth_user_profile",
        )

    async def products(self, token: str, vendor_id: int) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = await self._request(
                "GET",
                f"{self.api_base}/vendors/{vendor_id}/products",
                token=token,
                params={"page": page, "per_page": 100},
            )
            if isinstance(payload, list):
                products.extend(payload)
                break
            batch = payload.get("data") or []
            products.extend(batch)
            try:
                # Defensive cap avoids an invalid API response causing an endless loop.
                total_pages = min(max(1, int(payload.get("total_page") or 1)), 500)
            except (TypeError, ValueError):
                total_pages = 1
            if not batch:
                break
            page += 1
        return products

    async def update_price(self, token: str, product_id: int, price: int) -> dict[str, Any]:
        # Verified against basalam-sdk 1.2.0 CoreService.update_product.
        # Sending the minimal PATCH avoids overwriting unrelated product fields.
        return await self._request(
            "PATCH",
            f"{self.api_base}/products/{product_id}",
            token=token,
            json={"primary_price": price},
        )

    async def search_comparables(self, token: str, query: str) -> list[dict[str, Any]]:
        # Verified against basalam-sdk 1.2.0 SearchService.search_products.
        payload = await self._request(
            "POST",
            f"{self.api_base}/products/search",
            token=token,
            json={"q": query, "rows": 30, "start": 0},
        )
        if isinstance(payload, list):
            return payload
        return payload.get("data") or payload.get("results") or []

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        trace_id: str | None = None,
        operation: str | None = None,
        **kwargs: Any,
    ) -> Any:
        request_id = uuid4().hex[:16]
        trace_id = trace_id or "-"
        operation = operation or "basalam_api"
        path = urlsplit(url).path
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        logger.info(
            "basalam_request_started trace_id=%s request_id=%s operation=%s "
            "method=%s host=%s path=%s timeout_seconds=20 trust_env=%s",
            trace_id,
            request_id,
            operation,
            method,
            urlsplit(url).hostname,
            path,
            settings.marketplace_trust_env,
        )
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                trust_env=settings.marketplace_trust_env,
            ) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                payload = response.json()
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                response_fields = (
                    sorted(str(key) for key in payload.keys())
                    if isinstance(payload, dict)
                    else [f"<{type(payload).__name__}>"]
                )
                logger.info(
                    "basalam_request_succeeded trace_id=%s request_id=%s "
                    "operation=%s status=%s elapsed_ms=%s redirects=%s "
                    "content_type=%s content_length=%s provider_request_id=%s "
                    "response_fields=%s",
                    trace_id,
                    request_id,
                    operation,
                    response.status_code,
                    elapsed_ms,
                    len(response.history),
                    response.headers.get("content-type", "-"),
                    response.headers.get("content-length", "-"),
                    _provider_request_id(response),
                    response_fields,
                )
                return payload
        except httpx.HTTPStatusError as exc:
            response = exc.response
            error_detail: Any = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error_detail = {
                        key: _redact_detail(payload[key], kwargs)
                        for key in ("error", "error_description", "message", "detail")
                        if key in payload
                    }
            except ValueError:
                pass
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            logger.warning(
                "basalam_request_rejected trace_id=%s request_id=%s operation=%s "
                "method=%s path=%s status=%s elapsed_ms=%s redirects=%s "
                "content_type=%s content_length=%s provider_request_id=%s detail=%r",
                trace_id,
                request_id,
                operation,
                method,
                path,
                response.status_code,
                elapsed_ms,
                len(response.history),
                response.headers.get("content-type", "-"),
                response.headers.get("content-length", "-"),
                _provider_request_id(response),
                error_detail or "no structured error detail",
            )
            raise BasalamError(
                f"Basalam request failed: HTTP {response.status_code}",
                response.status_code,
                error_kind="http_status",
            ) from exc
        except httpx.HTTPError as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            logger.exception(
                "basalam_request_transport_error trace_id=%s request_id=%s "
                "operation=%s method=%s host=%s path=%s elapsed_ms=%s "
                "exception_type=%s",
                trace_id,
                request_id,
                operation,
                method,
                urlsplit(url).hostname,
                path,
                elapsed_ms,
                type(exc).__name__,
            )
            raise BasalamError(
                f"Basalam transport failed ({type(exc).__name__})",
                error_kind="transport",
            ) from exc
        except ValueError as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            logger.exception(
                "basalam_response_decode_error trace_id=%s request_id=%s "
                "operation=%s method=%s path=%s elapsed_ms=%s",
                trace_id,
                request_id,
                operation,
                method,
                path,
                elapsed_ms,
            )
            raise BasalamError(
                "Basalam returned an invalid JSON response",
                error_kind="invalid_json",
            ) from exc


def _provider_request_id(response: httpx.Response) -> str:
    for name in ("x-request-id", "x-correlation-id", "traceparent", "request-id"):
        if value := response.headers.get(name):
            return value[:200]
    return "-"


def _redact_detail(value: Any, request_kwargs: dict[str, Any]) -> Any:
    """Keep provider diagnostics useful without echoing OAuth credentials."""
    secrets_to_hide = [
        str(secret)
        for key, secret in (request_kwargs.get("json") or {}).items()
        if key in {"client_secret", "code", "access_token", "refresh_token"} and secret
    ]

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): (
                    "<redacted>"
                    if str(key).lower()
                    in {"client_secret", "code", "access_token", "refresh_token"}
                    else clean(child)
                )
                for key, child in list(item.items())[:20]
            }
        if isinstance(item, list):
            return [clean(child) for child in item[:20]]
        text = str(item)[:1000]
        for secret in secrets_to_hide:
            text = text.replace(secret, "<redacted>")
        return text

    return clean(value)


basalam = BasalamClient()
