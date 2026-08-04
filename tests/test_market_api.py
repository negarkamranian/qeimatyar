from fastapi.testclient import TestClient

from app.main import app
from app.marketplaces import MarketListing, SourceStatus, market_crawler


def test_market_analysis_endpoint(monkeypatch):
    listings = [
        MarketListing("torob", "محصول نمونه", 450_000, "https://torob.com/a", similarity=1),
        MarketListing("digikala", "محصول نمونه", 480_000, "https://digikala.com/a", similarity=1),
        MarketListing("basalam", "محصول نمونه", 510_000, "https://basalam.com/a", similarity=1),
        MarketListing("torob", "محصول نمونه", 530_000, "https://torob.com/b", similarity=1),
    ]

    async def fake_search(query):
        return {
            "listings": listings,
            "sources": [
                SourceStatus("torob", True, 2),
                SourceStatus("digikala", True, 1),
                SourceStatus("basalam", True, 1),
            ],
            "raw_count": 4,
        }

    monkeypatch.setattr(market_crawler, "search", fake_search)
    with TestClient(app) as client:
        response = client.post(
            "/api/market/analyze",
            json={"product_name": "محصول نمونه"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis"]["recommended"] == 495_000
    assert body["analysis"]["sample_size"] == 4
    assert body["analysis"]["elasticity"]["elasticity"] >= 0.7
    assert len(body["sources"]) == 3


def test_market_recalculation_uses_remaining_comparables():
    payload = {
        "listings": [
            {"source": "torob", "title": "الف", "price": 400_000, "url": "https://torob.com/a", "similarity": 1},
            {"source": "digikala", "title": "ب", "price": 500_000, "url": "https://digikala.com/b", "similarity": 1},
            {"source": "basalam", "title": "ج", "price": 600_000, "url": "https://basalam.com/c", "similarity": 1},
        ]
    }
    with TestClient(app) as client:
        response = client.post("/api/market/recalculate", json=payload)

    assert response.status_code == 200
    analysis = response.json()["analysis"]
    assert analysis["recommended"] == 500_000
    assert analysis["sample_size"] == 3


def test_market_recalculation_requires_three_comparables():
    payload = {
        "listings": [
            {"source": "torob", "title": "الف", "price": 400_000, "url": "https://torob.com/a"},
            {"source": "digikala", "title": "ب", "price": 500_000, "url": "https://digikala.com/b"},
        ]
    }
    with TestClient(app) as client:
        response = client.post("/api/market/recalculate", json=payload)

    assert response.status_code == 422


def test_market_analysis_resolves_link_and_excludes_own_basalam_product(monkeypatch):
    async def fake_resolve(value):
        assert value == "https://basalam.com/p/42"
        return "محصول نمونه", True

    listings = [
        MarketListing(
            "basalam",
            "محصول خود غرفه",
            400_000,
            "https://basalam.com/product/42?ref=search",
            similarity=1,
            external_id="42",
        ),
        MarketListing("torob", "محصول نمونه", 450_000, "https://torob.com/a", similarity=1),
        MarketListing("digikala", "محصول نمونه", 480_000, "https://digikala.com/a", similarity=1),
        MarketListing("basalam", "محصول مشابه", 510_000, "https://basalam.com/p/43", similarity=1),
    ]

    async def fake_search(query):
        assert query == "محصول نمونه"
        return {
            "listings": listings,
            "sources": [
                SourceStatus("torob", True, 1),
                SourceStatus("digikala", True, 1),
                SourceStatus("basalam", True, 2),
            ],
            "raw_count": 4,
        }

    monkeypatch.setattr("app.main.resolve_product_query", fake_resolve)
    monkeypatch.setattr(market_crawler, "search", fake_search)
    with TestClient(app) as client:
        response = client.post(
            "/api/market/analyze",
            json={
                "product_name": "https://basalam.com/p/42",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resolved_from_url"]
    assert body["query"] == "محصول نمونه"
    assert body["analysis"]["recommended"] == 480_000
    assert body["analysis"]["sample_size"] == 3
    assert all(
        item["url"] != "https://basalam.com/product/42?ref=search"
        for item in body["analysis"]["listings"]
    )
