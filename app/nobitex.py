from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class NobitexError(RuntimeError):
    pass


@dataclass(frozen=True)
class NobitexRate:
    symbol: str
    price_toman: int
    source: str
    last_update: int | None = None


class NobitexClient:
    base_url = "https://apiv2.nobitex.ir"

    async def usdt_irt_rate(self) -> NobitexRate:
        url = f"{self.base_url}/v3/orderbook/USDTIRT"
        started = time.perf_counter()
        logger.info("nobitex_request_started symbol=USDTIRT url=%s", url)
        async with httpx.AsyncClient(
            timeout=20,
            trust_env=settings.marketplace_trust_env,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") != "ok":
            raise NobitexError("Nobitex returned a non-ok status.")
        rate = _rate_from_orderbook(payload)
        logger.info(
            "nobitex_request_succeeded symbol=%s price_toman=%s source=%s elapsed_ms=%s",
            rate.symbol,
            rate.price_toman,
            rate.source,
            round((time.perf_counter() - started) * 1000),
        )
        logger.debug(
            "nobitex_orderbook_snapshot symbol=%s last_trade=%s best_ask=%s best_bid=%s last_update=%s",
            rate.symbol,
            payload.get("lastTradePrice"),
            _top_price(payload.get("asks")),
            _top_price(payload.get("bids")),
            payload.get("lastUpdate"),
        )
        return rate


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise NobitexError("Nobitex returned an invalid price.") from exc


def _top_price(levels: Any) -> Any:
    if isinstance(levels, list) and levels and isinstance(levels[0], list) and levels[0]:
        return levels[0][0]
    return None


def _rate_from_orderbook(payload: dict[str, Any]) -> NobitexRate:
    if payload.get("lastTradePrice"):
        price = _decimal(payload["lastTradePrice"])
        source = "lastTradePrice"
    else:
        asks = payload.get("asks") or []
        bids = payload.get("bids") or []
        if not asks or not bids:
            raise NobitexError("Nobitex orderbook has no usable price.")
        price = (_decimal(asks[0][0]) + _decimal(bids[0][0])) / Decimal("2")
        source = "midpoint"
    if price <= 0:
        raise NobitexError("Nobitex returned a non-positive price.")
    # Nobitex orderbook prices are returned in rial, but the product UI and
    # merchant notifications work in toman. Convert once at the boundary.
    price_toman = int(price / Decimal("10"))
    return NobitexRate(
        symbol="USDTIRT",
        price_toman=price_toman,
        source=source,
        last_update=payload.get("lastUpdate"),
    )


nobitex = NobitexClient()
