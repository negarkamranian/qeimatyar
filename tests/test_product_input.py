import asyncio

import pytest

from app.product_input import (
    ProductLinkError,
    _title_from_path,
    basalam_product_id_from_url,
    resolve_product_query,
)


def test_plain_product_name_is_returned_unchanged():
    query, from_url = asyncio.run(resolve_product_query("  کاور آیفون  "))
    assert query == "کاور آیفون"
    assert not from_url


def test_product_title_is_read_from_safe_marketplace_link(monkeypatch):
    async def fake_html(_):
        return """
        <html><head>
          <meta property="og:title" content="کاور آیفون | دیجی‌کالا">
        </head></html>
        """

    monkeypatch.setattr("app.product_input._fetch_product_html", fake_html)
    query, from_url = asyncio.run(
        resolve_product_query("https://www.digikala.com/product/dkp-123/a55/")
    )
    assert query == "کاور آیفون"
    assert from_url


def test_untrusted_product_link_is_rejected():
    with pytest.raises(ProductLinkError):
        asyncio.run(resolve_product_query("https://example.com/product/123"))


def test_trendyol_and_noon_product_links_are_allowed(monkeypatch):
    async def fake_html(value):
        marketplace = "Trendyol" if "trendyol" in value else "Noon"
        return f'<meta property="og:title" content="Apple iPhone 15 | {marketplace}">'

    monkeypatch.setattr("app.product_input._fetch_product_html", fake_html)
    trendyol = asyncio.run(
        resolve_product_query("https://www.trendyol.com/apple/iphone-15-p-123")
    )
    noon = asyncio.run(
        resolve_product_query("https://www.noon.com/uae-en/apple-iphone-15/N123/p/")
    )
    assert trendyol == ("Apple iPhone 15", True)
    assert noon == ("Apple iPhone 15", True)


def test_noon_fallback_uses_slug_instead_of_sku():
    url = (
        "https://www.noon.com/uae-en/floral-flush-blush-palette-6-color-pressed-powder/"
        "N70083872V/p/?o=tracking"
    )
    assert _title_from_path(url) == "floral flush blush palette 6 color pressed powder"


def test_noon_tracking_query_is_removed_before_fetch(monkeypatch):
    seen = []

    async def unavailable_html(value):
        seen.append(value)
        raise ProductLinkError("unavailable")

    monkeypatch.setattr("app.product_input._fetch_product_html", unavailable_html)
    query, from_url = asyncio.run(
        resolve_product_query(
            "https://www.noon.com/uae-en/floral-flush-blush-palette/N70083872V/p/"
            "?o=abc&pcl=very-long-tracking-value"
        )
    )
    assert seen == [
        "https://www.noon.com/uae-en/floral-flush-blush-palette/N70083872V/p/"
    ]
    assert query == "floral flush blush palette"
    assert from_url


def test_basalam_product_id_is_read_from_link():
    assert basalam_product_id_from_url("https://basalam.com/p/456?ref=search") == 456
    assert basalam_product_id_from_url("https://www.basalam.com/product/789") == 789
    assert basalam_product_id_from_url("https://torob.com/p/456") is None
