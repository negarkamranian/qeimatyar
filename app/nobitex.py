from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import settings


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
        async with httpx.AsyncClient(
            timeout=20,
            trust_env=settings.marketplace_trust_env,
        ) as client:
            response = await client.get(f"{self.base_url}/v3/orderbook/USDTIRT")
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") != "ok":
            raise NobitexError("Nobitex returned a non-ok status.")
        return _rate_from_orderbook(payload)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise NobitexError("Nobitex returned an invalid price.") from exc


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
    return NobitexRate(
        symbol="USDTIRT",
        price_toman=int(price),
        source=source,
        last_update=payload.get("lastUpdate"),
    )


nobitex = NobitexClient()
