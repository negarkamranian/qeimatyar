from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qeimatyar.scheduler")


async def run() -> None:
    interval = max(1, settings.merchant_sync_hours) * 60 * 60
    await asyncio.sleep(10)
    while True:
        try:
            # A batch can include several booths and many marketplace lookups.
            async with httpx.AsyncClient(timeout=1800, trust_env=False) as client:
                response = await client.post(
                    "http://app:8000/internal/merchant-sync",
                    headers={"X-Cron-Secret": settings.cron_secret},
                )
                response.raise_for_status()
                logger.info("Merchant refresh completed: %s", response.text)
        except Exception:
            logger.exception("Scheduled merchant refresh failed")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(run())
