from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

from app.config import settings


class BasalamError(RuntimeError):
    pass


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
        query = urlencode(
            {
                "client_id": settings.client_id,
                "scope": settings.scopes,
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

    async def me(self, token: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.api_base}/users/me", token=token)

    async def products(self, token: str, vendor_id: int) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"{self.api_base}/vendors/{vendor_id}/products",
            token=token,
            params={"per_page": 100},
        )
        if isinstance(payload, list):
            return payload
        return payload.get("data", [])

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
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BasalamError(f"Basalam request failed: {exc}") from exc


basalam = BasalamClient()
