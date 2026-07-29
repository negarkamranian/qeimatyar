from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet

from app.config import settings

logger = logging.getLogger(__name__)


class BasalamError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        query = urlencode(
            {
                "client_id": settings.client_id,
                "scope": " ".join(scopes),
                "redirect_uri": settings.redirect_uri,
                "state": state,
            }
        )
        return f"https://basalam.com/accounts/sso?{query}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{self.auth_base}/oauth/token",
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

    async def me(self, token: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.api_base}/users/me", token=token)

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
        **kwargs: Any,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                trust_env=settings.marketplace_trust_env,
            ) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            error_detail: Any = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error_detail = {
                        key: payload[key]
                        for key in ("error", "error_description", "message", "detail")
                        if key in payload
                    }
            except ValueError:
                pass
            logger.warning(
                "Basalam API rejected request method=%s path=%s status=%s detail=%r",
                method,
                urlsplit(url).path,
                response.status_code,
                error_detail or "no structured error detail",
            )
            raise BasalamError(
                f"Basalam request failed: HTTP {response.status_code}",
                response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BasalamError(f"Basalam request failed: {exc}") from exc


basalam = BasalamClient()
