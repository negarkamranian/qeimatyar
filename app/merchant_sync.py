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


def _nested_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("title", "name", "description"):
            if value.get(key):
                return str(value[key])
    return ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _basalam_product(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize the rich vendor-product response while keeping raw PII out."""
    category = item.get("category")
    status = item.get("status")
    return {
        "id": int(item["id"]),
        "title": item.get("title") or item.get("name") or "محصول بدون نام",
        "price": _safe_int(item.get("price") or item.get("primary_price")),
        "stock": _safe_int(item.get("inventory") or item.get("stock")),
        "image_url": _image_url(item.get("photo")),
        "category_title": _nested_label(category),
        "status_title": _nested_label(status),
        "view_count": _safe_int(item.get("view_count")),
        "sales_count": _safe_int(item.get("sales_count")),
        "review_count": _safe_int(item.get("review_count")),
        "rating": _safe_float(item.get("rating")),
        "product_created_at": item.get("created_at"),
        "product_updated_at": item.get("updated_at"),
        "product_url": item.get("url") or f"https://basalam.com/p/{item['id']}",
        "sku": item.get("sku"),
        "preparation_day": _safe_int(
            item.get("preparation_day") or item.get("preparation_days")
        ),
        "net_weight": _safe_float(
            item.get("net_weight_decimal") or item.get("net_weight")
        ),
        "packaged_weight": _safe_float(
            item.get("packaged_weight") or item.get("package_weight")
        ),
        "raw_enrichment": {
            "published": item.get("published"),
            "can_add_to_cart": item.get("can_add_to_cart"),
            "has_variation": item.get("has_variation"),
            "unit_quantity": item.get("unit_quantity"),
            "unit_type": item.get("unit_type"),
            "discount": item.get("discount"),
            "is_wholesale": item.get("is_wholesale"),
        },
    }


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

        if token.startswith("public-seller:"):
            catalog = await digikala.public_catalog(token.partition(":")[2])
            return self._public_digikala_products(catalog["products"])

        variants = await digikala.variants(token)
        products: dict[int, dict[str, Any]] = {}
        for item in variants:
            try:
                product_id = int(item.get("product_id") or 0)
            except (TypeError, ValueError):
                continue
            if product_id <= 0:
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

    def _public_digikala_products(
        self, products: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in products:
            try:
                product_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            variant = item.get("default_variant") or {}
            price_data = variant.get("price") or {}
            try:
                price_rial = int(price_data.get("selling_price") or 0)
                stock = int(
                    variant.get("marketable_stock")
                    or price_data.get("marketable_stock")
                    or 0
                )
            except (TypeError, ValueError):
                price_rial = 0
                stock = 0
            main_image = (
                ((item.get("images") or {}).get("main") or {}).get("url") or []
            )
            normalized.append(
                {
                    "id": product_id,
                    "title": (
                        item.get("title_fa")
                        or item.get("title_en")
                        or "محصول بدون نام"
                    ),
                    "price": round(price_rial / settings.digikala_price_divisor),
                    "stock": stock,
                    "image_url": (
                        main_image[0]
                        if isinstance(main_image, list) and main_image
                        else ""
                    ),
                }
            )
        return normalized

    async def _sync_basalam_analytics(
        self,
        user_id: int,
        account: dict[str, Any],
        token: str,
        products: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist sales facts and own-price history for scenario analysis.

        Customer identity/address fields returned with parcels are deliberately
        ignored. Existing OAuth grants without ``vendor.parcel.read`` continue
        to sync the catalog and are marked as needing renewed consent.
        """
        if account.get("marketplace") != "basalam":
            return {"status": "not_applicable", "sales": 0, "prices": 0}
        if "vendor.parcel.read" not in settings.scopes.split():
            with connection() as db:
                db.execute(
                    """UPDATE accounts SET analytics_status='needs_consent',
                    analytics_error=? WHERE user_id=?""",
                    (
                        "برای دریافت تاریخچه فروش، اتصال باسلام را با دسترسی خواندن سفارش‌ها تمدید کنید.",
                        user_id,
                    ),
                )
            return {"status": "needs_consent", "sales": 0, "prices": 0}

        product_ids = {
            int(product["id"])
            for product in products[: max(1, settings.merchant_product_limit)]
        }
        start = datetime.now(timezone.utc) - timedelta(days=180)
        start_iso = start.isoformat()
        end_iso = datetime.now(timezone.utc).isoformat()
        with connection() as db:
            db.execute(
                """UPDATE accounts SET analytics_status='running',
                analytics_error=NULL WHERE user_id=?""",
                (user_id,),
            )

        sales_error: Exception | None = None
        try:
            parcels = await basalam.vendor_parcels(token, int(account["vendor_id"]))
        except Exception as exc:
            sales_error = exc
            parcels = []

        semaphore = asyncio.Semaphore(5)

        async def history(product_id: int) -> tuple[int, list[dict[str, Any]] | Exception]:
            async with semaphore:
                try:
                    points = await basalam.product_price_history(
                        token,
                        product_id,
                        start_time=start_iso,
                        end_time=end_iso,
                    )
                    return product_id, points
                except Exception as exc:
                    return product_id, exc

        histories = await asyncio.gather(*(history(product_id) for product_id in product_ids))
        price_errors = [result for _, result in histories if isinstance(result, Exception)]
        sales_written = 0
        prices_written = 0
        synced_at = now_iso()
        with connection() as db:
            for parcel in parcels:
                order = parcel.get("order") or {}
                sold_at = order.get("paid_at") or parcel.get("created_at")
                if not sold_at or str(sold_at) < start_iso:
                    continue
                parcel_status_value = parcel.get("status")
                parcel_status = _nested_label(parcel_status_value)
                parcel_status_id = _safe_int(
                    parcel_status_value.get("id")
                    if isinstance(parcel_status_value, dict)
                    else None
                )
                if parcel_status_id in {3067, 3233, 3572} or any(
                    marker in parcel_status for marker in ("لغو", "عودت", "تحویل نشده")
                ):
                    # Cancelled, definitively dissatisfied, refunded or not-delivered
                    # parcels are not realized sales and must not inflate opportunity.
                    continue
                for item in parcel.get("items") or []:
                    item_product = item.get("product") or {}
                    product_id = _safe_int(item_product.get("id") or item.get("product_id"))
                    order_item_id = _safe_int(item.get("id"))
                    if product_id not in product_ids or order_item_id <= 0:
                        continue
                    db.execute(
                        """INSERT INTO merchant_sales_events
                        (user_id,order_item_id,product_id,quantity,unit_price,
                         sold_at,parcel_status,synced_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,order_item_id) DO UPDATE SET
                          product_id=excluded.product_id,quantity=excluded.quantity,
                          unit_price=excluded.unit_price,sold_at=excluded.sold_at,
                          parcel_status=excluded.parcel_status,
                          synced_at=excluded.synced_at""",
                        (
                            user_id,
                            order_item_id,
                            product_id,
                            max(1, _safe_int(item.get("quantity"), 1)),
                            _safe_int(item.get("price")),
                            str(sold_at),
                            parcel_status,
                            synced_at,
                        ),
                    )
                    sales_written += 1

            for product_id, result in histories:
                if isinstance(result, Exception):
                    continue
                for point in result:
                    changed_at = point.get("change_time") or point.get("changed_at")
                    price = _safe_int(point.get("price"))
                    if not changed_at or price <= 0:
                        continue
                    db.execute(
                        """INSERT INTO merchant_product_price_points
                        (user_id,product_id,changed_at,price,discounted_price,synced_at)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(user_id,product_id,changed_at) DO UPDATE SET
                          price=excluded.price,
                          discounted_price=excluded.discounted_price,
                          synced_at=excluded.synced_at""",
                        (
                            user_id,
                            product_id,
                            str(changed_at),
                            price,
                            _safe_int(point.get("discounted_price")) or None,
                            synced_at,
                        ),
                    )
                    prices_written += 1

            has_any_success = sales_error is None or len(price_errors) < len(histories)
            error_message = None
            if sales_error is not None:
                if isinstance(sales_error, BasalamError) and sales_error.status_code in {401, 403}:
                    error_message = "دسترسی تاریخچه فروش باسلام نیاز به اتصال دوباره دارد."
                else:
                    error_message = "بخشی از تاریخچه فروش فعلاً دریافت نشد."
            elif price_errors:
                error_message = "بخشی از تاریخچه قیمت محصولات فعلاً دریافت نشد."
            db.execute(
                """UPDATE accounts SET analytics_synced_at=?,analytics_status=?,
                analytics_error=? WHERE user_id=?""",
                (
                    synced_at if has_any_success else None,
                    "partial" if error_message else "ready",
                    error_message,
                    user_id,
                ),
            )
        return {
            "status": "partial" if sales_error or price_errors else "ready",
            "sales": sales_written,
            "prices": prices_written,
        }

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
                            product = _basalam_product(item)
                        normalized.append(product)
                        db.execute(
                            """INSERT INTO merchant_products
                            (user_id,product_id,title,current_price,stock,image_url,
                             category_title,status_title,view_count,sales_count,
                             review_count,rating,product_created_at,product_updated_at,
                             product_url,sku,preparation_day,net_weight,packaged_weight,
                             raw_enrichment,synced_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,product_id) DO UPDATE SET
                              title=excluded.title,current_price=excluded.current_price,
                              stock=excluded.stock,image_url=excluded.image_url,
                              category_title=excluded.category_title,
                              status_title=excluded.status_title,
                              view_count=excluded.view_count,
                              sales_count=excluded.sales_count,
                              review_count=excluded.review_count,rating=excluded.rating,
                              product_created_at=excluded.product_created_at,
                              product_updated_at=excluded.product_updated_at,
                              product_url=excluded.product_url,sku=excluded.sku,
                              preparation_day=excluded.preparation_day,
                              net_weight=excluded.net_weight,
                              packaged_weight=excluded.packaged_weight,
                              raw_enrichment=excluded.raw_enrichment,
                              synced_at=excluded.synced_at""",
                            (
                                user_id,
                                product["id"],
                                product["title"],
                                product["price"],
                                product["stock"],
                                product["image_url"],
                                product.get("category_title"),
                                product.get("status_title"),
                                product.get("view_count", 0),
                                product.get("sales_count", 0),
                                product.get("review_count", 0),
                                product.get("rating"),
                                product.get("product_created_at"),
                                product.get("product_updated_at"),
                                product.get("product_url"),
                                product.get("sku"),
                                product.get("preparation_day"),
                                product.get("net_weight"),
                                product.get("packaged_weight"),
                                json.dumps(product.get("raw_enrichment") or {}),
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

                analytics = await self._sync_basalam_analytics(
                    user_id,
                    account,
                    token,
                    normalized,
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
                            captured_at = now_iso()
                            competitor_snapshot = json.dumps(analysis["listings"][:12])
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET
                                    market_low=?,market_suggested=?,market_high=?,
                                    confidence=?,sample_size=?,source_counts=?,
                                    competitor_snapshot=?,estimate_error=NULL,estimated_at=?
                                    WHERE user_id=? AND product_id=?""",
                                    (
                                        analysis["range"]["low"],
                                        analysis["recommended"],
                                        analysis["range"]["high"],
                                        analysis["confidence"],
                                        analysis["sample_size"],
                                        json.dumps(analysis["source_counts"]),
                                        competitor_snapshot,
                                        captured_at,
                                        user_id,
                                        product["id"],
                                    ),
                                )
                                db.execute(
                                    """INSERT INTO merchant_market_snapshots
                                    (user_id,product_id,recommended_price,market_low,
                                     market_high,listings,captured_at)
                                    VALUES(?,?,?,?,?,?,?)""",
                                    (
                                        user_id,
                                        product["id"],
                                        analysis["recommended"],
                                        analysis["range"]["low"],
                                        analysis["range"]["high"],
                                        competitor_snapshot,
                                        captured_at,
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
                    "analytics": analytics,
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
                            captured_at = now_iso()
                            competitor_snapshot = json.dumps(analysis["listings"][:12])
                            with connection() as db:
                                db.execute(
                                    """UPDATE merchant_products SET
                                    market_low=?,market_suggested=?,market_high=?,
                                    confidence=?,sample_size=?,source_counts=?,
                                    competitor_snapshot=?,estimate_error=NULL,estimated_at=?
                                    WHERE user_id=? AND product_id=?""",
                                    (
                                        analysis["range"]["low"],
                                        analysis["recommended"],
                                        analysis["range"]["high"],
                                        analysis["confidence"],
                                        analysis["sample_size"],
                                        json.dumps(analysis["source_counts"]),
                                        competitor_snapshot,
                                        captured_at,
                                        user_id,
                                        product["product_id"],
                                    ),
                                )
                                db.execute(
                                    """INSERT INTO merchant_market_snapshots
                                    (user_id,product_id,recommended_price,market_low,
                                     market_high,listings,captured_at)
                                    VALUES(?,?,?,?,?,?,?)""",
                                    (
                                        user_id,
                                        product["product_id"],
                                        analysis["recommended"],
                                        analysis["range"]["low"],
                                        analysis["range"]["high"],
                                        competitor_snapshot,
                                        captured_at,
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
