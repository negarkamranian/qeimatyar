from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Awaitable, Callable, Any

from app.config import settings
from app.db import connection, now_iso, rows
from app.nobitex import NobitexRate, nobitex

RateFetcher = Callable[[], Awaitable[NobitexRate]]
logger = logging.getLogger(__name__)


def _fa_number(value: int | float) -> str:
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def _change_percent(previous: int, current: int) -> float:
    return ((current - previous) / previous) * 100


def _notification_text(change_percent: float, previous: int, current: int) -> tuple[str, str]:
    direction = "افزایش" if change_percent > 0 else "کاهش"
    action = "افزایش بدهی" if change_percent > 0 else "کاهش بدهی"
    amount = abs(change_percent)
    title = f"سطح قیمت‌ها در بازار تغییر کرده"
    body = (
        f"نرخ ارز نسبت به آخرین هشدار {_fa_number(amount)}٪ {direction} داشته است. "
        f"با توجه به تغییر سطح قیمت‌ها در بازار، بهتر است قیمت محصولاتت را برای افزایش یا کاهش بازنگری کنی. "
        f"نرخ قبلی: {previous:,} تومان، نرخ جدید: {current:,} تومان."
    )
    return title, body


def _notify_merchants(rate: NobitexRate, change_percent: float, previous: int) -> int:
    accounts = rows("SELECT user_id FROM accounts ORDER BY user_id")
    if not accounts:
        return 0
    title, body = _notification_text(change_percent, previous, rate.price_toman)
    created_at = now_iso()
    metadata = json.dumps(
        {
            "symbol": rate.symbol,
            "previous_price_toman": previous,
            "current_price_toman": rate.price_toman,
            "change_percent": round(change_percent, 4),
            "source": rate.source,
            "last_update": rate.last_update,
        },
        ensure_ascii=False,
    )
    with connection() as db:
        db.executemany(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,metadata,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    account["user_id"],
                    "currency_rate_change",
                    title,
                    body,
                    "/merchant",
                    metadata,
                    created_at,
                )
                for account in accounts
            ],
        )
    return len(accounts)


def _upsert_state(
    rate: NobitexRate,
    *,
    notified: bool,
    keep_notified_price: int | None,
) -> None:
    checked_at = now_iso()
    notified_at = checked_at if notified else None
    notified_price = rate.price_toman if notified else keep_notified_price
    with connection() as db:
        db.execute(
            """INSERT INTO currency_rate_state
            (symbol,last_price_toman,last_notified_price_toman,last_checked_at,last_notified_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
              last_price_toman=excluded.last_price_toman,
              last_notified_price_toman=excluded.last_notified_price_toman,
              last_checked_at=excluded.last_checked_at,
              last_notified_at=COALESCE(excluded.last_notified_at, currency_rate_state.last_notified_at)""",
            (
                rate.symbol,
                rate.price_toman,
                notified_price,
                checked_at,
                notified_at,
            ),
        )


async def check_usdt_rate_change(
    *,
    fetcher: RateFetcher | None = None,
    threshold_percent: float | None = None,
) -> dict[str, Any]:
    threshold = threshold_percent if threshold_percent is not None else settings.usdt_notification_percent
    rate = await (fetcher or nobitex.usdt_irt_rate)()
    state = rows(
        """SELECT last_price_toman,last_notified_price_toman
        FROM currency_rate_state WHERE symbol=?""",
        (rate.symbol,),
    )
    if not state:
        _upsert_state(rate, notified=False, keep_notified_price=rate.price_toman)
        logger.info(
            "usdt_rate_baseline_created symbol=%s price_toman=%s source=%s",
            rate.symbol,
            rate.price_toman,
            rate.source,
        )
        return {
            "ok": True,
            "symbol": rate.symbol,
            "price_toman": rate.price_toman,
            "baseline_created": True,
            "notified_accounts": 0,
        }

    baseline = state[0]["last_notified_price_toman"] or state[0]["last_price_toman"]
    change = _change_percent(int(baseline), rate.price_toman)
    should_notify = settings.usdt_notification_enabled and abs(change) >= threshold
    logger.info(
        "usdt_rate_checked symbol=%s current_price_toman=%s baseline_price_toman=%s "
        "change_percent=%s threshold_percent=%s notification_enabled=%s notified=%s",
        rate.symbol,
        rate.price_toman,
        int(baseline),
        round(change, 4),
        threshold,
        settings.usdt_notification_enabled,
        should_notify,
    )
    notified_accounts = _notify_merchants(rate, change, int(baseline)) if should_notify else 0
    if should_notify:
        logger.info(
            "usdt_rate_notification_created symbol=%s notified_accounts=%s current_price_toman=%s "
            "baseline_price_toman=%s change_percent=%s",
            rate.symbol,
            notified_accounts,
            rate.price_toman,
            int(baseline),
            round(change, 4),
        )
    else:
        logger.debug(
            "usdt_rate_notification_skipped symbol=%s reason=below_threshold current_price_toman=%s "
            "baseline_price_toman=%s change_percent=%s",
            rate.symbol,
            rate.price_toman,
            int(baseline),
            round(change, 4),
        )
    _upsert_state(
        rate,
        notified=should_notify,
        keep_notified_price=int(baseline),
    )
    return {
        "ok": True,
        "symbol": rate.symbol,
        "price_toman": rate.price_toman,
        "previous_price_toman": int(baseline),
        "change_percent": round(change, 4),
        "threshold_percent": threshold,
        "notified": should_notify,
        "notified_accounts": notified_accounts,
        "rate": asdict(rate),
    }
