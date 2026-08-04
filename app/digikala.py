from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class DigikalaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DigikalaClient:
    api_base = "https://seller.digikala.com/open-api/v1"

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


digikala = DigikalaClient()
