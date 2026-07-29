from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import settings

logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
logger = logging.getLogger("qeimatyar.scheduler")


async def _run_merchant_sync(client: httpx.AsyncClient) -> None:
    try:
        response = await client.post(
            "http://app:8000/internal/merchant-sync",
            headers={"X-Cron-Secret": settings.cron_secret},
        )
        response.raise_for_status()
        logger.info("Merchant refresh completed: %s", response.text)
    except Exception:
        logger.exception("Scheduled merchant refresh failed")


async def _run_usdt_rate_check(client: httpx.AsyncClient) -> None:
    try:
        response = await client.post(
            "http://app:8000/internal/usdt-rate-check",
            headers={"X-Cron-Secret": settings.cron_secret},
        )
        response.raise_for_status()
        logger.info("USDT rate check completed: %s", response.text)
    except Exception:
        logger.exception("Scheduled USDT rate check failed")


async def _run_every(
    *,
    name: str,
    interval_seconds: int,
    initial_delay_seconds: int,
    task,
) -> None:
    await asyncio.sleep(initial_delay_seconds)
    async with httpx.AsyncClient(timeout=1800, trust_env=False) as client:
        while True:
            started = time.perf_counter()
            logger.debug("Scheduled task started: %s", name)
            await task(client)
            logger.debug(
                "Scheduled task finished: %s elapsed_ms=%s",
                name,
                round((time.perf_counter() - started) * 1000),
            )
            await asyncio.sleep(interval_seconds)


async def run() -> None:
    interval = max(1, settings.merchant_sync_hours) * 60 * 60
    usdt_interval = max(1, settings.usdt_check_interval_minutes) * 60
    logger.info(
        "Scheduler started merchant_sync_interval_seconds=%s usdt_check_interval_seconds=%s log_level=%s",
        interval,
        usdt_interval,
        settings.log_level,
    )
    await asyncio.gather(
        _run_every(
            name="merchant-sync",
            interval_seconds=interval,
            initial_delay_seconds=10,
            task=_run_merchant_sync,
        ),
        _run_every(
            name="usdt-rate-check",
            interval_seconds=usdt_interval,
            initial_delay_seconds=15,
            task=_run_usdt_rate_check,
        ),
    )


if __name__ == "__main__":
    asyncio.run(run())
