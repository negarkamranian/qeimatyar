import asyncio

from app.currency_notifications import check_usdt_rate_change
from app.db import connection, init_db, now_iso
from app.nobitex import NobitexRate, _rate_from_orderbook


USER_ID = 991001
OTHER_USER_ID = 991002


def _cleanup():
    with connection() as db:
        db.execute(
            "DELETE FROM accounts WHERE user_id IN (?,?)",
            (USER_ID, OTHER_USER_ID),
        )
        db.execute("DELETE FROM currency_rate_state WHERE symbol='USDTIRT'")


def _insert_accounts():
    init_db()
    _cleanup()
    with connection() as db:
        for user_id in (USER_ID, OTHER_USER_ID):
            db.execute(
                """INSERT INTO accounts
                (user_id,vendor_id,vendor_title,access_token,connected_at)
                VALUES(?,?,?,?,?)""",
                (
                    user_id,
                    user_id + 10,
                    f"غرفه {user_id}",
                    "encrypted-token-not-used",
                    now_iso(),
                ),
            )


def test_usdt_rate_change_creates_notifications_after_threshold():
    _insert_accounts()
    prices = iter([100_000, 101_200, 101_300])

    async def fake_fetcher():
        return NobitexRate(
            symbol="USDTIRT",
            price_toman=next(prices),
            source="test",
            last_update=1,
        )

    try:
        first = asyncio.run(check_usdt_rate_change(fetcher=fake_fetcher, threshold_percent=1))
        assert first["baseline_created"]
        assert first["notified_accounts"] == 0

        second = asyncio.run(check_usdt_rate_change(fetcher=fake_fetcher, threshold_percent=1))
        assert second["notified"]
        assert second["notified_accounts"] == 2
        assert second["change_percent"] == 1.2

        third = asyncio.run(check_usdt_rate_change(fetcher=fake_fetcher, threshold_percent=1))
        assert not third["notified"]
        assert third["notified_accounts"] == 0

        with connection() as db:
            notifications = db.execute(
                """SELECT user_id,kind,title,body,metadata FROM merchant_notifications
                WHERE user_id IN (?,?) ORDER BY user_id""",
                (USER_ID, OTHER_USER_ID),
            ).fetchall()
            state = db.execute(
                "SELECT last_notified_price_toman FROM currency_rate_state WHERE symbol='USDTIRT'"
            ).fetchone()
        assert len(notifications) == 2
        assert {row["kind"] for row in notifications} == {"currency_rate_change"}
        assert all("USDT" in row["body"] for row in notifications)
        assert all("افزایش" in row["title"] for row in notifications)
        assert state["last_notified_price_toman"] == 101_200
    finally:
        _cleanup()


def test_nobitex_orderbook_rate_prefers_last_trade_price():
    rate = _rate_from_orderbook(
        {
            "status": "ok",
            "lastUpdate": 123,
            "lastTradePrice": "625000",
            "asks": [["626000", "10"]],
            "bids": [["624000", "10"]],
        }
    )
    assert rate.symbol == "USDTIRT"
    assert rate.price_toman == 625_000
    assert rate.source == "lastTradePrice"
