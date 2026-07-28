import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.basalam import encrypt_token
from app.db import connection, init_db, now_iso
from app.main import app
from app.marketplaces import MarketListing
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


def _cleanup():
    with connection() as db:
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
    finally:
        _cleanup()


def test_merchant_sync_reads_products_and_stores_estimate(monkeypatch):
    _insert_accounts_and_products()

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
