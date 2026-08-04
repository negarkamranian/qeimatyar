from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.basalam import BasalamError, basalam, decrypt_token, encrypt_token
from app.config import settings
from app.db import connection, now_iso, rows
from app.digikala import digikala
from app.marketplaces import (
    INTERNAL_MARKETPLACE_SOURCES,
    analyze_listings,
    exclude_marketplace_product,
    market_crawler,
)

logger = logging.getLogger(__name__)


def token_expiry_iso(expires_in: int | None) -> str:
    seconds = max(300, int(expires_in or 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _image_url(photo: Any) -> str:
    if isinstance(photo, str):
        return photo
    if isinstance(photo, dict):
        for key in ("md", "lg", "original", "MEDIUM", "SMALL"):
            value = photo.get(key)
            if value:
                return str(value)
    return ""


class MerchantSyncService:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _valid_token(self, account: dict[str, Any]) -> str:
        encrypted_access = account["access_token"]
        if account.get("marketplace") == "digikala":
            return decrypt_token(encrypted_access)
        expires_at = account.get("token_expires_at")
        should_refresh = False
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                should_refresh = expiry <= datetime.now(timezone.utc) + timedelta(minutes=5)
            except ValueError:
                should_refresh = False
        if not should_refresh:
            return decrypt_token(encrypted_access)

        encrypted_refresh = account.get("refresh_token")
        if not encrypted_refresh:
            return decrypt_token(encrypted_access)
        token_data = await basalam.refresh_access_token(decrypt_token(encrypted_refresh))
        access = token_data["access_token"]
        refresh = token_data.get("refresh_token")
        with connection() as db:
            db.execute(
                """UPDATE accounts SET access_token=?, refresh_token=?,
                token_expires_at=? WHERE user_id=?""",
                (
                    encrypt_token(access),
                    encrypt_token(refresh) if refresh else encrypted_refresh,
                    token_expiry_iso(token_data.get("expires_in")),
                    account["user_id"],
                ),
            )
        return access

    async def _remote_products(
        self,
        account: dict[str, Any],
        token: str,
    ) -> list[dict[str, Any]]:
        if account.get("marketplace") != "digikala":
            return await basalam.products(token, account["vendor_id"])

        variants = await digikala.variants(token)
        products: dict[int, dict[str, Any]] = {}
        for item in variants:
            try:
                product_id = int(item.get("product_id") or 0)
            except (TypeError, ValueError):
                continue
            if product_id <= 0:
                continue
            price_rial = int(
                item.get("price_sale")
                or item.get("cash_selling_price")
                or item.get("price_list")
                or 0
            )
            price = round(price_rial / settings.digikala_price_divisor)
            stock = int(item.get("marketplace_seller_stock") or 0) + int(
                item.get("warehouse_stock") or 0
            )
            product = products.setdefault(
                product_id,
                {
                    "id": product_id,
                    "title": item.get("product_title") or item.get("title") or "محصول بدون نام",
                    "price": price,
                    "stock": 0,
                    "image_url": item.get("image_src") or "",
                },
            )
            product["stock"] += stock
            if price > 0 and (not product["price"] or price < product["price"]):
                product["price"] = price
            if not product["image_url"] and item.get("image_src"):
                product["image_url"] = item["image_src"]
        return list(products.values())

    async def sync_user(self, user_id: int) -> dict[str, Any]:
        lock = self._lock(user_id)
        if lock.locked():
            return {"ok": True, "status": "already_running"}
        async with lock:
            account_rows = rows("SELECT * FROM accounts WHERE user_id=?", (user_id,))
            if not account_rows:
                return {"ok": False, "status": "account_not_found"}
            account = account_rows[0]
            with connection() as db:
                db.execute(
                    "UPDATE accounts SET sync_status='running',sync_error=NULL WHERE user_id=?",
                    (user_id,),
                )
            try:
                token = await self._valid_token(account)
                try:
                    remote_products = await self._remote_products(account, token)
                except BasalamError as exc:
                    if exc.status_code != 401 or not account.get("refresh_token"):
                        raise
                    token_data = await basalam.refresh_access_token(
                        decrypt_token(account["refresh_token"])
                    )
                    token = token_data["access_token"]
                    with connection() as db:
                        db.execute(
                            """UPDATE accounts SET access_token=?,refresh_token=?,
                            token_expires_at=? WHERE user_id=?""",
                            (
                                encrypt_token(token),
                                encrypt_token(token_data["refresh_token"])
                                if token_data.get("refresh_token")
                                else account["refresh_token"],
                                token_expiry_iso(token_data.get("expires_in")),
                                user_id,
                            ),
                        )
                    remote_products = await self._remote_products(account, token)

                sync_time = now_iso()
                normalized: list[dict[str, Any]] = []
                with connection() as db:
                    for item in remote_products:
                        if account.get("marketplace") == "digikala":
                            product = item
                        else:
                            product = {
                                "id": int(item["id"]),
                                "title": item.get("title") or item.get("name") or "محصول بدون نام",
                                "price": int(
                                    item.get("price") or item.get("primary_price") or 0
                                ),
                                "stock": int(item.get("inventory") or item.get("stock") or 0),
                                "image_url": _image_url(item.get("photo")),
                            }
                        normalized.append(product)
                        db.execute(
                            """INSERT INTO merchant_products
                            (user_id,product_id,title,current_price,stock,image_url,synced_at)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,product_id) DO UPDATE SET
                              title=excluded.title,current_price=excluded.current_price,
                              stock=excluded.stock,image_url=excluded.image_url,
                              synced_at=excluded.synced_at""",
                            (
                                user_id,
                                product["id"],
                                product["title"],
                                product["price"],
                                product["stock"],
                                product["image_url"],
                                sync_time,
                            ),
                        )
                    if normalized:
                        placeholders = ",".join("?" for _ in normalized)
                        db.execute(
                            f"""DELETE FROM merchant_products
                            WHERE user_id=? AND product_id NOT IN ({placeholders})""",
                            (user_id, *(item["id"] for item in normalized)),
                        )

                candidates = sorted(
                    normalized,
                    key=lambda item: (item["stock"] > 0, item["stock"]),
                    reverse=True,
                )[: max(1, settings.merchant_product_limit)]
                semaphore = asyncio.Semaphore(3)

                async def estimate(product: dict[str, Any]) -> bool:
                    async with semaphore:
                        try:
                            crawl = await market_crawler.search(product["title"])
                            comparable_listings = exclude_marketplace_product(
                                crawl["listings"],
                                account.get("marketplace") or "basalam",
                                product["id"],
                            )
                            comparable_listings = [
                                item for item in comparable_listings
                                if item.source in INTERNAL_MARKETPLACE_SOURCES
                            ]
                            analysis = analyze_listings(comparable_listings)
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET
                                    market_low=?,market_suggested=?,market_high=?,
                                    confidence=?,sample_size=?,source_counts=?,
                                    estimate_error=NULL,estimated_at=?
                                    WHERE user_id=? AND product_id=?""",
                                    (
                                        analysis["range"]["low"],
                                        analysis["recommended"],
                                        analysis["range"]["high"],
                                        analysis["confidence"],
                                        analysis["sample_size"],
                                        json.dumps(analysis["source_counts"]),
                                        now_iso(),
                                        user_id,
                                        product["id"],
                                    ),
                                )
                            return True
                        except Exception as exc:
                            logger.info(
                                "Estimate unavailable for product %s: %s",
                                product["id"],
                                exc,
                            )
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET estimate_error=?,
                                    estimated_at=? WHERE user_id=? AND product_id=?""",
                                    (
                                        "داده مشابه کافی پیدا نشد",
                                        now_iso(),
                                        user_id,
                                        product["id"],
                                    ),
                                )
                            return False

                estimated = await asyncio.gather(*(estimate(item) for item in candidates))
                with connection() as db:
                    db.execute(
                        """UPDATE accounts SET last_synced_at=?,sync_status='idle',
                        sync_error=NULL WHERE user_id=?""",
                        (now_iso(), user_id),
                    )
                return {
                    "ok": True,
                    "status": "complete",
                    "products": len(normalized),
                    "estimated": sum(estimated),
                }
            except Exception as exc:
                logger.exception("Merchant sync failed for user %s", user_id)
                marketplace_title = "دیجی‌کالا" if account.get("marketplace") == "digikala" else "باسلام"
                with connection() as db:
                    db.execute(
                        """UPDATE accounts SET sync_status='failed',sync_error=?
                        WHERE user_id=?""",
                        (f"همگام‌سازی با {marketplace_title} ناموفق بود", user_id),
                    )
                return {"ok": False, "status": "failed", "message": str(exc)}

    async def refresh_prices(self, user_id: int) -> dict[str, Any]:
        """Refresh market estimates without downloading the Basalam catalog again."""
        lock = self._lock(user_id)
        if lock.locked():
            return {"ok": True, "status": "already_running"}
        async with lock:
            account = rows(
                "SELECT user_id,marketplace FROM accounts WHERE user_id=?",
                (user_id,),
            )
            if not account:
                return {"ok": False, "status": "account_not_found"}
            products = rows(
                """SELECT product_id,title,stock FROM merchant_products
                WHERE user_id=? ORDER BY stock > 0 DESC, stock DESC""",
                (user_id,),
            )[: max(1, settings.merchant_product_limit)]
            with connection() as db:
                db.execute(
                    "UPDATE accounts SET sync_status='running',sync_error=NULL WHERE user_id=?",
                    (user_id,),
                )
            try:
                semaphore = asyncio.Semaphore(3)

                async def estimate(product: dict[str, Any]) -> bool:
                    async with semaphore:
                        try:
                            crawl = await market_crawler.search(product["title"])
                            comparable_listings = exclude_marketplace_product(
                                crawl["listings"],
                                account[0].get("marketplace") or "basalam",
                                product["product_id"],
                            )
                            comparable_listings = [
                                item for item in comparable_listings
                                if item.source in INTERNAL_MARKETPLACE_SOURCES
                            ]
                            analysis = analyze_listings(comparable_listings)
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET
                                    market_low=?,market_suggested=?,market_high=?,
                                    confidence=?,sample_size=?,source_counts=?,
                                    estimate_error=NULL,estimated_at=?
                                    WHERE user_id=? AND product_id=?""",
                                    (
                                        analysis["range"]["low"],
                                        analysis["recommended"],
                                        analysis["range"]["high"],
                                        analysis["confidence"],
                                        analysis["sample_size"],
                                        json.dumps(analysis["source_counts"]),
                                        now_iso(),
                                        user_id,
                                        product["product_id"],
                                    ),
                                )
                            return True
                        except Exception as exc:
                            logger.info(
                                "Price refresh unavailable for product %s: %s",
                                product["product_id"],
                                exc,
                            )
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET estimate_error=?,
                                    estimated_at=? WHERE user_id=? AND product_id=?""",
                                    (
                                        "داده مشابه کافی پیدا نشد",
                                        now_iso(),
                                        user_id,
                                        product["product_id"],
                                    ),
                                )
                            return False

                estimated = await asyncio.gather(*(estimate(item) for item in products))
                with connection() as db:
                    db.execute(
                        """UPDATE accounts SET last_synced_at=?,sync_status='idle',
                        sync_error=NULL WHERE user_id=?""",
                        (now_iso(), user_id),
                    )
                return {
                    "ok": True,
                    "status": "complete",
                    "products": len(products),
                    "estimated": sum(estimated),
                }
            except Exception as exc:
                logger.exception("Merchant price refresh failed for user %s", user_id)
                with connection() as db:
                    db.execute(
                        """UPDATE accounts SET sync_status='failed',sync_error=?
                        WHERE user_id=?""",
                        ("به‌روزرسانی قیمت‌های بازار ناموفق بود", user_id),
                    )
                return {"ok": False, "status": "failed", "message": str(exc)}

    async def sync_due_users(self) -> list[dict[str, Any]]:
        accounts = rows("SELECT user_id,last_synced_at,sync_status FROM accounts")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.merchant_sync_hours)
        due: list[int] = []
        for account in accounts:
            if account["sync_status"] == "running":
                continue
            last_synced = account.get("last_synced_at")
            if not last_synced:
                due.append(account["user_id"])
                continue
            try:
                if datetime.fromisoformat(last_synced) <= cutoff:
                    due.append(account["user_id"])
            except ValueError:
                due.append(account["user_id"])

        semaphore = asyncio.Semaphore(2)

        async def run(user_id: int) -> dict[str, Any]:
            async with semaphore:
                return await self.sync_user(user_id)

        return await asyncio.gather(*(run(user_id) for user_id in due))


merchant_sync = MerchantSyncService()
