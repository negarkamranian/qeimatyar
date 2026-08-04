import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.basalam import encrypt_token
from app.db import connection, init_db, now_iso
from app.main import app
from app.marketplaces import MarketListing, SourceStatus
from app.merchant_sync import merchant_sync
from app.sessions import COOKIE_NAME, create_session


USER_ID = 91827364
OTHER_USER_ID = 91827365


def _insert_accounts_and_products():
    init_db()
    with connection() as db:
        for user_id in (USER_ID, OTHER_USER_ID):
            db.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))
            db.execute(
                """INSERT INTO accounts
                (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,
                 token_expires_at,sync_status)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    user_id + 10,
                    f"غرفه {user_id}",
                    "کاربر تست",
                    encrypt_token("test-token"),
                    now_iso(),
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    "idle",
                ),
            )
        db.execute(
            """INSERT INTO merchant_products
            (user_id,product_id,title,current_price,stock,market_low,
             market_suggested,market_high,source_counts,synced_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                USER_ID,
                7001,
                "محصول تست",
                500_000,
                3,
                450_000,
                500_000,
                550_000,
                "{}",
                now_iso(),
            ),
        )
        db.execute(
            """INSERT INTO merchant_products
            (user_id,product_id,title,current_price,stock,source_counts,synced_at)
            VALUES(?,?,?,?,?,?,?)""",
            (OTHER_USER_ID, 7002, "محصول خصوصی دیگر", 1, 1, "{}", now_iso()),
        )
        db.execute(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,created_at)
            VALUES(?,?,?,?,?,?)""",
            (
                USER_ID,
                "price_opportunity",
                "فرصت قیمت‌گذاری",
                "یک محصول به بازبینی قیمت نیاز دارد.",
                "/merchant",
                now_iso(),
            ),
        )
        db.execute(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,created_at)
            VALUES(?,?,?,?,?,?)""",
            (
                OTHER_USER_ID,
                "private",
                "اعلان خصوصی",
                "این اعلان نباید دیده شود.",
                "/merchant",
                now_iso(),
            ),
        )


def _cleanup():
    with connection() as db:
        db.execute(
            "DELETE FROM subscriptions WHERE customer_id IN (?,?)",
            (USER_ID, OTHER_USER_ID),
        )
        db.execute(
            "DELETE FROM accounts WHERE user_id IN (?,?)",
            (USER_ID, OTHER_USER_ID),
        )


def test_merchant_dashboard_is_private_and_range_is_tenant_scoped():
    _insert_accounts_and_products()
    try:
        with TestClient(app) as anonymous:
            assert anonymous.get("/api/merchant/dashboard").status_code == 401

        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, create_session(USER_ID))
            dashboard = client.get("/api/merchant/dashboard")
            assert dashboard.status_code == 200
            products = dashboard.json()["products"]
            assert [item["product_id"] for item in products] == [7001]

            updated = client.patch(
                "/api/merchant/products/7001/range",
                json={"min_price": 470_000, "max_price": 530_000},
            )
            assert updated.status_code == 200
            product = client.get("/api/merchant/dashboard").json()["products"][0]
            assert product["effective_min"] == 470_000
            assert product["effective_max"] == 530_000

            forbidden = client.patch(
                "/api/merchant/products/7002/range",
                json={"min_price": 1, "max_price": 2},
            )
            assert forbidden.status_code == 404

            notifications = client.get("/api/merchant/notifications")
            assert notifications.status_code == 200
            body = notifications.json()
            assert body["unread_count"] == 1
            assert [item["title"] for item in body["notifications"]] == [
                "فرصت قیمت‌گذاری"
            ]

            notification_id = body["notifications"][0]["id"]
            assert client.patch(
                f"/api/merchant/notifications/{notification_id}/read"
            ).status_code == 200
            assert client.get("/api/merchant/notifications").json()["unread_count"] == 0

            assert client.patch(
                "/api/merchant/notifications/999999/read"
            ).status_code == 404
    finally:
        _cleanup()


def test_premium_analytics_are_redacted_until_subscription_is_active():
    _insert_accounts_and_products()
    with connection() as db:
        db.execute(
            """UPDATE merchant_products SET competitor_snapshot=?
            WHERE user_id=? AND product_id=?""",
            (
                '[{"source":"torob","title":"رقیب","price":600000,"url":"https://torob.com/x"}]',
                USER_ID,
                7001,
            ),
        )
        db.execute(
            """INSERT INTO merchant_sales_events
            (user_id,order_item_id,product_id,quantity,unit_price,sold_at,synced_at)
            VALUES(?,?,?,?,?,?,?)""",
            (USER_ID, 88001, 7001, 2, 450_000, now_iso(), now_iso()),
        )
    try:
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, create_session(USER_ID))
            free = client.get("/api/merchant/dashboard").json()
            assert free["premium"]["active"] is False
            assert free["premium"]["analytics"] is None
            assert free["premium"]["teaser"]["has_sales_history"] is True
            assert free["products"][0]["premium_analytics"] is None
            assert "competitor_snapshot" not in free["products"][0]

            with connection() as db:
                db.execute(
                    """INSERT INTO subscriptions
                    (customer_id,subscription_id,plan_id,status,updated_at)
                    VALUES(?,?,?,?,?)""",
                    (USER_ID, 1, 1, "active", now_iso()),
                )

            premium = client.get("/api/merchant/dashboard").json()
            assert premium["premium"]["active"] is True
            assert premium["premium"]["analytics"]["tracked_sales"] == 2
            assert premium["premium"]["analytics"]["estimated_revenue_opportunity"] == 100_000
            product_analytics = premium["products"][0]["premium_analytics"]
            assert product_analytics["tracked_sales_180d"] == 2
            assert product_analytics["competitors"][0]["source"] == "torob"
    finally:
        _cleanup()


def test_basalam_sync_persists_enrichment_sales_and_price_history(monkeypatch):
    _insert_accounts_and_products()

    async def fake_products(token, vendor_id):
        return [{
            "id": 7003,
            "title": "عسل ویژه",
            "price": 510_000,
            "inventory": 4,
            "photo": {},
            "category": {"title": "عسل"},
            "status": {"title": "منتشر شده"},
            "view_count": 120,
            "sales_count": 9,
            "review_count": 3,
            "rating": 4.7,
            "sku": "HONEY-1",
        }]

    async def fake_parcels(token, vendor_id):
        return [
            {
                "id": 991,
                "created_at": now_iso(),
                "status": {"id": 3238, "title": "ارسال شده"},
                "order": {"paid_at": now_iso(), "customer": {"mobile": "09120000000"}},
                "items": [{
                    "id": 881,
                    "quantity": 2,
                    "price": 490_000,
                    "product": {"id": 7003},
                }],
            },
            {
                "id": 992,
                "created_at": now_iso(),
                "status": {"id": 3067, "title": "لغو شده"},
                "order": {"paid_at": now_iso()},
                "items": [{
                    "id": 882,
                    "quantity": 5,
                    "price": 490_000,
                    "product": {"id": 7003},
                }],
            },
        ]

    async def fake_price_history(token, product_id, **kwargs):
        return [{
            "change_time": "2026-07-01T00:00:00+00:00",
            "price": 480_000,
            "discounted_price": 470_000,
        }]

    async def fake_market_search(query):
        return {
            "listings": [
                MarketListing("torob", query, 500_000, "https://torob.com/1", similarity=1),
                MarketListing("digikala", query, 520_000, "https://digikala.com/1", similarity=1),
                MarketListing("basalam", query, 540_000, "https://basalam.com/p/999", similarity=1),
            ],
            "sources": [],
            "raw_count": 3,
        }

    monkeypatch.setattr("app.merchant_sync.settings.scopes", "customer.profile.read vendor.profile.read vendor.product.read vendor.parcel.read")
    monkeypatch.setattr("app.merchant_sync.basalam.products", fake_products)
    monkeypatch.setattr("app.merchant_sync.basalam.vendor_parcels", fake_parcels)
    monkeypatch.setattr("app.merchant_sync.basalam.product_price_history", fake_price_history)
    monkeypatch.setattr("app.merchant_sync.market_crawler.search", fake_market_search)
    try:
        result = asyncio.run(merchant_sync.sync_user(USER_ID))
        assert result["analytics"] == {"status": "ready", "sales": 1, "prices": 1}
        with connection() as db:
            product = db.execute(
                "SELECT * FROM merchant_products WHERE user_id=? AND product_id=7003",
                (USER_ID,),
            ).fetchone()
            sale = db.execute(
                "SELECT * FROM merchant_sales_events WHERE user_id=? AND order_item_id=881",
                (USER_ID,),
            ).fetchone()
            point = db.execute(
                "SELECT * FROM merchant_product_price_points WHERE user_id=? AND product_id=7003",
                (USER_ID,),
            ).fetchone()
        assert product["category_title"] == "عسل"
        assert product["view_count"] == 120
        assert product["sales_count"] == 9
        assert sale["quantity"] == 2
        assert sale["unit_price"] == 490_000
        assert "09120000000" not in str(dict(sale))
        assert point["price"] == 480_000
    finally:
        _cleanup()


def test_merchant_sync_reads_products_and_stores_estimate(monkeypatch):
    _insert_accounts_and_products()
    monkeypatch.setattr(
        "app.merchant_sync.settings.scopes",
        "customer.profile.read vendor.profile.read vendor.product.read",
    )

    async def fake_products(token, vendor_id):
        assert token == "test-token"
        return [
            {
                "id": 7003,
                "title": "عسل آویشن ۹۰۰ گرم",
                "price": 510_000,
                "inventory": 4,
                "photo": {},
            }
        ]

    async def fake_market_search(query):
        return {
            "listings": [
                MarketListing("torob", query, 450_000, "", similarity=1),
                MarketListing("digikala", query, 500_000, "", similarity=1),
                MarketListing("basalam", query, 550_000, "", similarity=1),
            ],
            "sources": [],
            "raw_count": 3,
        }

    monkeypatch.setattr("app.merchant_sync.basalam.products", fake_products)
    monkeypatch.setattr("app.merchant_sync.market_crawler.search", fake_market_search)
    try:
        result = asyncio.run(merchant_sync.sync_user(USER_ID))
        assert result["ok"]
        with connection() as db:
            product = db.execute(
                """SELECT * FROM merchant_products
                WHERE user_id=? AND product_id=7003""",
                (USER_ID,),
            ).fetchone()
        assert product["market_suggested"] == 500_000
        assert product["market_low"] == 475_000
        assert product["market_high"] == 525_000
    finally:
        _cleanup()


def test_merchant_price_refresh_does_not_fetch_product_catalog(monkeypatch):
    _insert_accounts_and_products()

    async def catalog_must_not_be_called(*_):
        raise AssertionError("price refresh must not fetch the Basalam product catalog")

    async def fake_market_search(query):
        return {
            "listings": [
                MarketListing("torob", query, 600_000, "", similarity=1),
                MarketListing("digikala", query, 650_000, "", similarity=1),
                MarketListing("basalam", query, 700_000, "", similarity=1),
            ],
            "sources": [],
            "raw_count": 3,
        }

    monkeypatch.setattr("app.merchant_sync.basalam.products", catalog_must_not_be_called)
    monkeypatch.setattr("app.merchant_sync.market_crawler.search", fake_market_search)
    try:
        result = asyncio.run(merchant_sync.refresh_prices(USER_ID))
        assert result["ok"]
        with connection() as db:
            product = db.execute(
                """SELECT market_suggested FROM merchant_products
                WHERE user_id=? AND product_id=7001""",
                (USER_ID,),
            ).fetchone()
        assert product["market_suggested"] == 650_000
    finally:
        _cleanup()


def test_merchant_analysis_reads_current_price_server_side(monkeypatch):
    _insert_accounts_and_products()

    async def fake_market_search(_):
        return {
            "listings": [
                MarketListing(
                    "basalam",
                    "محصول خود غرفه",
                    900_000,
                    "https://basalam.com/p/7001",
                    similarity=1,
                    external_id="7001",
                ),
                MarketListing("torob", "محصول تست", 450_000, "", similarity=1),
                MarketListing("digikala", "محصول تست", 500_000, "", similarity=1),
                MarketListing("basalam", "محصول مشابه", 550_000, "", similarity=1),
            ],
            "sources": [
                SourceStatus("torob", True, 1),
                SourceStatus("digikala", True, 1),
                SourceStatus("basalam", True, 2),
            ],
            "raw_count": 4,
        }

    monkeypatch.setattr("app.main.market_crawler.search", fake_market_search)
    try:
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, create_session(USER_ID))
            response = client.post(
                "/api/market/analyze",
                json={
                    "product_name": "محصول تست",
                    "exclude_basalam_product_id": 7001,
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["merchant_product"]["current_price"] == 500_000
        assert body["analysis"]["recommended"] == 500_000
        assert body["analysis"]["sample_size"] == 3
        assert all(
            listing["url"] != "https://basalam.com/p/7001"
            for listing in body["analysis"]["listings"]
        )
    finally:
        _cleanup()
