import asyncio

from fastapi.testclient import TestClient

from app.basalam import decrypt_token, encrypt_token
from app.db import connection, init_db, now_iso
from app.digikala import DigikalaClient
from app.main import app
from app.marketplaces import MarketListing
from app.merchant_sync import merchant_sync
from app.sessions import COOKIE_NAME, read_session


SELLER_ID = 7612345
USER_ID = -SELLER_ID


def _cleanup():
    with connection() as db:
        db.execute("DELETE FROM accounts WHERE user_id=?", (USER_ID,))


def test_digikala_variants_reads_every_page(monkeypatch):
    client = DigikalaClient()
    pages = []

    async def fake_request(method, path, token, **kwargs):
        page = kwargs["params"]["page"]
        pages.append(page)
        return {
            "status": "ok",
            "data": {
                "items": [{"product_id": page}],
                "pager": {"total_pages": 3},
            },
        }

    monkeypatch.setattr(client, "_request", fake_request)
    variants = asyncio.run(client.variants("seller-token"))

    assert pages == [1, 2, 3]
    assert [item["product_id"] for item in variants] == [1, 2, 3]


def test_digikala_token_connection_validates_and_encrypts_token(monkeypatch):
    init_db()
    token = "dk-open-api-token-that-is-long-enough"

    async def fake_profile(received_token):
        assert received_token == token
        return {
            "seller_id": SELLER_ID,
            "seller_name": "فروشگاه تست دیجی‌کالا",
            "first_name": "کاربر",
            "last_name": "تست",
        }

    async def fake_sync(user_id):
        assert user_id == USER_ID
        return {"ok": True}

    monkeypatch.setattr("app.main.digikala.profile", fake_profile)
    monkeypatch.setattr("app.main.merchant_sync.sync_user", fake_sync)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/auth/digikala/token",
                data={"digikala_token": token},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/merchant"
        assert read_session(response.cookies.get(COOKIE_NAME)) == USER_ID
        with connection() as db:
            account = db.execute(
                "SELECT * FROM accounts WHERE user_id=?", (USER_ID,)
            ).fetchone()
        assert account["marketplace"] == "digikala"
        assert account["vendor_id"] == SELLER_ID
        assert decrypt_token(account["access_token"]) == token
        assert token not in account["access_token"]
    finally:
        _cleanup()


def test_digikala_sync_groups_variants_into_all_products(monkeypatch):
    init_db()
    _cleanup()
    with connection() as db:
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,
             sync_status,marketplace)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                USER_ID,
                SELLER_ID,
                "فروشگاه تست",
                "کاربر تست",
                encrypt_token("digikala-token"),
                now_iso(),
                "idle",
                "digikala",
            ),
        )

    async def fake_variants(token):
        assert token == "digikala-token"
        return [
            {
                "product_id": 101,
                "product_title": "محصول اول",
                "price_sale": 5_200_000,
                "marketplace_seller_stock": 2,
                "warehouse_stock": 1,
                "image_src": "https://example.com/one.jpg",
            },
            {
                "product_id": 101,
                "product_title": "محصول اول",
                "price_sale": 5_000_000,
                "marketplace_seller_stock": 4,
                "warehouse_stock": 0,
            },
            {
                "product_id": 102,
                "product_title": "محصول دوم",
                "price_sale": 7_000_000,
                "marketplace_seller_stock": 1,
                "warehouse_stock": 0,
            },
        ]

    async def fake_market_search(query):
        return {
            "listings": [
                MarketListing("torob", query, 450_000, "https://torob.com/a"),
                MarketListing("digikala", query, 500_000, "https://digikala.com/a"),
                MarketListing("basalam", query, 550_000, "https://basalam.com/a"),
            ],
            "sources": [],
            "raw_count": 3,
        }

    monkeypatch.setattr("app.merchant_sync.digikala.variants", fake_variants)
    monkeypatch.setattr("app.merchant_sync.market_crawler.search", fake_market_search)
    try:
        result = asyncio.run(merchant_sync.sync_user(USER_ID))
        assert result["ok"]
        assert result["products"] == 2
        with connection() as db:
            products = db.execute(
                """SELECT product_id,current_price,stock FROM merchant_products
                WHERE user_id=? ORDER BY product_id""",
                (USER_ID,),
            ).fetchall()
        assert [row["product_id"] for row in products] == [101, 102]
        assert products[0]["current_price"] == 500_000
        assert products[0]["stock"] == 7
    finally:
        _cleanup()
