from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any
from urllib.parse import urlsplit
import httpx

from app.config import settings
from app.nobitex import nobitex


TOROB_SEARCH_URL = "https://api.torob.com/v4/base-product/search/"
DIGIKALA_SEARCH_URL = "https://api.digikala.com/v1/search/"
BASALAM_SEARCH_URL = "https://openapi.basalam.com/v1/products/search"
TRENDYOL_SEARCH_URL = (
    "https://public.trendyol.com/discovery-web-searchgw-service/v2/api/infinite-scroll/sr"
)
NOON_SEARCH_URL = "https://www.noon.com/_vs/nc/mp-customer-catalog-api/api/v3/u/search/"
USD_RATES_URL = "https://open.er-api.com/v6/latest/USD"

MARKETPLACE_SOURCES = (
    "torob",
    "digikala",
    "basalam",
    "trendyol",
    "noon_uae",
)

INTERNAL_MARKETPLACE_SOURCES = frozenset({"torob", "digikala", "basalam"})
FOREIGN_MARKETPLACE_SOURCES = frozenset({"trendyol", "noon_uae"})

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
    external_id: str = ""
    native_price: float | None = None
    native_currency: str = ""
    origin: str = "live"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("external_id", None)
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
        image = value.strip()
        return f"https:{image}" if image.startswith("//") else image
    if isinstance(value, list):
        for item in value:
            found = _first_image(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in (
            "main",
            "url",
            "webp_url",
            "thumbnail_url",
            "original",
            "md",
            "lg",
            "MEDIUM",
            "SMALL",
        ):
            found = _first_image(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _first_image(nested)
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
                external_id=str(product_id) if product_id is not None else "",
            )
        )
    return listings


def _nested_items(payload: Any, paths: tuple[tuple[str, ...], ...]) -> list[dict[str, Any]]:
    for path in paths:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.,]", "", value).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            tail = cleaned.rsplit(",", 1)[-1]
            cleaned = cleaned.replace(",", "." if len(tail) <= 2 else "")
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def parse_trendyol(
    payload: dict[str, Any],
    query: str,
    toman_per_try: float,
) -> list[MarketListing]:
    products = _nested_items(
        payload,
        (
            ("result", "products"),
            ("result", "content", "products"),
            ("data", "products"),
            ("products",),
        ),
    )
    listings: list[MarketListing] = []
    for item in products:
        title = (item.get("name") or item.get("title") or "").strip()
        price_data = item.get("price") or {}
        if not isinstance(price_data, dict):
            price_data = {}
        discounted = price_data.get("discountedPrice") or {}
        if not isinstance(discounted, dict):
            discounted = {}
        native_price = _number(
            discounted.get("value")
            or price_data.get("sellingPrice")
            or price_data.get("salePrice")
            or item.get("salePrice")
            or item.get("price")
        )
        if not title or native_price is None:
            continue
        path = item.get("url") or item.get("productUrl") or ""
        url = str(path) if str(path).startswith("http") else f"https://www.trendyol.com{path}"
        product_id = item.get("id") or item.get("productId") or item.get("contentId")
        listings.append(
            MarketListing(
                source="trendyol",
                title=title,
                price=max(1, round(native_price * toman_per_try)),
                url=url,
                image_url=_first_image(item.get("images") or item.get("image")),
                similarity=title_similarity(query, title),
                external_id=str(product_id) if product_id is not None else "",
                native_price=native_price,
                native_currency="TRY",
            )
        )
    return listings


def parse_noon(
    payload: dict[str, Any],
    query: str,
    *,
    source: str,
    currency: str,
    toman_per_unit: float,
) -> list[MarketListing]:
    products = _nested_items(
        payload,
        (
            ("hits",),
            ("data", "hits"),
            ("data", "products"),
            ("results",),
            ("products",),
        ),
    )
    listings: list[MarketListing] = []
    for item in products:
        title = (item.get("name") or item.get("title") or item.get("product_title") or "").strip()
        price_data = item.get("price") or {}
        if not isinstance(price_data, dict):
            price_data = {}
        native_price = _number(
            item.get("sale_price")
            or item.get("price")
            or price_data.get("sale_price")
            or price_data.get("value")
            or item.get("offer_price")
        )
        if not title or native_price is None:
            continue
        sku = item.get("sku") or item.get("catalog_sku") or item.get("id")
        path = str(item.get("url") or item.get("product_url") or "")
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            url = f"https://www.noon.com{path}"
        elif path and sku:
            locale = {"noon_uae": "uae-en"}.get(source, "uae-en")
            url = f"https://www.noon.com/{locale}/{path.strip('/')}/{sku}/p/"
        elif sku:
            url = f"https://www.noon.com/uae-en/{sku}/p/"
        else:
            url = "https://www.noon.com"
        listings.append(
            MarketListing(
                source=source,
                title=title,
                price=max(1, round(native_price * toman_per_unit)),
                url=url,
                image_url=_first_image(item.get("image_url") or item.get("image") or item.get("images")),
                similarity=title_similarity(query, title),
                external_id=str(sku) if sku is not None else "",
                native_price=native_price,
                native_currency=currency,
            )
        )
    return listings


def ensure_noon_uae(payload: dict[str, Any]) -> None:
    """Reject a response that explicitly identifies a non-UAE Noon storefront.

    Noon's customer catalog endpoint selects its storefront from the caller's
    public egress IP and normally defaults unknown locations to UAE. Most
    responses do not include a country field, so absence is accepted; an
    explicit country/currency mismatch is not.
    """
    search = payload.get("search") or {}
    if not isinstance(search, dict):
        search = {}
    explicit_values = {
        str(value).strip().lower()
        for value in (
            payload.get("country"),
            payload.get("country_code"),
            payload.get("currency"),
            payload.get("locale"),
            search.get("country"),
            search.get("country_code"),
            search.get("currency"),
            search.get("locale"),
        )
        if value
    }
    uae_values = {"ae", "uae", "united arab emirates", "aed", "en-ae", "ar-ae"}
    non_uae_values = {
        "sa", "ksa", "saudi", "saudi arabia", "sar", "en-sa", "ar-sa",
        "eg", "egypt", "egp", "en-eg", "ar-eg",
    }
    if explicit_values & non_uae_values and not explicit_values & uae_values:
        raise ValueError("پاسخ نون مربوط به بازار امارات نیست")


class CurrencyConverter:
    """Convert MENA storefront currencies to toman with a short-lived cache."""

    def __init__(self, cache_seconds: int = 1800) -> None:
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict[str, float]] | None = None
        self._lock = asyncio.Lock()

    async def toman_rate(self, currency: str, client: httpx.AsyncClient) -> float:
        override = getattr(settings, f"{currency.lower()}_toman_rate", None)
        if override:
            return float(override)
        rates = await self._live_rates(client)
        try:
            return rates[currency]
        except KeyError as exc:
            raise ValueError(f"نرخ تبدیل {currency} در دسترس نیست") from exc

    async def _live_rates(self, client: httpx.AsyncClient) -> dict[str, float]:
        cached = self._cache
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        async with self._lock:
            cached = self._cache
            if cached and time.monotonic() - cached[0] < self.cache_seconds:
                return cached[1]
            fx_response, usdt_rate = await asyncio.gather(
                client.get(USD_RATES_URL),
                nobitex.usdt_irt_rate(),
            )
            fx_response.raise_for_status()
            payload = fx_response.json()
            usd_rates = payload.get("rates") or {}
            converted: dict[str, float] = {}
            for currency in ("TRY", "AED"):
                units_per_usd = _number(usd_rates.get(currency))
                if units_per_usd:
                    converted[currency] = usdt_rate.price_toman / units_per_usd
            if len(converted) != 2:
                raise ValueError("پاسخ سرویس نرخ ارز کامل نیست")
            self._cache = (time.monotonic(), converted)
            return converted


def exclude_marketplace_product(
    listings: list[MarketListing],
    source: str,
    external_id: int | str,
) -> list[MarketListing]:
    identifier = str(external_id)

    def is_same_product(listing: MarketListing) -> bool:
        if listing.source != source:
            return False
        if listing.external_id and listing.external_id == identifier:
            return True
        # Backward-compatible fallback for cached/adapted listings without an ID.
        return identifier in {
            segment for segment in urlsplit(listing.url).path.split("/") if segment
        }

    return [listing for listing in listings if not is_same_product(listing)]


class MarketCrawler:
    """Small, rate-conscious adapters over marketplace public search responses."""

    def __init__(self, cache_seconds: int = 600) -> None:
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._currencies = CurrencyConverter()

    async def search(
        self,
        query: str,
        source_queries: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        source_queries = source_queries or {}
        source_query = {
            source: source_queries.get(source, query)
            for source in MARKETPLACE_SOURCES
        }
        key = "|".join(
            f"{source}:{normalize_text(source_query[source])}"
            for source in MARKETPLACE_SOURCES
        )
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
                self._torob(client, source_query["torob"]),
                self._digikala(client, source_query["digikala"]),
                self._basalam(client, source_query["basalam"]),
                self._trendyol(client, source_query["trendyol"]),
                self._noon(client, source_query["noon_uae"]),
            ]
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        listings: list[MarketListing] = []
        statuses: list[SourceStatus] = []
        for source, outcome in zip(MARKETPLACE_SOURCES, outcomes, strict=True):
            if isinstance(outcome, Exception):
                statuses.append(SourceStatus(source, False, 0, "دسترسی موقتاً ناموفق بود"))
                continue
            listings.extend(outcome)
            statuses.append(SourceStatus(source, True, len(outcome)))

        # Relevance threshold is intentionally permissive for short generic queries.
        # When LLM similarity is enabled, skip filtering entirely and return all
        # results so the LLM can rank them without pre-filtering.
        if settings.llm_similarity_enabled:
            relevant = sorted(listings, key=lambda item: (-item.similarity, item.price))
        else:
            query_size = len(_tokens(query))
            threshold = 0.48 if query_size >= 3 else 0.34
            relevant = [item for item in listings if item.similarity >= threshold]
            relevant.sort(key=lambda item: (-item.similarity, item.price))

        result = {
            "listings": relevant[:72],
            "sources": statuses,
            "raw_count": len(listings),
            "search_queries": source_query,
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

    async def _trendyol(self, client: httpx.AsyncClient, query: str) -> list[MarketListing]:
        payload = await self._request_json(
            client,
            "GET",
            TRENDYOL_SEARCH_URL,
            params={"q": query, "pi": 1, "culture": "tr-TR", "channelId": 1},
        )
        rate = await self._currencies.toman_rate("TRY", client)
        return parse_trendyol(payload, query, rate)

    async def _noon(
        self,
        client: httpx.AsyncClient,
        query: str,
    ) -> list[MarketListing]:
        payload = await self._request_json(
            client,
            "GET",
            NOON_SEARCH_URL,
            params={"q": query, "limit": 24, "page": 1},
        )
        ensure_noon_uae(payload)
        rate = await self._currencies.toman_rate("AED", client)
        return parse_noon(
            payload,
            query,
            source="noon_uae",
            currency="AED",
            toman_per_unit=rate,
        )

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


def _estimate_elasticity(price: int, recommended: int, low: int, high: int) -> dict[str, float]:
    if not recommended or recommended <= 0:
        return {"demand_change_pct": 0.0, "revenue_change_pct": 0.0, "elasticity": 0.0}
    spread = max(high - low, 1)
    distance_pct = ((price - recommended) / max(recommended, 1)) * 100
    elasticity = 1.0
    if abs(distance_pct) < 3:
        elasticity = 0.7
    elif abs(distance_pct) < 8:
        elasticity = 1.1
    elif abs(distance_pct) < 15:
        elasticity = 1.4
    else:
        elasticity = 1.8

    if price > recommended:
        demand_change_pct = -min(35.0, abs(distance_pct) * 0.35 * elasticity)
        revenue_change_pct = demand_change_pct + (distance_pct * 0.0)
    else:
        demand_change_pct = min(25.0, abs(distance_pct) * 0.25 * elasticity)
        revenue_change_pct = -min(35.0, abs(distance_pct) * 0.28 * elasticity)

    if price > recommended:
        revenue_change_pct = max(-35.0, revenue_change_pct)
    else:
        revenue_change_pct = max(-35.0, revenue_change_pct)

    return {
        "demand_change_pct": round(demand_change_pct, 1),
        "revenue_change_pct": round(revenue_change_pct, 1),
        "elasticity": round(elasticity, 2),
        "distance_pct": round(distance_pct, 1),
        "band_width_pct": round((spread / max(recommended, 1)) * 100, 1),
    }


def analyze_listings(
    listings: list[MarketListing],
    llm_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    has_llm = bool(llm_scores)
    positive = [item for item in listings if item.price > 0]
    if len(positive) < 3:
        raise ValueError("برای محاسبه بازه قیمت، حداقل سه نتیجه مشابه لازم است.")

    prices = sorted(item.price for item in positive)
    if len(prices) >= 4:
        q1 = _percentile(prices, 0.25)
        q3 = _percentile(prices, 0.75)
        iqr = q3 - q1
        if has_llm:
            retained = list(positive)
        else:
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
        for source in MARKETPLACE_SOURCES
    }
    elasticity = _estimate_elasticity(int(fair), int(fair), int(quick), int(patient))

    if has_llm:
        display_listings = sorted(
            retained,
            key=lambda item: -(llm_scores.get(item.url, 0)),
        )
        listing_dicts = []
        for item in display_listings:
            d = item.public_dict()
            d["llm_similarity"] = round(llm_scores.get(item.url, 0), 2)
            listing_dicts.append(d)
    else:
        listing_dicts = [item.public_dict() for item in retained]

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
        "listings": listing_dicts,
        "method": "LLM ranking" if has_llm else "IQR + P25/P50/P75",
        "elasticity": elasticity,
        "llm_similarity_enabled": has_llm,
    }


market_crawler = MarketCrawler()
