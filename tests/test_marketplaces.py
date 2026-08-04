import asyncio

from app.marketplaces import (
    MarketCrawler,
    MarketListing,
    analyze_listings,
    ensure_noon_uae,
    parse_basalam,
    parse_digikala,
    parse_noon,
    parse_torob,
    parse_trendyol,
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
                "name1": "کاور آیفون",
                # "name2": "ظرفیت 256 گیگ",
                "price": 25_500_000,
                "web_client_absolute_url": "/p/example/",
                "image_url": "https://image.torob.com/example.jpg",
            }
        ]
    }
    result = parse_torob(payload, "کاور آیفون")
    assert result[0].price == 25_500_000
    assert result[0].source == "torob"


def test_digikala_parser_converts_rial_to_toman():
    image_url = "https://dkstatics-public.digikala.com/product.jpg"
    payload = {
        "data": {
            "products": [
                {
                    "title_fa": "کاور آیفون",
                    "url": {"uri": "/product/dkp-1/"},
                    "default_variant": {"price": {"selling_price": 255_000_000}},
                    "images": {"main": {"url": [image_url]}},
                }
            ]
        }
    }
    result = parse_digikala(payload, "کاور آیفون")
    assert result[0].price == 25_500_000
    assert result[0].source == "digikala"
    assert result[0].image_url == image_url


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


def test_trendyol_parser_converts_try_to_toman_and_preserves_native_price():
    result = parse_trendyol(
        {
            "result": {
                "products": [
                    {
                        "id": 123,
                        "name": "Apple iPhone 15 128 GB",
                        "url": "/apple/iphone-15-p-123",
                        "image": "https://cdn.trendyol.com/iphone.jpg",
                        "price": {"discountedPrice": {"value": 50000}},
                    }
                ]
            }
        },
        "Apple iPhone 15 128 GB",
        2_500,
    )
    assert result[0].price == 125_000_000
    assert result[0].native_price == 50_000
    assert result[0].native_currency == "TRY"
    assert result[0].source == "trendyol"


def test_noon_parser_supports_uae_search_hits():
    result = parse_noon(
        {
            "hits": [
                {
                    "sku": "N123",
                    "name": "Apple iPhone 15 128GB",
                    "sale_price": 2500,
                    "url": "apple-iphone-15",
                    "image_url": "https://f.nooncdn.com/iphone.jpg",
                }
            ]
        },
        "Apple iPhone 15 128GB",
        source="noon_uae",
        currency="AED",
        toman_per_unit=25_000,
    )
    assert result[0].price == 62_500_000
    assert result[0].native_currency == "AED"
    assert result[0].url == "https://www.noon.com/uae-en/apple-iphone-15/N123/p/"


def test_noon_uae_guard_accepts_implicit_or_explicit_uae_payload():
    ensure_noon_uae({"hits": []})
    ensure_noon_uae({"country_code": "AE", "currency": "AED", "hits": []})


def test_noon_uae_guard_rejects_explicit_non_uae_payload():
    try:
        ensure_noon_uae({"country": "sa", "currency": "SAR", "hits": []})
    except ValueError as exc:
        assert "امارات" in str(exc)
    else:
        raise AssertionError("Saudi Noon payload must not be treated as UAE/AED")


def test_numeric_variant_mismatch_reduces_similarity():
    exact = title_similarity(
        "کاور آیفون 15",
        "کاور آیفون 15",
    )
    wrong_storage = title_similarity(
        "کاور آیفون 15",
        "کاور آیفون 14",
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
    monkeypatch.setattr(crawler, "_trendyol", no_results)

    async def no_noon_results(*_, **__):
        return []

    monkeypatch.setattr(crawler, "_noon", no_noon_results)

    result = asyncio.run(crawler.search("محصول آزمایشی"))
    assert result["raw_count"] == 0
    assert all(status.ok for status in result["sources"])


def test_crawler_routes_localized_queries_to_each_marketplace(monkeypatch):
    crawler = MarketCrawler()
    received = {}

    def recorder(source):
        async def search_source(_client, query, *args):
            received[source] = query
            return []
        return search_source

    monkeypatch.setattr(crawler, "_torob", recorder("torob"))
    monkeypatch.setattr(crawler, "_digikala", recorder("digikala"))
    monkeypatch.setattr(crawler, "_basalam", recorder("basalam"))
    monkeypatch.setattr(crawler, "_trendyol", recorder("trendyol"))
    monkeypatch.setattr(crawler, "_noon", recorder("noon_uae"))

    result = asyncio.run(
        crawler.search(
            "original English title",
            source_queries={
                "torob": "پالت رژگونه 6 رنگ",
                "digikala": "پالت رژگونه 6 رنگ",
                "basalam": "پالت رژگونه 6 رنگ",
                "trendyol": "6 renk allık paleti",
                "noon_uae": "6 color blush palette",
            },
        )
    )
    assert received == {
        "torob": "پالت رژگونه 6 رنگ",
        "digikala": "پالت رژگونه 6 رنگ",
        "basalam": "پالت رژگونه 6 رنگ",
        "trendyol": "6 renk allık paleti",
        "noon_uae": "6 color blush palette",
    }
    assert result["search_queries"] == received
