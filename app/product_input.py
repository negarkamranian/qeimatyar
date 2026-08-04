from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app.config import settings

ALLOWED_PRODUCT_HOSTS = {
    "basalam.com",
    "www.basalam.com",
    "torob.com",
    "www.torob.com",
    "digikala.com",
    "www.digikala.com",
    "trendyol.com",
    "www.trendyol.com",
    "noon.com",
    "www.noon.com",
}
MAX_HTML_BYTES = 750_000


class ProductLinkError(ValueError):
    pass


class _ProductTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_titles: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._inside_title = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in {"og:title", "twitter:title"} and attributes.get("content"):
                self.meta_titles[key] = attributes["content"]
        elif tag.lower() == "title":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title_parts.append(data)

    def product_title(self) -> str:
        return (
            self.meta_titles.get("og:title")
            or self.meta_titles.get("twitter:title")
            or " ".join(self.title_parts)
        )


def _validated_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProductLinkError("ساختار لینک محصول معتبر نیست.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or host not in ALLOWED_PRODUCT_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        raise ProductLinkError(
            "فقط لینک محصول از باسلام، ترب، دیجی‌کالا، ترندیول یا نون قابل بررسی است."
        )
    return value


def _clean_title(value: str) -> str:
    title = " ".join(value.split()).strip()
    title = re.sub(
        r"\s*[|–—]\s*(?:دیجی‌کالا|ترب|باسلام|ترندیول|نون|Trendyol|Noon).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return title[:160].strip()


def _title_from_path(value: str) -> str:
    segments = [
        unquote(segment).strip()
        for segment in urlsplit(value).path.split("/")
        if segment.strip()
    ]
    for segment in reversed(segments):
        if re.fullmatch(r"(?:dkp[-_]?)?\d+|[0-9a-f-]{20,}", segment, re.I):
            continue
        if segment.lower() in {"p", "product", "products"}:
            continue
        candidate = _clean_title(segment.replace("-", " ").replace("_", " "))
        if len(candidate) >= 3 and any(character.isalpha() for character in candidate):
            return candidate
    return ""


def basalam_product_id_from_url(value: str) -> int | None:
    parsed = urlsplit(value.strip())
    if (parsed.hostname or "").lower() not in {"basalam.com", "www.basalam.com"}:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in {"p", "product", "products"}:
            candidate = segments[index + 1]
            if candidate.isdigit():
                return int(candidate)
    return None


async def _fetch_product_html(value: str) -> str:
    current_url = _validated_url(value)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fa-IR,fa;q=0.9",
        "User-Agent": "Qeimatyar/0.3 product-link-preview",
    }
    timeout = httpx.Timeout(12, connect=7)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
        trust_env=settings.marketplace_trust_env,
    ) as client:
        for _ in range(4):
            async with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ProductLinkError("لینک محصول به مقصد معتبری هدایت نشد.")
                    current_url = _validated_url(urljoin(current_url, location))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise ProductLinkError("این لینک یک صفحه محصول قابل خواندن نیست.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = MAX_HTML_BYTES - len(content)
                    if remaining <= 0:
                        break
                    content.extend(chunk[:remaining])
                encoding = response.encoding or "utf-8"
                return bytes(content).decode(encoding, errors="replace")
    raise ProductLinkError("تعداد انتقال‌های لینک محصول بیش از حد بود.")


async def resolve_product_query(value: str) -> tuple[str, bool]:
    raw_value = " ".join(value.split()).strip()
    if len(raw_value) < 2:
        raise ProductLinkError("نام یا لینک محصول را کامل‌تر وارد کنید.")
    parsed = urlsplit(raw_value)
    is_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if not is_url:
        return raw_value[:160], False

    _validated_url(raw_value)
    fallback = _title_from_path(raw_value)
    try:
        html = await _fetch_product_html(raw_value)
        parser = _ProductTitleParser()
        parser.feed(html)
        title = _clean_title(parser.product_title())
    except (httpx.HTTPError, UnicodeError, ProductLinkError):
        title = fallback
    if title.lower() in {
        "باسلام", "ترب", "دیجی‌کالا", "ترندیول", "نون",
        "digikala", "torob", "trendyol", "noon",
    }:
        title = fallback
    if len(title) < 2:
        raise ProductLinkError(
            "عنوان این لینک خوانده نشد؛ نام محصول را به‌جای لینک وارد کنید."
        )
    return title, True
