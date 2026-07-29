from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any
import httpx

from app.config import settings


TOROB_SEARCH_URL = "https://api.torob.com/v4/base-product/search/"
DIGIKALA_SEARCH_URL = "https://api.digikala.com/v1/search/"
BASALAM_SEARCH_URL = "https://openapi.basalam.com/v1/products/search"

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_STOP_WORDS = {
    "از",
    "با",
    "برای",
    "به",
    "در",
    "مدل",
    "و",
    "یا",
    "خرید",
    "قیمت",
    "فروش",
}


@dataclass(frozen=True)
class MarketListing:
    source: str
    title: str
    price: int
    url: str
    image_url: str = ""
    similarity: float = 0

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["similarity"] = round(self.similarity, 2)
        return data


@dataclass(frozen=True)
class SourceStatus:
    source: str
    ok: bool
    count: int
    message: str = ""


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).translate(_DIGITS)
    value = value.replace("ي", "ی").replace("ك", "ک").lower()
    value = value.replace("گیگابایت", " gb ").replace("گیگ", " gb ")
    value = re.sub(r"(?<=\d)(?=[a-zA-Z])|(?<=[a-zA-Z])(?=\d)", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_text(value).split()
        if len(token) > 1 and token not in _STOP_WORDS
    }


def title_similarity(query: str, title: str) -> float:
    query_tokens = _tokens(query)
    title_tokens = _tokens(title)
    if not query_tokens or not title_tokens:
        return 0
    overlap = query_tokens & title_tokens
    containment = len(overlap) / len(query_tokens)
    jaccard = len(overlap) / len(query_tokens | title_tokens)
    score = containment * 0.75 + jaccard * 0.25

    # Model numbers, storage, and package weight are high-value identity signals.
    query_numbers = {token for token in query_tokens if any(ch.isdigit() for ch in token)}
    if query_numbers:
        numeric_coverage = len(query_numbers & title_tokens) / len(query_numbers)
        score *= 0.25 + 0.75 * numeric_coverage
    normalized_query = normalize_text(query)
    normalized_title = normalize_text(title)
    bundle_words = ("بسته", "پک", "عددی", "جفت")
    if any(word in normalized_title for word in bundle_words) and not any(
        word in normalized_query for word in bundle_words
    ):
        score *= 0.45
    return round(score, 4)


def _first_image(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, dict):
        for key in ("url", "webp_url", "original", "md", "MEDIUM", "SMALL"):
            found = _first_image(value.get(key))
            if found:
                return found
    return ""


def parse_torob(payload: dict[str, Any], query: str) -> list[MarketListing]:
    listings: list[MarketListing] = []
    for item in payload.get("results") or []:
        price = item.get("price")
        title = " ".join(filter(None, [item.get("name1"), item.get("name2")])).strip()
        if not title or not isinstance(price, (int, float)) or price <= 0:
            continue
        path = item.get("web_client_absolute_url") or ""
        url = path if str(path).startswith("http") else f"https://torob.com{path}"
        listings.append(
            MarketListing(
                source="torob",
                title=title,
                price=int(price),  # Torob's response and price_text are in toman.
                url=url,
                image_url=_first_image(item.get("image_url") or item.get("media_urls")),
                similarity=title_similarity(query, title),
            )
        )
    return listings


def parse_digikala(payload: dict[str, Any], query: str) -> list[MarketListing]:
    products = (payload.get("data") or {}).get("products") or []
    listings: list[MarketListing] = []
    for item in products:
        variant = item.get("default_variant") or {}
        price_data = variant.get("price") or {}
        rial_price = price_data.get("selling_price")
        title = (item.get("title_fa") or "").strip()
        if not title or not isinstance(rial_price, (int, float)) or rial_price <= 0:
            continue
        uri = (item.get("url") or {}).get("uri") or ""
        url = uri if str(uri).startswith("http") else f"https://www.digikala.com{uri}"
        listings.append(
            MarketListing(
                source="digikala",
                title=title,
                price=int(rial_price / 10),  # Digikala reports rial; UI uses toman.
                url=url,
                image_url=_first_image(item.get("images")),
                similarity=title_similarity(query, title),
            )
        )
    return listings


def parse_basalam(payload: Any, query: str) -> list[MarketListing]:
    if isinstance(payload, dict):
        products = payload.get("products") or payload.get("data") or payload.get("results") or []
    else:
        products = payload if isinstance(payload, list) else []
    listings: list[MarketListing] = []
    for item in products:
        price = item.get("price") or item.get("primaryPrice") or item.get("primary_price")
        title = (item.get("name") or item.get("title") or "").strip()
        if not title or not isinstance(price, (int, float)) or price <= 0:
            continue
        product_id = item.get("id")
        listings.append(
            MarketListing(
                source="basalam",
                title=title,
                # Basalam's public search value is rial (the storefront displays toman).
                price=int(price / 10),
                url=f"https://basalam.com/p/{product_id}" if product_id else "https://basalam.com",
                image_url=_first_image(item.get("photo")),
                similarity=title_similarity(query, title),
            )
        )
    return listings


class MarketCrawler:
    """Small, rate-conscious adapters over marketplace public search responses."""

    def __init__(self, cache_seconds: int = 600) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def search(self, query: str) -> dict[str, Any]:
        key = normalize_text(query)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        headers = {
            "Accept": "application/json",
            "Accept-Language": "fa-IR,fa;q=0.9",
            "User-Agent": "Nerkhban/0.2 marketplace-price-research",
        }
        timeout = httpx.Timeout(12, connect=7)
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            # Desktop proxy variables frequently point to a local SOCKS service.
            # Marketplace crawling should use a direct connection unless the
            # operator explicitly opts into environment proxy settings.
            trust_env=settings.marketplace_trust_env,
        ) as client:
            tasks = [
                self._torob(client, query),
                self._digikala(client, query),
                self._basalam(client, query),
            ]
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        listings: list[MarketListing] = []
        statuses: list[SourceStatus] = []
        sources = ("torob", "digikala", "basalam")
        for source, outcome in zip(sources, outcomes, strict=True):
            if isinstance(outcome, Exception):
                statuses.append(SourceStatus(source, False, 0, "دسترسی موقتاً ناموفق بود"))
                continue
            listings.extend(outcome)
            statuses.append(SourceStatus(source, True, len(outcome)))

        # Relevance threshold is intentionally permissive for short generic queries.
        query_size = len(_tokens(query))
        threshold = 0.48 if query_size >= 3 else 0.34
        relevant = [item for item in listings if item.similarity >= threshold]
        relevant.sort(key=lambda item: (-item.similarity, item.price))

        result = {
            "listings": relevant[:60],
            "sources": statuses,
            "raw_count": len(listings),
        }
        async with self._lock:
            self._cache[key] = (time.monotonic(), result)
        return result

    async def _torob(self, client: httpx.AsyncClient, query: str) -> list[MarketListing]:
        payload = await self._request_json(
            client,
            "GET",
            TOROB_SEARCH_URL,
            params={"q": query, "sort": "popularity", "size": 24},
        )
        return parse_torob(payload, query)

    async def _digikala(self, client: httpx.AsyncClient, query: str) -> list[MarketListing]:
        payload = await self._request_json(
            client,
            "GET",
            DIGIKALA_SEARCH_URL,
            params={"q": query, "page": 1},
        )
        return parse_digikala(payload, query)

    async def _basalam(self, client: httpx.AsyncClient, query: str) -> list[MarketListing]:
        payload = await self._request_json(
            client,
            "POST",
            BASALAM_SEARCH_URL,
            json={"q": query, "rows": 24, "start": 0},
        )
        return parse_basalam(payload, query)

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                if attempt == 0:
                    await asyncio.sleep(0.35)
        assert last_error is not None
        raise last_error


def _percentile(values: list[int], ratio: float) -> float:
    if len(values) == 1:
        return float(values[0])
    position = ratio * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _round_toman(value: float) -> int:
    step = 1_000 if value < 1_000_000 else 10_000
    return max(step, int(round(value / step) * step))


def analyze_listings(listings: list[MarketListing]) -> dict[str, Any]:
    positive = [item for item in listings if item.price > 0]
    if len(positive) < 3:
        raise ValueError("برای محاسبه بازه قیمت، حداقل سه نتیجه مشابه لازم است.")

    prices = sorted(item.price for item in positive)
    if len(prices) >= 4:
        q1 = _percentile(prices, 0.25)
        q3 = _percentile(prices, 0.75)
        iqr = q3 - q1
        low_fence, high_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        retained = [item for item in positive if low_fence <= item.price <= high_fence]
    else:
        retained = positive
    if len(retained) < 3:
        retained = positive

    retained.sort(key=lambda item: item.price)
    prices = [item.price for item in retained]
    scale_low = _round_toman(float(prices[0]))
    scale_high = _round_toman(float(prices[-1]))
    quick = _round_toman(_percentile(prices, 0.25))
    fair = _round_toman(float(median(prices)))
    patient = _round_toman(_percentile(prices, 0.75))
    sources = {item.source for item in retained}
    dispersion = (patient - quick) / fair if fair else 1
    confidence = min(95, 20 + min(45, len(retained) * 3) + len(sources) * 10)
    if dispersion > 0.65:
        confidence -= 15
    confidence = max(20, confidence)

    counts = {
        source: sum(1 for item in retained if item.source == source)
        for source in ("torob", "digikala", "basalam")
    }
    return {
        "range": {"low": quick, "high": patient},
        "scale": {"low": scale_low, "high": scale_high},
        "recommended": fair,
        "positions": {
            "quick": quick,
            "fair": fair,
            "patient": patient,
        },
        "confidence": confidence,
        "sample_size": len(retained),
        "excluded_count": len(positive) - len(retained),
        "source_counts": counts,
        "listings": [item.public_dict() for item in retained[:18]],
        "method": "IQR + P25/P50/P75",
    }


market_crawler = MarketCrawler()
