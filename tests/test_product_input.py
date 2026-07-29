import asyncio

import pytest

from app.product_input import (
    ProductLinkError,
    basalam_product_id_from_url,
    resolve_product_query,
)


def test_plain_product_name_is_returned_unchanged():
    query, from_url = asyncio.run(resolve_product_query("  گوشی سامسونگ A55  "))
    assert query == "گوشی سامسونگ A55"
    assert not from_url


def test_product_title_is_read_from_safe_marketplace_link(monkeypatch):
    async def fake_html(_):
        return """
        <html><head>
          <meta property="og:title" content="گوشی سامسونگ A55 | دیجی‌کالا">
        </head></html>
        """

    monkeypatch.setattr("app.product_input._fetch_product_html", fake_html)
    query, from_url = asyncio.run(
        resolve_product_query("https://www.digikala.com/product/dkp-123/a55/")
    )
    assert query == "گوشی سامسونگ A55"
    assert from_url


def test_untrusted_product_link_is_rejected():
    with pytest.raises(ProductLinkError):
        asyncio.run(resolve_product_query("https://example.com/product/123"))


def test_basalam_product_id_is_read_from_link():
    assert basalam_product_id_from_url("https://basalam.com/p/456?ref=search") == 456
    assert basalam_product_id_from_url("https://www.basalam.com/product/789") == 789
    assert basalam_product_id_from_url("https://torob.com/p/456") is None
