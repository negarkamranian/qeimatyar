from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit
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
ANALYTICS_OAUTH_SCOPES = {"vendor.parcel.read"}


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

    def requested_scopes(self) -> list[str]:
        """Return the least-privilege scopes required by the current product.

        Optional analytics scopes are used only when explicitly configured.
        The product remains fully usable with the catalog-only scopes.
        """
        configured = settings.scopes.split()
        scopes = list(dict.fromkeys(configured))
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
        return scopes

    def authorization_url(self, state: str) -> str:
        scopes = self.requested_scopes()
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

    async def product_price_history(
        self,
        token: str,
        product_id: int,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the merchant product's own historical prices.

        This endpoint is part of the Core service and is covered by the
        ``vendor.product.read`` authority.
        """
        params = {
            key: value
            for key, value in {"start_time": start_time, "end_time": end_time}.items()
            if value
        }
        payload = await self._request(
            "GET",
            f"{self.api_base}/products/{product_id}/price-history",
            token=token,
            params=params,
            operation="product_price_history",
        )
        if isinstance(payload, list):
            return payload
        return payload.get("data") or []

    async def vendor_parcels(
        self,
        token: str,
        vendor_id: int,
        *,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        """Read recent seller parcels without retaining customer information.

        The caller extracts only product, quantity, unit-price and timestamp.
        Pagination is cursor based in the Order Processing service.
        """
        parcels: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max(1, min(max_pages, 100))):
            params: list[tuple[str, Any]] = [
                ("items.vendor_ids", str(vendor_id)),
                ("per_page", 100),
                ("sort", "created_at:desc"),
            ]
            if cursor:
                params.append(("cursor", cursor))
            payload = await self._request(
                "GET",
                f"{self.api_base}/vendor-parcels",
                token=token,
                params=params,
                operation="vendor_sales_history",
            )
            if isinstance(payload, list):
                parcels.extend(item for item in payload if isinstance(item, dict))
                break
            batch = payload.get("data") or []
            parcels.extend(item for item in batch if isinstance(item, dict))
            next_cursor = payload.get("next_cursor")
            if not batch or not next_cursor or str(next_cursor) in seen_cursors:
                break
            cursor = str(next_cursor)
            seen_cursors.add(cursor)
        return parcels

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


class _BasalamProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.json_ld: list[dict[str, Any]] = []
        self._in_script = False
        self._script_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and attributes.get("content"):
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key:
                self.meta[key] = attributes["content"]
        elif tag.lower() == "script" and attributes.get("type") == "application/ld+json":
            self._in_script = True
            self._script_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self._in_script = False
            raw = "".join(self._script_buffer).strip()
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, list):
                        self.json_ld.extend(d for d in data if isinstance(d, dict))
                    elif isinstance(data, dict):
                        self.json_ld.append(data)
                except json.JSONDecodeError:
                    pass

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_buffer.append(data)

    def product_details(self) -> dict[str, Any]:
        title = (
            self.meta.get("og:title")
            or self.meta.get("twitter:title")
            or self.meta.get("title")
            or ""
        )
        description = self.meta.get("og:description") or ""
        image_url = self.meta.get("og:image") or ""
        brand = ""
        specs: dict[str, str] = {}
        for entry in self.json_ld:
            entry_type = entry.get("@type", "")
            if not brand and entry_type == "Brand":
                brand = str(entry.get("name", ""))
            if entry_type in {"Product", "ProductModel"}:
                if not title:
                    title = str(entry.get("name", ""))
                if not description:
                    description = str(entry.get("description", ""))
                if not image_url and entry.get("image"):
                    img = entry["image"]
                    if isinstance(img, dict):
                        image_url = str(img.get("url", ""))
                    else:
                        image_url = str(img)
                offers = entry.get("offers", {})
                if isinstance(offers, dict):
                    specs.setdefault("price", str(offers.get("price", "")))
                    specs.setdefault("currency", str(offers.get("priceCurrency", "")))
            if entry_type == "ProductModel":
                props = entry.get("additionalProperty", [])
                if isinstance(props, list):
                    for prop in props:
                        if isinstance(prop, dict) and prop.get("name") and prop.get("value"):
                            specs[str(prop["name"])] = str(prop["value"])
        return {
            "title": _clean_html_title(title),
            "description": description,
            "brand": brand,
            "image_url": image_url,
            "specs": specs,
        }


def _clean_html_title(title: str) -> str:
    title = re.sub(r"\s*[|–—]\s*(?:دیجی‌کالا|ترب|باسلام).*$", "", title, flags=re.IGNORECASE)
    return " ".join(title.split()).strip()


def _basalam_product_id_from_url(url: str) -> int | None:
    parsed = urlsplit(url.strip())
    if (parsed.hostname or "").lower() not in {"basalam.com", "www.basalam.com"}:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in {"p", "product", "products"}:
            candidate = segments[index + 1]
            if candidate.isdigit():
                return int(candidate)
    return None


def _basalam_store_id_from_url(url: str) -> str | None:
    parsed = urlsplit(url.strip())
    if (parsed.hostname or "").lower() not in {"basalam.com", "www.basalam.com"}:
        return None
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) >= 2 and segments[0].lower() in {"s", "store", "vendor"}:
        return unquote(segments[1])
    return None


_PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Qeimatyar/0.3",
}


async def fetch_basalam_product(product_id: int) -> dict[str, Any]:
    """Fetch full product details from Basalam (public endpoint when possible)."""
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            trust_env=settings.marketplace_trust_env,
        ) as client:
            url = f"{basalam.api_base}/products/{product_id}"
            response = await client.get(url, headers=_PUBLIC_HEADERS)
            if response.status_code == 200:
                data = response.json()
                title = (
                    data.get("title")
                    or data.get("name")
                    or data.get("data", {}).get("title", "")
                )
                if title:
                    return {
                        "title": str(title),
                        "description": str(data.get("description", "")),
                        "brand": str(data.get("brand", {}).get("name", "")) if isinstance(data.get("brand"), dict) else "",
                        "price": int(data.get("price") or data.get("primary_price") or 0),
                        "image_url": str(data.get("photo") or data.get("image_url", "")) if not isinstance(data.get("photo"), dict) else str((data.get("photo") or {}).get("md", "")),
                        "specs": _flatten_basalam_specs(data.get("params") or data.get("attributes", [])),
                        "product_id": product_id,
                    }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Basalam product API fetch failed for %s: %s", product_id, exc)

    html_details = await _fetch_product_details(product_id)
    if html_details.get("title"):
        return {**html_details, "price": 0, "product_id": product_id}
    return {}


async def _fetch_product_title(product_id: int) -> str:
    details = await _fetch_product_details(product_id)
    return str(details.get("title", ""))


async def _fetch_product_details(product_id: int) -> dict[str, Any]:
    url = f"https://basalam.com/p/{product_id}"
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fa-IR,fa;q=0.9",
        "User-Agent": "Qeimatyar/0.3 product-link-preview",
    }
    timeout = httpx.Timeout(12, connect=7)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        trust_env=settings.marketplace_trust_env,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            return {}
        parser = _BasalamProductHTMLParser()
        parser.feed(response.text)
        details = parser.product_details()
        title = details.get("title", "")
        if not title or title.lower() in {"باسلام", "basalam"}:
            return {}
        return details


def _flatten_basalam_specs(params: list[Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    if not isinstance(params, list):
        return specs
    for param in params:
        if isinstance(param, dict):
            name = str(param.get("name", param.get("title", "")))
            value = str(param.get("value", param.get("text", "")))
            if name and value:
                specs[name] = value
    return specs


async def fetch_basalam_store(store_identifier: str) -> dict[str, Any]:
    """Fetch store vendor info from Basalam."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            url = f"{basalam.api_base}/vendors/slug/{store_identifier}"
            response = await client.get(url, headers=_PUBLIC_HEADERS)
            if response.status_code == 200:
                data = response.json()
                vendor_id = data.get("id") or data.get("vendor_id")
                title = data.get("title") or data.get("title", "")
                return {
                    "vendor_id": vendor_id,
                    "title": str(title) if title else store_identifier,
                    "store_identifier": store_identifier,
                    "product_count": int(data.get("products_count", 0) or 0),
                }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Basalam store API fetch failed for %s: %s", store_identifier, exc)
    return {"vendor_id": None, "title": store_identifier, "store_identifier": store_identifier, "product_count": 0}


async def fetch_store_product_list(store_identifier: str, max_products: int = 100) -> list[dict[str, Any]]:
    """Fetch product IDs and titles from a Basalam store page (public, no auth).

    Returns a list of dicts with 'product_id' and 'title' keys.
    """
    url = f"https://basalam.com/s/{store_identifier}"
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fa-IR,fa;q=0.9",
        "User-Agent": "Qeimatyar/0.3 store-product-fetcher",
    }
    timeout = httpx.Timeout(20, connect=10)
    products: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        page = 1
        while len(products) < max_products:
            page_url = url if page == 1 else f"https://basalam.com/s/{store_identifier}?page={page}"
            try:
                response = await client.get(page_url)
                response.raise_for_status()
                content = response.text
            except (httpx.HTTPError, Exception) as exc:
                logger.warning("Failed to fetch store page %s (page %d): %s", store_identifier, page, exc)
                break

            product_ids = re.findall(r"/p/(\d+)", content)
            seen = set()
            for pid in product_ids:
                if pid in seen:
                    continue
                seen.add(pid)
                product_url = f"https://basalam.com/p/{pid}"
                title_match = re.search(
                    r'<a[^>]*href="/p/' + re.escape(pid) + r'"[^>]*>(.*?)</a>',
                    content,
                    re.DOTALL | re.IGNORECASE,
                )
                title = ""
                if title_match:
                    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
                products.append({
                    "product_id": int(pid),
                    "title": title,
                    "url": product_url,
                })
                if len(products) >= max_products:
                    break

            if not product_ids:
                break
            if len(product_ids) < 20:
                break
            page += 1
            if page > 5:
                break

    logger.info("Fetched %d products from Basalam store %s", len(products), store_identifier)
    return products
