from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings


class DigikalaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DigikalaClient:
    api_base = "https://seller.digikala.com/open-api/v1"
    public_api_base = "https://api.digikala.com/discovery/api/v1"
    _seller_path = re.compile(r"^/seller/([A-Za-z0-9]+)/?$")

    def seller_code(self, value: str) -> str | None:
        """Return a seller code only for a valid public Digikala seller URL."""
        candidate = value.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"digikala.com", "www.digikala.com"}:
            return None
        match = self._seller_path.fullmatch(parsed.path)
        return match.group(1).upper() if match else None

    async def profile(self, token: str) -> dict[str, Any]:
        payload = await self._request("GET", "/profile", token)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("seller_id"):
            raise DigikalaError("Digikala profile response is incomplete.")
        return data

    async def variants(self, token: str) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = await self._request(
                "GET",
                "/variants",
                token,
                params={"page": page, "size": 50, "sort": "id", "order": "asc"},
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise DigikalaError("Digikala variants response is incomplete.")
            batch = data.get("items") or []
            if not isinstance(batch, list):
                raise DigikalaError("Digikala variants list is invalid.")
            variants.extend(item for item in batch if isinstance(item, dict))
            pager = data.get("pager") or {}
            try:
                total_pages = min(max(1, int(pager.get("total_pages") or 1)), 500)
            except (TypeError, ValueError):
                total_pages = 1
            if not batch:
                break
            page += 1
        return variants

    async def public_catalog(self, seller_code: str) -> dict[str, Any]:
        """Fetch every publicly visible product page for a Digikala seller."""
        code = seller_code.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+", code):
            raise DigikalaError("Digikala seller code is invalid.")

        products: list[dict[str, Any]] = []
        seller: dict[str, Any] | None = None
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = await self._public_request(
                f"/sellers/{code}", params={"page": page}
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise DigikalaError("Digikala seller response is incomplete.")
            if seller is None and isinstance(data.get("seller"), dict):
                seller = data["seller"]
            batch = data.get("products") or []
            if not isinstance(batch, list):
                raise DigikalaError("Digikala seller products are invalid.")
            products.extend(item for item in batch if isinstance(item, dict))
            pager = data.get("pager") or {}
            try:
                total_pages = min(max(1, int(pager.get("total_pages") or 1)), 500)
            except (TypeError, ValueError):
                total_pages = 1
            if not batch:
                break
            page += 1

        if not seller or not seller.get("id"):
            raise DigikalaError("Digikala seller was not found.", 404)
        return {"seller": seller, "products": products}

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=25,
                follow_redirects=True,
                trust_env=settings.marketplace_trust_env,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.api_base}{path}",
                    headers=headers,
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise DigikalaError("Could not connect to Digikala Seller API.") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("message") or "Digikala rejected the API token."
            except ValueError:
                message = "Digikala rejected the API request."
            raise DigikalaError(str(message), response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DigikalaError("Digikala returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise DigikalaError("Digikala returned an invalid response.")
        return payload

    async def _public_request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=25,
                follow_redirects=True,
                trust_env=settings.marketplace_trust_env,
            ) as client:
                response = await client.get(
                    f"{self.public_api_base}{path}",
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise DigikalaError("Could not connect to Digikala public API.") from exc
        if response.status_code >= 400:
            raise DigikalaError("Digikala seller link was rejected.", response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DigikalaError("Digikala returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise DigikalaError("Digikala returned an invalid response.")
        return payload


digikala = DigikalaClient()
