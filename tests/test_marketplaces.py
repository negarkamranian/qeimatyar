import asyncio

from app.marketplaces import (
    MarketCrawler,
    MarketListing,
    analyze_listings,
    parse_basalam,
    parse_digikala,
    parse_torob,
    exclude_marketplace_product,
    title_similarity,
)


def test_exclude_marketplace_product_uses_external_id_not_url_shape():
    listings = [
        MarketListing(
            "basalam",
            "محصول خود غرفه",
            100_000,
            "https://basalam.com/product/somewhere",
            external_id="42",
        ),
        MarketListing(
            "basalam",
            "محصول مشابه",
            120_000,
            "https://basalam.com/p/43",
            external_id="43",
        ),
    ]
    result = exclude_marketplace_product(listings, "basalam", 42)
    assert [listing.external_id for listing in result] == ["43"]


def test_torob_parser_keeps_toman_price():
    payload = {
        "results": [
            {
                "name1": "گوشی سامسونگ A55",
                "name2": "ظرفیت 256 گیگ",
                "price": 25_500_000,
                "web_client_absolute_url": "/p/example/",
                "image_url": "https://image.torob.com/example.jpg",
            }
        ]
    }
    result = parse_torob(payload, "سامسونگ A55 256 گیگ")
    assert result[0].price == 25_500_000
    assert result[0].source == "torob"


def test_digikala_parser_converts_rial_to_toman():
    payload = {
        "data": {
            "products": [
                {
                    "title_fa": "گوشی سامسونگ A55 ظرفیت 256 گیگ",
                    "url": {"uri": "/product/dkp-1/"},
                    "default_variant": {"price": {"selling_price": 255_000_000}},
                    "images": {},
                }
            ]
        }
    }
    result = parse_digikala(payload, "سامسونگ A55 256 گیگ")
    assert result[0].price == 25_500_000
    assert result[0].source == "digikala"


def test_basalam_parser_accepts_list_payload():
    result = parse_basalam(
        [{"id": 42, "name": "عسل آویشن 900 گرمی", "price": 4_900_000}],
        "عسل آویشن 900 گرم",
    )
    assert result[0].url.endswith("/42")
    assert result[0].price == 490_000


def test_basalam_parser_accepts_live_products_envelope():
    result = parse_basalam(
        {"products": [{"id": 42, "name": "عسل آویشن 900 گرمی", "price": 4_900_000}]},
        "عسل آویشن 900 گرم",
    )
    assert len(result) == 1


def test_numeric_variant_mismatch_reduces_similarity():
    exact = title_similarity(
        "گوشی سامسونگ A55 ظرفیت 256 گیگ",
        "گوشی موبایل سامسونگ Galaxy A55 256GB",
    )
    wrong_storage = title_similarity(
        "گوشی سامسونگ A55 ظرفیت 256 گیگ",
        "گوشی موبایل سامسونگ Galaxy A55 128GB",
    )
    assert exact > wrong_storage
    assert exact > 0.45


def test_bundle_is_penalized_when_not_requested():
    single = title_similarity("عسل آویشن 900 گرم", "عسل طبیعی آویشن 900 گرم")
    bundle = title_similarity("عسل آویشن 900 گرم", "عسل طبیعی آویشن 900 گرم بسته 2 عددی")
    assert bundle < single * 0.5


def test_market_analysis_removes_extreme_outlier():
    listings = [
        MarketListing("torob", "a", 440_000, ""),
        MarketListing("torob", "b", 460_000, ""),
        MarketListing("digikala", "c", 480_000, ""),
        MarketListing("digikala", "d", 500_000, ""),
        MarketListing("basalam", "e", 520_000, ""),
        MarketListing("basalam", "f", 540_000, ""),
        MarketListing("torob", "outlier", 4_900_000, ""),
    ]
    analysis = analyze_listings(listings)
    assert analysis["range"]["high"] < 1_000_000
    assert analysis["scale"] == {"low": 440_000, "high": 540_000}
    assert analysis["recommended"] == 490_000
    assert analysis["excluded_count"] == 1
    assert analysis["source_counts"]["basalam"] == 2


def test_crawler_does_not_require_socks_for_environment_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:10808")
    crawler = MarketCrawler()

    async def no_results(*_):
        return []

    monkeypatch.setattr(crawler, "_torob", no_results)
    monkeypatch.setattr(crawler, "_digikala", no_results)
    monkeypatch.setattr(crawler, "_basalam", no_results)

    result = asyncio.run(crawler.search("محصول آزمایشی"))
    assert result["raw_count"] == 0
    assert all(status.ok for status in result["sources"])
