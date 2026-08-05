from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import httpx

from app.basalam import BasalamError, basalam, decrypt_token, encrypt_token, fetch_basalam_product, fetch_basalam_store, _basalam_product_id_from_url, _basalam_store_id_from_url
from app.config import refresh_settings, save_admin_overrides, settings, _env_file_paths
from app.currency_notifications import check_usdt_rate_change
from app.db import connection, init_db, now_iso, rows, seed_demo
from app.digikala import DigikalaError, digikala
from app.elasticity import ElasticityPoint, analyze_elasticity
from app.llm import (
    WebSearchResult,
    optimize_marketplace_queries,
    optimize_search_query,
    score_product_similarity,
    web_search_products,
)
from app.marketplaces import (
    FOREIGN_MARKETPLACE_SOURCES,
    INTERNAL_MARKETPLACE_SOURCES,
    MarketListing,
    analyze_listings,
    exclude_marketplace_product,
    market_crawler,
)
from app.merchant_sync import merchant_sync, token_expiry_iso
from app.pricing import decide_reprice, recommend_price
from app.product_input import (
    ProductLinkError,
    basalam_product_id_from_url,
    resolve_product_query,
)
from app.sessions import COOKIE_NAME, SESSION_SECONDS, create_session, read_session

BASE = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.demo_mode:
        seed_demo()
    yield


app = FastAPI(title="دقیقه", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _admin_session(request: Request | None) -> bool:
    if request is None:
        return False
    return request.cookies.get("admin_session") == settings.secret


def admin_dashboard_context(request: Request | None) -> dict[str, Any]:
    users = rows(
        """SELECT a.user_id, a.vendor_id, a.vendor_title, a.user_name, a.marketplace, a.sync_status,
        a.last_synced_at, a.connected_at, a.sync_error, a.token_expires_at,
        COUNT(mp.product_id) AS product_count,
        GROUP_CONCAT(mp.title, ' | ') AS product_titles
        FROM accounts a
        LEFT JOIN merchant_products mp ON mp.user_id = a.user_id
        GROUP BY a.user_id
        ORDER BY a.connected_at DESC, a.user_id DESC""",
    )
    for user in users:
        user["is_active"] = user.get("sync_status") in {"running", "queued"}
        user["products_synced"] = user.get("product_count", 0) > 0
        user["product_titles"] = user.get("product_titles") or ""
        user["token_expired"] = (
            user.get("marketplace") != "digikala"
            and _is_token_expired(user.get("token_expires_at"))
        )
    return {
        "settings": settings,
        "admin_settings_file": settings.admin_settings_file,
        "users": users,
        "user_count": len(users),
        "active_users": sum(1 for user in users if user["is_active"]),
        "synced_users": sum(1 for user in users if user["products_synced"]),
        "expired_users": sum(1 for user in users if user["token_expired"]),
        "error_users": sum(1 for user in users if user.get("sync_error")),
        "attention_users": sum(
            1 for user in users if user["token_expired"] or user.get("sync_error")
        ),
        "total_products": sum(int(user.get("product_count") or 0) for user in users),
        "recent_searches": _recent_searches_count(),
        "net_feedback": _net_feedback(),
        "total_store_views": _total_store_views(),
    }


def _is_token_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _recent_searches_count() -> int:
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with connection() as db:
            return db.execute(
                """SELECT COUNT(*) FROM search_analytics
                WHERE created_at > ?""",
                (since,),
            ).fetchone()[0]
    except Exception:
        return 0


def _net_feedback() -> int:
    try:
        with connection() as db:
            return db.execute(
                "SELECT COALESCE(SUM(rating), 0) FROM user_feedback"
            ).fetchone()[0]
    except Exception:
        return 0


def _total_store_views() -> int:
    try:
        with connection() as db:
            return db.execute("SELECT COUNT(*) FROM store_page_views").fetchone()[0]
    except Exception:
        return 0


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request) -> HTMLResponse:
    if _admin_session(request):
        return RedirectResponse("/admin")
    return templates.TemplateResponse(request=request, name="admin_login.html", context={})


@app.post("/admin/login")
def admin_login(
    request: Request,
    password: str = Form(...),
    next_url: Annotated[str, Form()] = "/admin",
) -> RedirectResponse:
    if not settings.admin_enabled or not settings.admin_password:
        raise HTTPException(403, "پنل مدیریت فعال نیست.")
    if password != settings.admin_password:
        raise HTTPException(401, "رمزعبور اشتباه است.")
    safe_next_url = next_url if next_url.startswith("/admin") and not next_url.startswith("//") else "/admin"
    response = RedirectResponse(safe_next_url, status_code=303)
    response.set_cookie("admin_session", settings.secret, httponly=True, secure=settings.app_env == "production", samesite="lax")
    return response


def _admin_context(request: Request | None, active_tab: str) -> dict[str, Any]:
    context = admin_dashboard_context(request)
    context["active_tab"] = active_tab
    return context


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/overview.html",
        context=_admin_context(request, "overview"),
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context=_admin_context(request, "users"),
    )


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(request: Request, user_id: int) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    account = rows(
        """SELECT * FROM accounts WHERE user_id=?""",
        (user_id,),
    )
    if not account:
        raise HTTPException(404, "کاربر پیدا نشد.")
    products = rows(
        """SELECT * FROM merchant_products WHERE user_id=?
        ORDER BY synced_at DESC""",
        (user_id,),
    )
    for product in products:
        product["source_counts"] = json.loads(product.get("source_counts") or "{}")
    return templates.TemplateResponse(
        request=request,
        name="admin/user_detail.html",
        context={
            **_admin_context(request, "users"),
            "account": account[0],
            "products": products,
            "now_iso": now_iso(),
        },
    )


@app.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/notifications.html",
        context=_admin_context(request, "notifications"),
    )


@app.get("/admin/connectivity", response_class=HTMLResponse)
def admin_connectivity_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/connectivity.html",
        context={
            **_admin_context(request, "connectivity"),
            "status": {
                "basalam_key_configured": bool(settings.client_id),
                "basalam_secret_configured": bool(settings.client_secret),
                "avalai_key_configured": bool(settings.avalai_api_key),
                "avalai_base_url": settings.avalai_base_url,
                "avalai_model": settings.avalai_model,
                "llm_similarity_enabled": settings.llm_similarity_enabled,
                "marketplace_trust_env": settings.marketplace_trust_env,
                "demo_mode": settings.demo_mode,
                "app_env": settings.app_env,
            },
        },
    )


@app.get("/admin/api/status")
def admin_api_status(request: Request) -> dict[str, Any]:
    if not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    return {
        "basalam_key_configured": bool(settings.client_id),
        "basalam_secret_configured": bool(settings.client_secret),
        "avalai_key_configured": bool(settings.avalai_api_key),
        "avalai_base_url": settings.avalai_base_url,
        "avalai_model": settings.avalai_model,
        "llm_similarity_enabled": settings.llm_similarity_enabled,
        "marketplace_trust_env": settings.marketplace_trust_env,
        "demo_mode": settings.demo_mode,
        "app_env": settings.app_env,
    }


@app.post("/admin/test/basalam")
async def test_basalam_connection(request: Request) -> dict[str, Any]:
    if not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://openapi.basalam.com/v1/products/search",
                json={"q": "آیفون", "rows": 1, "start": 0},
            )
            if response.status_code == 200:
                data = response.json()
                count = 0
                if isinstance(data, list):
                    count = len(data)
                elif isinstance(data, dict):
                    count = len(data.get("data") or data.get("results") or [])
                return {"ok": True, "status": response.status_code, "result_count": count, "message": "اتصال موفق"}
            return {"ok": False, "status": response.status_code, "message": f"خطا: {response.status_code} — {response.text[:200]}"}
    except Exception as exc:
        logger.warning("Basalam connectivity test failed: %s", exc)
        return {"ok": False, "status": 0, "message": str(exc)[:200]}


@app.post("/admin/test/marketplace")
async def test_marketplace_connection(request: Request) -> dict[str, Any]:
    if not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    try:
        crawl = await market_crawler.search("آیفون")
        results = {}
        for status in crawl["sources"]:
            results[status.source] = {"ok": status.ok, "count": status.count, "message": status.message}
        return {"ok": True, "results": results, "raw_count": crawl["raw_count"]}
    except Exception as exc:
        logger.exception("Marketplace connectivity test failed")
        return {"ok": False, "message": str(exc)[:200]}


@app.post("/admin/test/llm")
async def test_llm_connection(
    request: Request,
    prompt: str | None = Form(default=None),
) -> dict[str, Any]:
    if not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    if not settings.avalai_api_key:
        return {"ok": False, "message": "کلید API AvalAI تنظیم نشده است.", "llm_configured": False}
    from app.llm import _call_llm

    test_prompt = prompt or "سلام، امروز چه خبر؟"
    content = await _call_llm(test_prompt, max_tokens=200)
    if content:
        return {
            "ok": True,
            "model": settings.avalai_model,
            "base_url": settings.avalai_base_url,
            "prompt": test_prompt,
            "response": content[:200],
            "message": "اتصال موفق (Responses API)",
        }
    return {
        "ok": False,
        "model": settings.avalai_model,
        "base_url": settings.avalai_base_url,
        "prompt": test_prompt,
        "message": "اتصال ناموفق — لاگ سرور را بررسی کنید (کلید API یا نام مدل ممکن است اشتباه باشد)",
    }


@app.get("/admin/llm", response_class=HTMLResponse)
def admin_llm_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/llm.html",
        context={
            **_admin_context(request, "llm"),
        },
    )


@app.post("/admin/llm/save")
def admin_save_llm_settings(
    request: Request,
    avalai_api_key: str | None = Form(default=None),
    avalai_base_url: str | None = Form(default=None),
    avalai_model: str | None = Form(default=None),
    llm_similarity_enabled: str | None = Form(default=None),
) -> RedirectResponse:
    if not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    if not avalai_model:
        raise HTTPException(422, "نام مدل الزامی است.")
    existing = {}
    for env_path in _env_file_paths():
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                existing[key.strip()] = value.strip()
            break
    overrides = {
        "AVALAI_API_KEY": avalai_api_key if avalai_api_key and avalai_api_key.strip() else existing.get("AVALAI_API_KEY", str(settings.avalai_api_key)),
        "AVALAI_BASE_URL": avalai_base_url or str(settings.avalai_base_url),
        "AVALAI_MODEL": avalai_model,
        "LLM_SIMILARITY_ENABLED": llm_similarity_enabled or str(settings.llm_similarity_enabled).lower(),
    }
    try:
        save_admin_overrides(overrides)
    except Exception:
        logger.exception("LLM settings save failed")
        return RedirectResponse("/admin/llm?saved=0", status_code=303)
    return RedirectResponse("/admin/llm?saved=1", status_code=303)


@app.get("/admin/usdt", response_class=HTMLResponse)
def admin_usdt_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/usdt.html",
        context=_admin_context(request, "usdt"),
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        request=request,
        name="admin/settings.html",
        context={
            **_admin_context(request, "settings"),
            "env_file_paths": [str(p) for p in _env_file_paths()],
            "saved": bool(request.query_params.get("saved")),
            "error": bool(request.query_params.get("error")),
            "refreshed": bool(request.query_params.get("refreshed")),
            "refresh_error": bool(request.query_params.get("refresh_error")),
        },
    )


@app.post("/admin/settings")
def admin_update_settings(
    request: Request,
    merchant_sync_hours: int | None = Form(default=None),
    usdt_notification_enabled: str | None = Form(default=None),
    usdt_notification_percent: float | None = Form(default=None),
    usdt_check_interval_minutes: int | None = Form(default=None),
    app_env: str | None = Form(default=None),
    app_log_level: str | None = Form(default=None),
    app_base_url: str | None = Form(default=None),
    demo_mode: str | None = Form(default=None),
    avalai_api_key: str | None = Form(default=None),
    avalai_base_url: str | None = Form(default=None),
    avalai_model: str | None = Form(default=None),
    llm_similarity_enabled: str | None = Form(default=None),
) -> RedirectResponse:
    if request is not None and not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    overrides = {
        "MERCHANT_SYNC_HOURS": merchant_sync_hours if isinstance(merchant_sync_hours, int) else settings.merchant_sync_hours,
        "USDT_NOTIFICATION_ENABLED": usdt_notification_enabled if isinstance(usdt_notification_enabled, str) else str(settings.usdt_notification_enabled).lower(),
        "USDT_NOTIFICATION_PERCENT": usdt_notification_percent if isinstance(usdt_notification_percent, (int, float)) else settings.usdt_notification_percent,
        "USDT_CHECK_INTERVAL_MINUTES": usdt_check_interval_minutes if isinstance(usdt_check_interval_minutes, int) else settings.usdt_check_interval_minutes,
        "APP_ENV": app_env if isinstance(app_env, str) else str(settings.app_env),
        "APP_LOG_LEVEL": app_log_level if isinstance(app_log_level, str) else str(settings.log_level),
        "APP_BASE_URL": app_base_url if isinstance(app_base_url, str) else str(settings.base_url),
        "DEMO_MODE": demo_mode if isinstance(demo_mode, str) else str(settings.demo_mode).lower(),
        "AVALAI_API_KEY": str(avalai_api_key) if isinstance(avalai_api_key, str) and avalai_api_key else str(settings.avalai_api_key),
        "AVALAI_BASE_URL": avalai_base_url if isinstance(avalai_base_url, str) and avalai_base_url else str(settings.avalai_base_url),
        "AVALAI_MODEL": avalai_model if isinstance(avalai_model, str) and avalai_model else str(settings.avalai_model),
        "LLM_SIMILARITY_ENABLED": llm_similarity_enabled if isinstance(llm_similarity_enabled, str) and llm_similarity_enabled else str(settings.llm_similarity_enabled).lower(),
    }
    try:
        save_admin_overrides(overrides)
    except Exception as exc:
        logger.exception("Settings save failed")
        response = RedirectResponse("/admin/settings?error=1", status_code=303)
        return response
    response = RedirectResponse("/admin/settings?saved=1", status_code=303)
    return response


@app.post("/admin/settings/refresh")
def admin_refresh_settings(request: Request) -> RedirectResponse:
    if request is not None and not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    try:
        refresh_settings()
    except Exception:
        logger.exception("Settings refresh failed")
        return RedirectResponse("/admin/settings?refresh_error=1", status_code=303)
    return RedirectResponse("/admin/settings?refreshed=1", status_code=303)


@app.post("/admin/notifications/clear")
def admin_clear_notifications(request: Request) -> RedirectResponse:
    if request is not None and not _admin_session(request):
        raise HTTPException(401, "دسترسی مجاز نیست.")
    clear_all_merchant_notifications()
    response = RedirectResponse("/admin/notifications", status_code=303)
    return response


class PolicyInput(BaseModel):
    enabled: bool
    floor_price: int = Field(gt=0)
    objective: str = Field(pattern="^(fast|balanced|margin)$")
    interval_days: int = Field(ge=1, le=30)
    max_drop_percent: float = Field(gt=0, le=20)


class MarketSearchInput(BaseModel):
    product_name: str = Field(min_length=2, max_length=5000)
    exclude_basalam_product_id: int | None = Field(default=None, gt=0)
    basalam_product_url: str | None = Field(default=None, max_length=500)


class ComparableListingInput(BaseModel):
    source: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=1000)
    price: int = Field(gt=0)
    url: str = Field(min_length=1, max_length=2000)
    image_url: str = Field(default="", max_length=2000)
    similarity: float = Field(default=0, ge=0, le=1)
    llm_similarity: float | None = Field(default=None, ge=0, le=1)
    native_price: float | None = Field(default=None, gt=0)
    native_currency: str = Field(default="", max_length=10)


class ComparableRecalculationInput(BaseModel):
    listings: list[ComparableListingInput] = Field(min_length=3, max_length=72)


class RangeOverrideInput(BaseModel):
    min_price: int | None = Field(default=None, gt=0)
    max_price: int | None = Field(default=None, gt=0)


class ElasticityObservationInput(BaseModel):
    period: str = Field(min_length=4, max_length=40)
    price: int = Field(gt=0)
    units: int = Field(ge=0, le=10_000_000)


class ElasticityObservationsInput(BaseModel):
    observations: list[ElasticityObservationInput] = Field(min_length=1, max_length=365)


def create_merchant_notification(
    user_id: int,
    *,
    kind: str,
    title: str,
    body: str,
    target_url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with connection() as db:
        db.execute(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,metadata,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                user_id,
                kind,
                title,
                body,
                target_url,
                json.dumps(metadata or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def _notification_payload(notification: dict[str, Any]) -> dict[str, Any]:
    notification["metadata"] = json.loads(notification.get("metadata") or "{}")
    notification["read"] = bool(notification.get("read_at"))
    return notification


def clear_all_merchant_notifications() -> int:
    with connection() as db:
        result = db.execute("DELETE FROM merchant_notifications")
    return int(result.rowcount or 0)


class SearchRateLimiter:
    def __init__(self, limit: int = 20, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.requests: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def allow(self, client_id: str) -> bool:
        now = time.monotonic()
        async with self.lock:
            bucket = self.requests[client_id]
            while bucket and bucket[0] <= now - self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


search_rate_limiter = SearchRateLimiter()


SAMPLE_PRODUCT_URLS = [
    "https://basalam.com/essencegallery/product/25289339",
    "https://basalam.com/rudika-com/product/33702253",
    "https://basalam.com/nakhl_zivar/product/1967705",
    "https://basalam.com/ketab_dabiran/product/19832064",
    "https://basalam.com/aryapetrol/product/36657854",
]


@app.get("/api/sample-products")
async def sample_products() -> dict[str, Any]:
    async def fetch_one(url: str) -> dict[str, Any]:
        product_id = _basalam_product_id_from_url(url)
        if product_id:
            try:
                data = await fetch_basalam_product(product_id)
                if data:
                    return {
                        "url": url,
                        "title": data.get("title", ""),
                        "image_url": data.get("image_url", ""),
                        "price": data.get("price", 0),
                    }
            except Exception as exc:
                logger.warning("Sample product fetch failed for %s: %s", url, exc)
        return {"url": url, "title": "", "image_url": "", "price": 0}

    results = await asyncio.gather(*(fetch_one(url) for url in SAMPLE_PRODUCT_URLS))
    return {"products": results}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"demo_mode": settings.demo_mode, "merchant_connected": bool(user_id)},
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if read_session(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/merchant")
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "oauth_ready": bool(settings.client_id and settings.client_secret),
            "digikala_error": request.query_params.get("digikala_error"),
        },
    )


@app.get("/merchant", response_class=HTMLResponse)
def merchant_page(request: Request):
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if not user_id:
        return RedirectResponse("/login")
    account = rows(
        "SELECT vendor_title,user_name,marketplace FROM accounts WHERE user_id=?",
        (user_id,),
    )
    if not account:
        response = RedirectResponse("/login")
        response.delete_cookie(COOKIE_NAME)
        return response
    return templates.TemplateResponse(
        request=request,
        name="merchant.html",
        context={"account": account[0]},
    )


@app.get("/merchant/products/{product_id}/elasticity", response_class=HTMLResponse)
def merchant_elasticity_page(product_id: int, request: Request):
    user_id = _merchant_user(request)
    product = rows(
        "SELECT title FROM merchant_products WHERE user_id=? AND product_id=?",
        (user_id, product_id),
    )
    if not product:
        raise HTTPException(404, "محصول پیدا نشد.")
    return templates.TemplateResponse(
        request=request,
        name="merchant_elasticity.html",
        context={"product": product[0], "product_id": product_id},
    )


@app.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "demo" if settings.demo_mode else "live"}


@app.get("/store/{store_id}")
def store_page(request: Request, store_id: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="store.html",
        context={"store_id": store_id, "settings": settings},
    )


class StoreAnalysisInput(BaseModel):
    store_id: str = Field(min_length=1, max_length=200)
    product_limit: int = Field(default=50, ge=1, le=100)
    use_llm: bool = Field(default=False)


@app.post("/api/store/analyze")
async def store_analysis(
    payload: StoreAnalysisInput, request: Request
) -> dict[str, Any]:
    client_id = request.client.host if request.client else "unknown"
    store_id = _basalam_store_id_from_url(payload.store_id) or payload.store_id
    try:
        store_info = await fetch_basalam_store(store_id)
    except Exception as exc:
        logger.warning("Store fetch failed for %s: %s", store_id, exc)
        store_info = {"vendor_id": None, "title": store_id, "store_identifier": store_id}

    with connection() as db:
        db.execute(
            """INSERT INTO store_page_views (store_id, client_id, created_at)
            VALUES (?, ?, ?)""",
            (store_id, client_id, now_iso()),
        )

    products = await fetch_store_product_list(store_id, max_products=payload.product_limit)
    if not products:
        return {"store": store_info, "results": [], "error": "هیچ محصولی یافت نشد."}

    semaphore = asyncio.Semaphore(3)

    async def analyze_product(item: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                product_id = item.get("product_id")
                source_product = None
                search_query = item["title"]
                if product_id:
                    source_product = await fetch_basalam_product(product_id)
                    if source_product and source_product.get("title"):
                        search_query = source_product["title"]
                        if settings.llm_similarity_enabled and settings.avalai_api_key:
                            optimized = await optimize_search_query(source_product)
                            if optimized:
                                search_query = optimized
                crawl = await market_crawler.search(search_query)
                listings = crawl["listings"]
                llm_scores: dict[str, float] = {}
                if payload.use_llm and settings.llm_similarity_enabled:
                    llm_scores = await score_product_similarity(search_query, listings, source_product)
                listings = [
                    item for item in listings
                    if item.source in INTERNAL_MARKETPLACE_SOURCES
                ]
                llm_scores = {
                    item.url: llm_scores[item.url]
                    for item in listings
                    if item.url in llm_scores
                }
                analysis = analyze_listings(listings, llm_scores)
                return {
                    "product_id": item["product_id"] if product_id else 0,
                    "title": source_product.get("title", "") if source_product else item["title"],
                    "url": item["url"],
                    "basalam_url": f"https://basalam.com/p/{item['product_id']}" if item.get("product_id") else "",
                    "image_url": source_product.get("image_url", "") if source_product else "",
                    "current_price": source_product.get("price", 0) if source_product else 0,
                    "stock": 0,
                    "analysis": {
                        "recommended": analysis["recommended"],
                        "range": analysis["range"],
                        "confidence": analysis["confidence"],
                        "sample_size": analysis["sample_size"],
                        "source_counts": analysis["source_counts"],
                        "listings": analysis["listings"],
                        "llm_similarity_enabled": analysis.get("llm_similarity_enabled", False),
                        "method": analysis["method"],
                    },
                }
            except Exception as exc:
                logger.info("Store product analysis failed for %s: %s", item.get("product_id"), exc)
                return None

    results = await asyncio.gather(
        *(analyze_product(item) for item in products)
    )
    valid_results = [r for r in results if r is not None]
    return {"store": store_info, "results": valid_results, "total": len(products)}


class FeedbackInput(BaseModel):
    feedback_type: str = Field(pattern="^(similarity|recommendation)$")
    target_url: str = Field(min_length=1, max_length=2000)
    rating: int = Field(ge=-1, le=1)
    product_name: str | None = None
    store_id: str | None = None


@app.post("/api/feedback")
def submit_feedback(
    payload: FeedbackInput, request: Request
) -> dict[str, bool]:
    client_id = request.client.host if request.client else "unknown"
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    with connection() as db:
        db.execute(
            """INSERT INTO user_feedback
            (client_id, user_id, feedback_type, target_url, rating, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                user_id,
                payload.feedback_type,
                payload.target_url,
                payload.rating,
                json.dumps(
                    {
                        "product_name": payload.product_name,
                        "store_id": payload.store_id,
                    },
                    ensure_ascii=False,
                ),
                now_iso(),
            ),
        )
    return {"ok": True}


def _display_listing_groups(
    listings: list[MarketListing],
    llm_scores: dict[str, float] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    scores = llm_scores or {}
    ordered = sorted(
        listings,
        key=lambda item: (-scores.get(item.url, item.similarity), item.price),
    )
    groups: dict[str, list[dict[str, Any]]] = {"internal": [], "foreign": []}
    for item in ordered:
        public = item.public_dict()
        if item.url in scores:
            public["llm_similarity"] = round(scores[item.url], 2)
        group = "foreign" if item.source in FOREIGN_MARKETPLACE_SOURCES else "internal"
        groups[group].append(public)
    return groups


def _market_analysis_variants(
    listings: list[MarketListing],
    llm_scores: dict[str, float] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    internal = [
        item for item in listings if item.source in INTERNAL_MARKETPLACE_SOURCES
    ]
    internal_scores = {
        url: score
        for url, score in (llm_scores or {}).items()
        if any(item.url == url for item in internal)
    }
    internal_analysis = analyze_listings(internal, internal_scores)
    combined_analysis = analyze_listings(listings, llm_scores)
    return (
        internal_analysis,
        {"internal": internal_analysis, "with_foreign": combined_analysis},
        _display_listing_groups(listings, llm_scores),
    )


@app.post("/api/market/recalculate")
def recalculate_comparables(payload: ComparableRecalculationInput) -> dict[str, Any]:
    """Rebuild the price analysis after a user removes an irrelevant comparable."""
    listings = [
        MarketListing(
            source=item.source,
            title=item.title,
            price=item.price,
            url=item.url,
            image_url=item.image_url,
            similarity=item.similarity,
            native_price=item.native_price,
            native_currency=item.native_currency,
        )
        for item in payload.listings
    ]
    llm_scores = {
        item.url: item.llm_similarity
        for item in payload.listings
        if item.llm_similarity is not None
    }
    try:
        analysis, variants, groups = _market_analysis_variants(listings, llm_scores)
        return {
            "analysis": analysis,
            "analysis_variants": variants,
            "listing_groups": groups,
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ButtonClickInput(BaseModel):
    button_name: str = Field(min_length=1, max_length=100)
    product_id: int | None = None
    store_id: str | None = None
    product_url: str | None = None


@app.post("/api/metrics/button-click")
def record_button_click(
    payload: ButtonClickInput, request: Request
) -> dict[str, bool]:
    client_id = request.client.host if request.client else "unknown"
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    with connection() as db:
        db.execute(
            """INSERT INTO button_click_metrics
            (button_name, product_id, store_id, product_url, client_id, user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.button_name,
                str(payload.product_id) if payload.product_id else None,
                payload.store_id,
                payload.product_url,
                client_id,
                user_id,
                now_iso(),
            ),
        )
    return {"ok": True}


@app.get("/admin/metrics", response_class=HTMLResponse)
def admin_metrics_page(
    request: Request,
    days: str = Query("30", pattern="^(7|30|90|all)$"),
) -> HTMLResponse:
    if not _admin_session(request):
        return RedirectResponse("/admin/login")
    selected_days = None if days == "all" else int(days)
    since = (
        (datetime.now(timezone.utc) - timedelta(days=selected_days)).isoformat()
        if selected_days
        else None
    )
    where = " WHERE created_at >= ?" if since else ""
    params = (since,) if since else ()
    with connection() as db:
        total_feedback = db.execute(
            f"SELECT COUNT(*) FROM user_feedback{where}", params
        ).fetchone()[0]
        recommendation_likes = db.execute(
            "SELECT COUNT(*) FROM user_feedback WHERE feedback_type='recommendation' AND rating=1"
            + (" AND created_at >= ?" if since else ""), params
        ).fetchone()[0]
        recommendation_dislikes = db.execute(
            "SELECT COUNT(*) FROM user_feedback WHERE feedback_type='recommendation' AND rating=-1"
            + (" AND created_at >= ?" if since else ""), params
        ).fetchone()[0]
        similarity_likes = db.execute(
            "SELECT COUNT(*) FROM user_feedback WHERE feedback_type='similarity' AND rating=1"
            + (" AND created_at >= ?" if since else ""), params
        ).fetchone()[0]
        similarity_dislikes = db.execute(
            "SELECT COUNT(*) FROM user_feedback WHERE feedback_type='similarity' AND rating=-1"
            + (" AND created_at >= ?" if since else ""), params
        ).fetchone()[0]
        total_clicks = db.execute(
            f"SELECT COUNT(*) FROM button_click_metrics{where}", params
        ).fetchone()[0]
        total_store_views = db.execute(
            f"SELECT COUNT(*) FROM store_page_views{where}", params
        ).fetchone()[0]
        total_searches = db.execute(
            f"SELECT COUNT(*) FROM search_analytics{where}", params
        ).fetchone()[0]
        button_counts = {
            row["button_name"]: row["cnt"]
            for row in db.execute(
                f"SELECT button_name, COUNT(*) AS cnt FROM button_click_metrics{where} GROUP BY button_name",
                params,
            ).fetchall()
        }
        distinct_visitors = db.execute(
            """SELECT COUNT(DISTINCT client_id) FROM (
                SELECT client_id, created_at FROM search_analytics
                UNION ALL SELECT client_id, created_at FROM store_page_views
                UNION ALL SELECT client_id, created_at FROM button_click_metrics
            ) WHERE client_id IS NOT NULL""" + (" AND created_at >= ?" if since else ""),
            params,
        ).fetchone()[0]
        recent_searches = [
            dict(row)
            for row in db.execute(
                """SELECT query, resolved_from_url, result_count, used_llm, created_at, client_id
                FROM search_analytics""" + where + " ORDER BY created_at DESC LIMIT 30",
                params,
            ).fetchall()
        ]
        recent_feedback = [
            dict(row)
            for row in db.execute(
                """SELECT feedback_type, rating, target_url, user_id, client_id, created_at
                FROM user_feedback""" + where + " ORDER BY created_at DESC LIMIT 50",
                params,
            ).fetchall()
        ]
        recent_clicks = [
            dict(row)
            for row in db.execute(
                """SELECT button_name, product_id, store_id, product_url, user_id, client_id, created_at
                FROM button_click_metrics""" + where + " ORDER BY created_at DESC LIMIT 50",
                params,
            ).fetchall()
        ]
        activity: dict[str, dict[str, int]] = defaultdict(
            lambda: {"searches": 0, "views": 0, "clicks": 0}
        )
        for table, key in (
            ("search_analytics", "searches"),
            ("store_page_views", "views"),
            ("button_click_metrics", "clicks"),
        ):
            for row in db.execute(
                f"SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count FROM {table}{where} GROUP BY day ORDER BY day DESC LIMIT 14",
                params,
            ).fetchall():
                activity[row["day"]][key] = row["count"]
        activity_days = []
        for day in sorted(activity)[-14:]:
            item = {"day": day, **activity[day]}
            item["total"] = item["searches"] + item["views"] + item["clicks"]
            activity_days.append(item)
        max_activity = max((item["total"] for item in activity_days), default=1)
    positive_feedback = recommendation_likes + similarity_likes
    positive_rate = round(positive_feedback * 100 / total_feedback) if total_feedback else 0
    intent_clicks = sum(
        button_counts.get(name, 0)
        for name in ("use_recommended_price", "update_price_basalam", "set_price_basalam_merchant")
    )
    action_rate = round(intent_clicks * 100 / total_searches) if total_searches else 0
    return templates.TemplateResponse(
        request=request,
        name="admin/metrics.html",
        context={
            **_admin_context(request, "metrics"),
            "total_feedback": total_feedback,
            "recommendation_likes": recommendation_likes,
            "recommendation_dislikes": recommendation_dislikes,
            "similarity_likes": similarity_likes,
            "similarity_dislikes": similarity_dislikes,
            "total_clicks": total_clicks,
            "total_store_views": total_store_views,
            "total_searches": total_searches,
            "button_counts": button_counts,
            "recent_searches": recent_searches,
            "recent_feedback": recent_feedback,
            "recent_clicks": recent_clicks,
            "selected_period": days,
            "distinct_visitors": distinct_visitors,
            "positive_rate": positive_rate,
            "action_rate": action_rate,
            "activity_days": activity_days,
            "max_activity": max_activity,
        },
    )


@app.post("/api/market/analyze")
async def market_analysis(payload: MarketSearchInput, request: Request) -> dict[str, Any]:
    client_id = request.client.host if request.client else "unknown"
    if not await search_rate_limiter.allow(client_id):
        raise HTTPException(
            429,
            "تعداد جست‌وجوها بیش از حد مجاز است؛ یک دقیقه دیگر دوباره تلاش کنید.",
            headers={"Retry-After": "60"},
        )
    try:
        query, resolved_from_url = await resolve_product_query(payload.product_name)
    except ProductLinkError as exc:
        raise HTTPException(422, str(exc)) from exc
    excluded_product_id = (
        payload.exclude_basalam_product_id
        or basalam_product_id_from_url(payload.product_name)
    )
    merchant_product = None
    merchant_user_id = read_session(request.cookies.get(COOKIE_NAME))
    merchant_marketplace = "basalam"
    if merchant_user_id and payload.exclude_basalam_product_id:
        merchant_rows = rows(
            """SELECT mp.product_id,mp.title,mp.current_price,a.marketplace
            FROM merchant_products mp JOIN accounts a ON a.user_id=mp.user_id
            WHERE mp.user_id=? AND mp.product_id=?""",
            (merchant_user_id, payload.exclude_basalam_product_id),
        )
        if merchant_rows:
            merchant_product = merchant_rows[0]
            merchant_marketplace = merchant_product.get("marketplace") or "basalam"
    source_product: dict[str, Any] | None = None
    search_query = query
    source_queries: dict[str, str] | None = None
    if payload.basalam_product_url and settings.llm_similarity_enabled:
        product_id = _basalam_product_id_from_url(payload.basalam_product_url)
        if product_id:
            source_product = await fetch_basalam_product(product_id)
            if source_product and source_product.get("title"):
                search_query = source_product["title"]
                optimized = await optimize_search_query(source_product)
                if optimized:
                    search_query = optimized
    if (
        resolved_from_url
        and source_product is None
        and settings.llm_similarity_enabled
        and settings.avalai_api_key
    ):
        localized = await optimize_marketplace_queries(query)
        if localized:
            source_queries = {
                "torob": localized["iran"],
                "digikala": localized["iran"],
                "basalam": localized["iran"],
                "trendyol": localized["trendyol"],
                "noon_uae": localized["noon"],
            }
    try:
        if source_queries:
            crawl = await market_crawler.search(
                search_query,
                source_queries=source_queries,
            )
        else:
            crawl = await market_crawler.search(search_query)
    except Exception as exc:
        logger.exception("Marketplace crawler initialization failed")
        raise HTTPException(
            502,
            "اتصال خروجی سرور به بازارها برقرار نشد؛ تنظیمات شبکه یا پراکسی را بررسی کنید.",
        ) from exc
    statuses = [asdict(status) for status in crawl["sources"]]
    if not any(status["ok"] for status in statuses):
        raise HTTPException(502, "هیچ‌کدام از بازارها در دسترس نبودند؛ کمی بعد دوباره تلاش کنید.")
    try:
        listings = crawl["listings"]
        if excluded_product_id:
            listings = exclude_marketplace_product(
                listings,
                merchant_marketplace,
                excluded_product_id,
            )
        llm_scores: dict[str, float] = {}
        if settings.llm_similarity_enabled:
            comparison_query = query if source_queries else search_query
            llm_scores = await score_product_similarity(
                comparison_query,
                listings,
                source_product,
            )
        analysis, analysis_variants, listing_groups = _market_analysis_variants(
            listings,
            llm_scores,
        )
    except ValueError as exc:
        raise HTTPException(
            422,
            {
                "message": str(exc),
                "sources": statuses,
                "raw_count": crawl["raw_count"],
            },
        ) from exc
    with connection() as db:
        db.execute(
            """INSERT INTO search_analytics
            (client_id, user_id, query, resolved_from_url, source_product_id,
            result_count, used_llm, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                merchant_user_id,
                query,
                resolved_from_url,
                str(source_product.get("product_id")) if source_product else None,
                analysis["sample_size"],
                settings.llm_similarity_enabled,
                now_iso(),
            ),
        )
    return {
        "query": query,
        "resolved_from_url": resolved_from_url,
        "merchant_product": merchant_product,
        "source_product": source_product,
        "analysis": analysis,
        "analysis_variants": analysis_variants,
        "listing_groups": listing_groups,
        "sources": statuses,
        "raw_count": crawl["raw_count"],
        "search_queries": crawl.get("search_queries", {}),
        "disclaimer": "این بازه از قیمت‌های فعلی فروش ساخته شده، نه تراکنش‌های قطعی‌شده.",
    }


@app.post("/api/market/analyze-extended")
async def market_analysis_extended(payload: MarketSearchInput, request: Request) -> dict[str, Any]:
    """Extended analysis: fetch product from Basalam link, optimize query via LLM,
    web-search 36 product links across all Iranian marketplaces, score similarity,
    and return the top 18 results with full analysis.
    """
    if not settings.llm_similarity_enabled or not settings.avalai_api_key:
        raise HTTPException(400, "امکانات LLM/وب‌جستجو فعال نیست.")

    client_id = request.client.host if request.client else "unknown"
    if not await search_rate_limiter.allow(client_id):
        raise HTTPException(
            429,
            "تعداد جست‌وجوها بیش از حد مجاز است؛ یک دقیقه دیگر دوباره تلاش کنید.",
            headers={"Retry-After": "60"},
        )

    try:
        query, resolved_from_url = await resolve_product_query(payload.product_name)
    except ProductLinkError as exc:
        raise HTTPException(422, str(exc)) from exc

    excluded_product_id = (
        payload.exclude_basalam_product_id
        or basalam_product_id_from_url(payload.product_name)
    )

    source_product: dict[str, Any] | None = None
    search_query = query

    if payload.basalam_product_url and settings.llm_similarity_enabled:
        product_id = _basalam_product_id_from_url(payload.basalam_product_url)
        if product_id:
            source_product = await fetch_basalam_product(product_id)
            if source_product and source_product.get("title"):
                search_query = source_product["title"]
                optimized = await optimize_search_query(source_product)
                if optimized:
                    search_query = optimized

    web_results = await web_search_products(search_query, count=36)

    if not web_results:
        logger.warning("Web search returned no results for query: %s", search_query)

    listings = [
        MarketListing(
            source="web_search",
            title=r.title,
            price=0,
            url=r.url,
            image_url="",
            similarity=0,
            external_id="",
        )
        for r in web_results[:36]
    ]

    llm_scores = await score_product_similarity(search_query, listings, source_product)

    for listing in listings:
        if listing.url in llm_scores:
            listing.__dict__["similarity"] = llm_scores[listing.url]  # type: ignore

    if excluded_product_id:
        listings = exclude_marketplace_product(listings, "basalam", excluded_product_id)

    analysis = analyze_listings(listings, llm_scores)

    with connection() as db:
        db.execute(
            """INSERT INTO search_analytics
            (client_id, user_id, query, resolved_from_url, source_product_id,
            result_count, used_llm, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                read_session(request.cookies.get(COOKIE_NAME)),
                query,
                resolved_from_url,
                str(source_product.get("product_id")) if source_product else None,
                analysis["sample_size"],
                settings.llm_similarity_enabled,
                now_iso(),
            ),
        )

    top_18 = analysis["listings"][:18]
    analysis["listings"] = top_18
    analysis["total_listings"] = len(listings)

    return {
        "query": query,
        "resolved_from_url": resolved_from_url,
        "source_product": source_product,
        "analysis": analysis,
        "sources": [],
        "raw_count": len(listings),
        "disclaimer": "این نتایج از وب‌جستجو و مرورگرهای بازار آنلاین ایران جمع‌آوری شده‌اند؛ برای تصمیم‌گیری نهایی، قیمت و وضعیت کالا را بررسی کنید.",
    }


@app.get("/auth/basalam")
def connect_basalam(renew: str | None = None) -> RedirectResponse:
    if not settings.client_id or not settings.client_secret:
        raise HTTPException(503, "Basalam OAuth credentials are not configured.")
    state = secrets.token_urlsafe(32)
    trace_id = secrets.token_hex(12)
    started = time.perf_counter()
    try:
        authorization_url = basalam.authorization_url(state)
    except ValueError as exc:
        logger.warning(
            "oauth_authorization_failed trace_id=%s stage=authorization_url error=%s",
            trace_id,
            exc,
        )
        raise HTTPException(503, str(exc)) from exc
    logger.info(
        "oauth_authorization_started trace_id=%s redirect_uri=%s scopes=%s renewal=%s "
        "state_fingerprint=%s elapsed_ms=%s",
        trace_id,
        settings.redirect_uri,
        basalam.requested_scopes(),
        renew == "analytics",
        _fingerprint(state),
        round((time.perf_counter() - started) * 1000),
    )
    response = RedirectResponse(authorization_url)
    response.set_cookie(
        "oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
    )
    response.set_cookie(
        "oauth_trace",
        trace_id,
        max_age=600,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
    )
    return response


@app.post("/auth/digikala/source")
async def connect_digikala_source(
    background_tasks: BackgroundTasks,
    seller_link: Annotated[str | None, Form(max_length=500)] = None,
    digikala_token: Annotated[str | None, Form(max_length=5000)] = None,
) -> RedirectResponse:
    token = (digikala_token or "").strip()
    link = (seller_link or "").strip()
    seller_code = digikala.seller_code(link) if link else None
    try:
        if token:
            if len(token) < 20:
                raise ValueError("token is too short")
            profile = await digikala.profile(token)
            seller_id = int(profile["seller_id"])
            seller_title = (
                profile.get("seller_name")
                or profile.get("secondary_business_name")
                or f"فروشنده دیجی‌کالا {seller_id}"
            )
            user_name = " ".join(
                part
                for part in (profile.get("first_name"), profile.get("last_name"))
                if part
            ) or seller_title
            stored_source = token
            connection_mode = "token"
        elif seller_code:
            catalog = await digikala.public_catalog(seller_code)
            seller = catalog["seller"]
            seller_id = int(seller["id"])
            seller_title = seller.get("title") or f"فروشنده دیجی‌کالا {seller_code}"
            user_name = seller_title
            stored_source = f"public-seller:{seller_code}"
            connection_mode = "public_link"
        else:
            raise ValueError("missing or invalid Digikala source")
        if seller_id <= 0:
            raise ValueError("invalid seller id")
    except (DigikalaError, KeyError, TypeError, ValueError):
        logger.warning("digikala_source_connection_rejected")
        return RedirectResponse("/login?digikala_error=invalid_source", status_code=303)

    # Public links are imports, not authentication. Keep them in a separate ID
    # namespace so a public URL can never overwrite a private token connection.
    user_id = (
        -seller_id
        if connection_mode == "token"
        else -(1_000_000_000 + seller_id)
    )
    with connection() as db:
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,refresh_token,
             token_expires_at,connected_at,sync_status,marketplace)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              vendor_id=excluded.vendor_id,vendor_title=excluded.vendor_title,
              user_name=excluded.user_name,access_token=excluded.access_token,
              refresh_token=NULL,token_expires_at=NULL,
              connected_at=excluded.connected_at,sync_status='queued',
              sync_error=NULL,marketplace='digikala'""",
            (
                user_id,
                seller_id,
                seller_title,
                user_name,
                encrypt_token(stored_source),
                None,
                None,
                now_iso(),
                "queued",
                "digikala",
            ),
        )
    create_merchant_notification(
        user_id,
        kind="store_connected",
        title="فروشگاه دیجی‌کالا وصل شد",
        body="دقیقه فهرست محصولات، قیمت و موجودی فروشگاه را دریافت می‌کند.",
        target_url="/merchant",
        metadata={
            "marketplace": "digikala",
            "seller_id": seller_id,
            "connection_mode": connection_mode,
            **({"seller_code": seller_code} if seller_code else {}),
        },
    )
    background_tasks.add_task(merchant_sync.sync_user, user_id)
    response = RedirectResponse("/merchant", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        create_session(user_id),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
    )
    return response


@app.get("/auth/basalam/callback")
async def auth_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    trace_id = request.cookies.get("oauth_trace") or secrets.token_hex(12)
    callback_started = time.perf_counter()
    expected = request.cookies.get("oauth_state", "")
    if not expected or not state or not hmac.compare_digest(expected, state):
        logger.warning(
            "oauth_callback_rejected trace_id=%s stage=state_validation "
            "state_cookie_present=%s state_fingerprint=%s expected_fingerprint=%s",
            trace_id,
            bool(expected),
            _fingerprint(state),
            _fingerprint(expected) if expected else "-",
        )
        raise HTTPException(400, "Invalid OAuth state.")
    if error:
        logger.warning(
            "oauth_callback_rejected trace_id=%s stage=provider_authorization "
            "provider_error=%s provider_description=%s",
            trace_id,
            _safe_log_text(error),
            _safe_log_text(error_description),
        )
        raise HTTPException(
            400,
            f"Basalam authorization was rejected. Diagnostic ID: {trace_id}",
        )
    if not code:
        logger.warning(
            "oauth_callback_rejected trace_id=%s stage=callback_parameters "
            "reason=authorization_code_missing",
            trace_id,
        )
        raise HTTPException(
            400,
            f"Basalam authorization code is missing. Diagnostic ID: {trace_id}",
        )
    logger.info(
        "oauth_callback_started trace_id=%s stage=state_validation "
        "state_fingerprint=%s code_fingerprint=%s client_host_fingerprint=%s",
        trace_id,
        _fingerprint(state),
        _fingerprint(code),
        _fingerprint(request.client.host) if request.client else "-",
    )
    oauth_stage = "token_exchange"
    try:
        token_data = await basalam.exchange_code(code, trace_id=trace_id)
        access = token_data["access_token"]
        logger.info(
            "oauth_stage_succeeded trace_id=%s stage=token_exchange "
            "token_type=%s expires_in=%s refresh_token_present=%s",
            trace_id,
            token_data.get("token_type", "-"),
            token_data.get("expires_in", "-"),
            bool(token_data.get("refresh_token")),
        )
        oauth_stage = "user_profile"
        user = await basalam.me(access, trace_id=trace_id)
        vendor = user.get("vendor") or {}
        if not vendor.get("id"):
            logger.warning(
                "oauth_callback_rejected trace_id=%s stage=user_profile "
                "reason=vendor_missing user_fingerprint=%s",
                trace_id,
                _fingerprint(user.get("id")),
            )
            raise HTTPException(400, "This Basalam account has no vendor booth.")
        logger.info(
            "oauth_stage_succeeded trace_id=%s stage=user_profile "
            "user_fingerprint=%s vendor_fingerprint=%s",
            trace_id,
            _fingerprint(user.get("id")),
            _fingerprint(vendor.get("id")),
        )
        oauth_stage = "account_persistence"
        with connection() as db:
            db.execute(
                """INSERT INTO accounts
                (user_id,vendor_id,vendor_title,user_name,access_token,refresh_token,
                 token_expires_at,connected_at,sync_status,analytics_status,
                 analytics_error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  vendor_id=excluded.vendor_id, vendor_title=excluded.vendor_title,
                  user_name=excluded.user_name,
                  access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                  token_expires_at=excluded.token_expires_at,
                  connected_at=excluded.connected_at,sync_status='queued',
                  sync_error=NULL,analytics_status='pending',analytics_error=NULL""",
                (
                    user["id"],
                    vendor["id"],
                    vendor.get("title", "غرفه باسلام"),
                    user.get("name") or user.get("username") or "غرفه‌دار",
                    encrypt_token(access),
                    encrypt_token(token_data["refresh_token"]) if token_data.get("refresh_token") else None,
                    token_expiry_iso(token_data.get("expires_in")),
                    now_iso(),
                    "queued",
                    "pending",
                    None,
                ),
            )
        logger.info(
            "oauth_stage_succeeded trace_id=%s stage=account_persistence "
            "user_fingerprint=%s",
            trace_id,
            _fingerprint(user.get("id")),
        )
        create_merchant_notification(
            int(user["id"]),
            kind="booth_connected",
            title="غرفه شما وصل شد",
            body="دقیقه محصولات غرفه را دریافت می‌کند و پیشنهادهای قیمت را در همین داشبورد نشان می‌دهد.",
            target_url="/merchant",
            metadata={"vendor_id": vendor["id"]},
        )
    except (BasalamError, KeyError) as exc:
        logger.exception(
            "oauth_callback_failed trace_id=%s stage=%s elapsed_ms=%s "
            "exception_type=%s provider_status=%s error_kind=%s",
            trace_id,
            oauth_stage,
            round((time.perf_counter() - callback_started) * 1000),
            type(exc).__name__,
            getattr(exc, "status_code", None),
            getattr(exc, "error_kind", "missing_response_field"),
        )
        raise HTTPException(
            502,
            f"Basalam OAuth failed. Diagnostic ID: {trace_id}",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "oauth_callback_unexpected_error trace_id=%s stage=%s elapsed_ms=%s",
            trace_id,
            oauth_stage,
            round((time.perf_counter() - callback_started) * 1000),
        )
        raise HTTPException(
            500,
            f"OAuth processing failed. Diagnostic ID: {trace_id}",
        )
    background_tasks.add_task(merchant_sync.sync_user, int(user["id"]))
    logger.info(
        "oauth_callback_succeeded trace_id=%s stage=session_created "
        "elapsed_ms=%s user_fingerprint=%s sync_queued=true",
        trace_id,
        round((time.perf_counter() - callback_started) * 1000),
        _fingerprint(user.get("id")),
    )
    response = RedirectResponse("/merchant")
    response.delete_cookie("oauth_state")
    response.delete_cookie("oauth_trace")
    response.set_cookie(
        COOKIE_NAME,
        create_session(int(user["id"])),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
    )
    return response


def _fingerprint(value: Any) -> str:
    if value is None:
        return "-"
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def _safe_log_text(value: Any) -> str:
    if value is None:
        return "-"
    return " ".join(str(value).split())[:300]


def _merchant_user(request: Request) -> int:
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if not user_id:
        raise HTTPException(401, "برای مشاهده غرفه وارد شوید.")
    return user_id


@app.get("/api/merchant/dashboard")
def merchant_dashboard(request: Request) -> dict[str, Any]:
    user_id = _merchant_user(request)
    account_rows = rows(
        """SELECT user_id,vendor_id,vendor_title,user_name,last_synced_at,
        sync_status,sync_error,marketplace,analytics_synced_at,analytics_status,
        analytics_error FROM accounts WHERE user_id=?""",
        (user_id,),
    )
    if not account_rows:
        raise HTTPException(401, "اتصال غرفه پیدا نشد.")
    account = account_rows[0]
    history_since = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    products = rows(
        """SELECT mp.*,
        COALESCE(SUM(CASE WHEN se.sold_at >= ? THEN se.quantity ELSE 0 END), 0)
          AS tracked_sales_180d,
        COALESCE(SUM(CASE WHEN se.sold_at >= ?
          THEN se.quantity * se.unit_price ELSE 0 END), 0) AS tracked_revenue_180d,
        COALESCE(SUM(CASE WHEN se.sold_at >= ?
          AND mp.market_suggested > se.unit_price
          THEN se.quantity * (mp.market_suggested - se.unit_price) ELSE 0 END), 0)
          AS estimated_opportunity_180d
        FROM merchant_products mp
        LEFT JOIN merchant_sales_events se
          ON se.user_id=mp.user_id AND se.product_id=mp.product_id
        WHERE mp.user_id=?
        GROUP BY mp.user_id,mp.product_id
        ORDER BY mp.stock > 0 DESC,mp.estimate_error IS NULL DESC,mp.title""",
        (history_since, history_since, history_since, user_id),
    )
    subscription_rows = rows(
        """SELECT status,period_end FROM subscriptions WHERE customer_id=?""",
        (user_id,),
    )
    subscription = subscription_rows[0] if subscription_rows else None
    subscription_current = True
    if subscription and subscription.get("period_end"):
        try:
            subscription_current = (
                datetime.fromisoformat(subscription["period_end"])
                > datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            subscription_current = False
    premium_active = bool(
        subscription
        and subscription.get("status") in {"active", "trialing"}
        and subscription_current
    )
    sales_history: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    own_price_history: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    market_history: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for point in rows(
        """SELECT product_id,changed_at,price,discounted_price
        FROM merchant_product_price_points
        WHERE user_id=? AND changed_at>=?
        ORDER BY changed_at""",
        (user_id, history_since),
    ):
        own_price_history[int(point.pop("product_id"))].append(point)
    if premium_active:
        for point in rows(
            """SELECT product_id,substr(sold_at,1,10) AS day,
            SUM(quantity) AS units,SUM(quantity * unit_price) AS revenue
            FROM merchant_sales_events
            WHERE user_id=? AND sold_at>=?
            GROUP BY product_id,substr(sold_at,1,10)
            ORDER BY day""",
            (user_id, history_since),
        ):
            sales_history[int(point.pop("product_id"))].append(point)
        for point in rows(
            """SELECT product_id,captured_at,recommended_price,market_low,market_high
            FROM merchant_market_snapshots
            WHERE user_id=? AND captured_at>=?
            ORDER BY captured_at""",
            (user_id, history_since),
        ):
            market_history[int(point.pop("product_id"))].append(point)
    ready = 0
    tracked_sales = 0
    tracked_revenue = 0
    estimated_opportunity = 0
    for product in products:
        product["source_counts"] = json.loads(product.get("source_counts") or "{}")
        competitor_snapshot = json.loads(product.pop("competitor_snapshot", "[]") or "[]")
        product["raw_enrichment"] = json.loads(product.get("raw_enrichment") or "{}")
        product["effective_min"] = product["user_min"] or product["market_low"]
        product["effective_max"] = product["user_max"] or product["market_high"]
        product["customized"] = bool(product["user_min"] or product["user_max"])
        product["basalam_url"] = (
            f"https://basalam.com/p/{product['product_id']}"
            if account.get("marketplace") == "basalam"
            else None
        )
        product["basalam_price_history"] = own_price_history[product["product_id"]]
        product_sales = int(product.pop("tracked_sales_180d") or 0)
        tracked_sales += product_sales
        tracked_revenue += int(product.pop("tracked_revenue_180d") or 0)
        product_opportunity = int(product.pop("estimated_opportunity_180d") or 0)
        estimated_opportunity += product_opportunity
        product["premium_analytics"] = (
            {
                "tracked_sales_180d": product_sales,
                "estimated_revenue_opportunity_180d": product_opportunity,
                "competitors": competitor_snapshot,
                "sales_history": sales_history[product["product_id"]],
                "own_price_history": own_price_history[product["product_id"]],
                "market_recommendation_history": market_history[product["product_id"]],
                "method": "current_recommendation_backtest",
            }
            if premium_active
            else None
        )
        if product["market_suggested"]:
            ready += 1
    return {
        "account": account,
        "summary": {
            "products": len(products),
            "estimated": ready,
            "customized": sum(1 for item in products if item["customized"]),
            "refresh_hours": settings.merchant_sync_hours,
            "product_limit": settings.merchant_product_limit,
        },
        "premium": {
            "active": premium_active,
            "subscription_status": subscription.get("status") if subscription else None,
            "window_days": 180,
            "analytics": (
                {
                    "tracked_sales": tracked_sales,
                    "tracked_revenue": tracked_revenue,
                    "estimated_revenue_opportunity": estimated_opportunity,
                    "method": "current_recommendation_backtest",
                    "disclaimer": (
                        "سناریوی درآمدی بر پایه تعداد فروش واقعی و پیشنهاد فعلی مدل است؛ "
                        "سود قطعی یا تضمین تکرار فروش نیست."
                    ),
                }
                if premium_active
                else None
            ),
            "teaser": {
                "has_sales_history": tracked_sales > 0,
                "has_competitor_data": any(item.get("sample_size") for item in products),
                "title": "تحلیل رقبا و فرصت درآمدی ۱۸۰ روزه آماده است",
            },
        },
        "products": products,
    }


def _merchant_elasticity_points(user_id: int, product_id: int) -> list[ElasticityPoint]:
    points = [
        ElasticityPoint(
            price=float(item["price"]),
            units=float(item["units"]),
            period=item["period"],
            source=item["source"],
        )
        for item in rows(
            """SELECT period,price,units,source
            FROM merchant_elasticity_observations
            WHERE user_id=? AND product_id=? ORDER BY period""",
            (user_id, product_id),
        )
    ]
    # Parcel history is optional. When consent is unavailable this simply returns no API points.
    points.extend(
        ElasticityPoint(
            price=float(item["price"]),
            units=float(item["units"]),
            period=item["period"],
            source="basalam_api",
        )
        for item in rows(
            """SELECT substr(sold_at,1,10) AS period,unit_price AS price,
            SUM(quantity) AS units
            FROM merchant_sales_events
            WHERE user_id=? AND product_id=? AND unit_price>0
            GROUP BY substr(sold_at,1,10),unit_price ORDER BY period""",
            (user_id, product_id),
        )
    )
    return points


@app.get("/api/merchant/products/{product_id}/elasticity")
def merchant_elasticity(product_id: int, request: Request) -> dict[str, Any]:
    user_id = _merchant_user(request)
    product_rows = rows(
        "SELECT product_id,title,current_price,market_suggested FROM merchant_products WHERE user_id=? AND product_id=?",
        (user_id, product_id),
    )
    if not product_rows:
        raise HTTPException(404, "محصول پیدا نشد.")
    product = product_rows[0]
    analysis = analyze_elasticity(
        _merchant_elasticity_points(user_id, product_id),
        current_price=int(product["current_price"] or 0),
        market_suggested=product["market_suggested"],
    )
    return {"product": product, "analysis": analysis}


@app.post("/api/merchant/products/{product_id}/elasticity")
def save_merchant_elasticity(
    product_id: int,
    payload: ElasticityObservationsInput,
    request: Request,
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    exists = rows(
        "SELECT 1 FROM merchant_products WHERE user_id=? AND product_id=?",
        (user_id, product_id),
    )
    if not exists:
        raise HTTPException(404, "محصول پیدا نشد.")
    with connection() as db:
        db.execute(
            "DELETE FROM merchant_elasticity_observations WHERE user_id=? AND product_id=? AND source='manual'",
            (user_id, product_id),
        )
        db.executemany(
            """INSERT INTO merchant_elasticity_observations
            (user_id,product_id,period,price,units,source,created_at)
            VALUES(?,?,?,?,?,'manual',?)""",
            [
                (user_id, product_id, item.period.strip(), item.price, item.units, now_iso())
                for item in payload.observations
            ],
        )
    return merchant_elasticity(product_id, request)


@app.post("/api/merchant/analyze-product/{product_id}")
async def merchant_analyze_product(
    product_id: int,
    request: Request,
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    product_rows = rows(
        """SELECT * FROM merchant_products WHERE user_id=? AND product_id=?""",
        (user_id, product_id),
    )
    if not product_rows:
        raise HTTPException(404, "محصول پیدا نشد.")
    product = product_rows[0]
    product_id_val = product["product_id"]
    account_rows = rows("SELECT marketplace FROM accounts WHERE user_id=?", (user_id,))
    marketplace = account_rows[0].get("marketplace") if account_rows else "basalam"
    source_product: dict[str, Any] | None = None
    search_query = product["title"]
    if marketplace == "basalam":
        try:
            source_product = await fetch_basalam_product(product_id_val)
            if source_product and source_product.get("title"):
                search_query = source_product["title"]
                if settings.llm_similarity_enabled and settings.avalai_api_key:
                    optimized = await optimize_search_query(source_product)
                    if optimized:
                        search_query = optimized
        except Exception as exc:
            logger.warning("fetch_basalam_product failed for %s: %s", product_id_val, exc)

    crawl = await market_crawler.search(search_query)
    listings = exclude_marketplace_product(
        crawl["listings"],
        marketplace or "basalam",
        product_id_val,
    )
    llm_scores: dict[str, float] = {}
    if settings.llm_similarity_enabled:
        llm_scores = await score_product_similarity(search_query, listings, source_product)
    listings = [
        item for item in listings if item.source in INTERNAL_MARKETPLACE_SOURCES
    ]
    llm_scores = {
        item.url: llm_scores[item.url]
        for item in listings
        if item.url in llm_scores
    }
    analysis = analyze_listings(listings, llm_scores)
    return {
        "product_id": product_id_val,
        "title": product["title"],
        "source_product": source_product,
        "analysis": analysis,
        "basalam_edit_url": (
            f"https://vendor.basalam.com/edit-product/{product_id_val}"
            if marketplace == "basalam"
            else None
        ),
    }


@app.get("/merchant/notifications", response_class=HTMLResponse)
def merchant_notifications_page(request: Request) -> HTMLResponse:
    user_id = _merchant_user(request)
    account = rows(
        "SELECT vendor_title,user_name FROM accounts WHERE user_id=?",
        (user_id,),
    )
    if not account:
        response = RedirectResponse("/login")
        response.delete_cookie(COOKIE_NAME)
        return response
    unread_count = rows(
        """SELECT COUNT(*) AS count FROM merchant_notifications
        WHERE user_id=? AND read_at IS NULL""",
        (user_id,),
    )[0]["count"]
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={"account": account[0], "unread_count": unread_count},
    )


@app.get("/api/merchant/notifications")
def merchant_notifications(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    notifications = rows(
        """SELECT id,kind,title,body,target_url,metadata,read_at,created_at
        FROM merchant_notifications WHERE user_id=?
        ORDER BY created_at DESC, id DESC LIMIT ?""",
        (user_id, limit),
    )
    unread_count = rows(
        """SELECT COUNT(*) AS count FROM merchant_notifications
        WHERE user_id=? AND read_at IS NULL""",
        (user_id,),
    )[0]["count"]
    return {
        "unread_count": unread_count,
        "notifications": [_notification_payload(item) for item in notifications],
    }


@app.patch("/api/merchant/notifications/{notification_id}/read")
def mark_merchant_notification_read(
    notification_id: int,
    request: Request,
) -> dict[str, bool]:
    user_id = _merchant_user(request)
    with connection() as db:
        result = db.execute(
            """UPDATE merchant_notifications SET read_at=COALESCE(read_at, ?)
            WHERE user_id=? AND id=?""",
            (now_iso(), user_id, notification_id),
        )
    if result.rowcount == 0:
        raise HTTPException(404, "اعلان پیدا نشد.")
    return {"ok": True}


@app.post("/api/merchant/notifications/read-all")
def mark_all_merchant_notifications_read(request: Request) -> dict[str, bool]:
    user_id = _merchant_user(request)
    with connection() as db:
        db.execute(
            """UPDATE merchant_notifications SET read_at=COALESCE(read_at, ?)
            WHERE user_id=? AND read_at IS NULL""",
            (now_iso(), user_id),
        )
    return {"ok": True}


@app.post("/api/merchant/sync")
def merchant_manual_sync(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    account = rows("SELECT sync_status FROM accounts WHERE user_id=?", (user_id,))
    if not account:
        raise HTTPException(404, "غرفه پیدا نشد.")
    if account[0]["sync_status"] in {"running", "queued"}:
        return {"ok": True, "status": account[0]["sync_status"]}
    with connection() as db:
        db.execute(
            "UPDATE accounts SET sync_status='queued',sync_error=NULL WHERE user_id=?",
            (user_id,),
        )
    background_tasks.add_task(merchant_sync.sync_user, user_id)
    return {"ok": True, "status": "queued"}


@app.post("/api/merchant/refresh-prices")
def merchant_price_refresh(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    account = rows("SELECT sync_status FROM accounts WHERE user_id=?", (user_id,))
    if not account:
        raise HTTPException(404, "غرفه پیدا نشد.")
    if account[0]["sync_status"] in {"running", "queued"}:
        return {"ok": True, "status": account[0]["sync_status"]}
    with connection() as db:
        db.execute(
            "UPDATE accounts SET sync_status='queued',sync_error=NULL WHERE user_id=?",
            (user_id,),
        )
    background_tasks.add_task(merchant_sync.refresh_prices, user_id)
    return {"ok": True, "status": "queued"}


@app.patch("/api/merchant/products/{product_id}/range")
def update_merchant_range(
    product_id: int,
    payload: RangeOverrideInput,
    request: Request,
) -> dict[str, Any]:
    user_id = _merchant_user(request)
    product_rows = rows(
        """SELECT market_low,market_high FROM merchant_products
        WHERE user_id=? AND product_id=?""",
        (user_id, product_id),
    )
    if not product_rows:
        raise HTTPException(404, "محصول پیدا نشد.")
    market = product_rows[0]
    effective_min = payload.min_price or market["market_low"]
    effective_max = payload.max_price or market["market_high"]
    if effective_min and effective_max and effective_min > effective_max:
        raise HTTPException(422, "حداقل قیمت نمی‌تواند از حداکثر بیشتر باشد.")
    with connection() as db:
        db.execute(
            """UPDATE merchant_products SET user_min=?,user_max=?
            WHERE user_id=? AND product_id=?""",
            (payload.min_price, payload.max_price, user_id, product_id),
        )
    return {"ok": True}


@app.post("/internal/merchant-sync")
async def scheduled_merchant_sync(
    x_cron_secret: str = Header(default=""),
) -> dict[str, Any]:
    if not settings.cron_secret or not hmac.compare_digest(
        x_cron_secret, settings.cron_secret
    ):
        raise HTTPException(401, "Invalid scheduler secret.")
    results = await merchant_sync.sync_due_users()
    return {"ok": True, "accounts": len(results), "results": results}


@app.post("/internal/usdt-rate-check")
async def scheduled_usdt_rate_check(
    x_cron_secret: str = Header(default=""),
) -> dict[str, Any]:
    if not settings.cron_secret or not hmac.compare_digest(
        x_cron_secret, settings.cron_secret
    ):
        raise HTTPException(401, "Invalid scheduler secret.")
    return await check_usdt_rate_change()


def _product_payload(product: dict[str, Any]) -> dict[str, Any]:
    prices = json.loads(product.pop("comparable_prices", "[]"))
    band = recommend_price(
        product["price"],
        prices,
        views=product["views"],
        sales=product["sales"],
        objective=product.get("objective") or "balanced",
    )
    product["enabled"] = bool(product.get("enabled"))
    product["recommendation"] = band.__dict__
    return product


def _legacy_demo_only() -> None:
    if not settings.demo_mode:
        raise HTTPException(
            410,
            "این مسیر قدیمی غیرفعال است؛ از داشبورد پیشنهاد دهنده هوشمند لحظه‌ای قیمت استفاده کنید.",
        )


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    _legacy_demo_only()
    products = rows(
        """SELECT p.*, po.enabled, po.floor_price, po.objective,
        po.interval_days, po.max_drop_percent, po.last_changed_at
        FROM products p LEFT JOIN policies po ON po.product_id=p.id
        ORDER BY p.stock > 0 DESC, p.views DESC"""
    )
    history = rows("SELECT * FROM price_history ORDER BY created_at DESC LIMIT 8")
    active = sum(1 for p in products if p.get("enabled"))
    opportunity = sum(
        max(0, _product_payload(dict(p))["recommendation"]["suggested"] - p["price"])
        * min(p["stock"], 10)
        for p in products
    )
    return {
        "vendor": {"title": "غرفه نمونه دقیقه" if settings.demo_mode else "غرفه متصل"},
        "mode": "demo" if settings.demo_mode else "live",
        "metrics": {
            "products": len(products),
            "active_policies": active,
            "opportunity": opportunity,
            "changes_30d": len(history),
        },
        "products": [_product_payload(dict(p)) for p in products],
        "history": history,
    }


@app.post("/api/products/{product_id}/policy")
def save_policy(product_id: int, payload: PolicyInput) -> dict[str, Any]:
    _legacy_demo_only()
    with connection() as db:
        product = db.execute("SELECT price FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found.")
        if payload.floor_price > product["price"]:
            raise HTTPException(422, "Floor price cannot exceed the current price.")
        db.execute(
            """INSERT INTO policies
            (product_id,enabled,floor_price,objective,interval_days,max_drop_percent,last_changed_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(product_id) DO UPDATE SET enabled=excluded.enabled,
              floor_price=excluded.floor_price, objective=excluded.objective,
              interval_days=excluded.interval_days,
              max_drop_percent=excluded.max_drop_percent""",
            (
                product_id,
                int(payload.enabled),
                payload.floor_price,
                payload.objective,
                payload.interval_days,
                payload.max_drop_percent,
                now_iso(),
            ),
        )
    return {"ok": True}


@app.post("/api/products/{product_id}/apply")
async def apply_recommendation(product_id: int) -> dict[str, Any]:
    _legacy_demo_only()
    with connection() as db:
        row = db.execute(
            """SELECT p.*, po.objective FROM products p
            LEFT JOIN policies po ON po.product_id=p.id WHERE p.id=?""",
            (product_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Product not found.")
        product = dict(row)
        band = recommend_price(
            product["price"],
            json.loads(product["comparable_prices"]),
            views=product["views"],
            sales=product["sales"],
            objective=product.get("objective") or "balanced",
        )
        if band.suggested == product["price"]:
            return {"ok": True, "changed": False, "price": product["price"]}
        if not settings.demo_mode:
            account = db.execute(
                "SELECT access_token FROM accounts WHERE marketplace='basalam' LIMIT 1"
            ).fetchone()
            if not account:
                raise HTTPException(409, "Connect a Basalam booth first.")
            try:
                await basalam.update_price(
                    decrypt_token(account["access_token"]),
                    product_id,
                    band.suggested,
                )
            except BasalamError as exc:
                raise HTTPException(502, str(exc)) from exc
        db.execute("UPDATE products SET price=? WHERE id=?", (band.suggested, product_id))
        db.execute(
            """INSERT INTO price_history(product_id,old_price,new_price,reason,created_at)
            VALUES (?,?,?,?,?)""",
            (product_id, product["price"], band.suggested, band.reason, now_iso()),
        )
    return {"ok": True, "changed": True, "price": band.suggested}


@app.post("/api/sync")
async def sync_products() -> dict[str, Any]:
    _legacy_demo_only()
    if settings.demo_mode:
        return {"ok": True, "synced": len(rows("SELECT id FROM products")), "demo": True}
    with connection() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE marketplace='basalam' LIMIT 1"
        ).fetchone()
        if not account:
            raise HTTPException(409, "Connect a Basalam booth first.")
        try:
            remote = await basalam.products(
                decrypt_token(account["access_token"]),
                account["vendor_id"],
            )
        except BasalamError as exc:
            raise HTTPException(502, str(exc)) from exc
        token = decrypt_token(account["access_token"])
        semaphore = asyncio.Semaphore(5)

        async def comparable_prices(item: dict[str, Any]) -> list[int]:
            async with semaphore:
                try:
                    matches = await basalam.search_comparables(
                        token,
                        item.get("title") or item.get("name", ""),
                    )
                except BasalamError:
                    return []
            prices = []
            for match in matches:
                if match.get("id") == item.get("id"):
                    continue
                price = match.get("price") or match.get("primaryPrice") or match.get("primary_price")
                if price:
                    prices.append(int(price))
            return prices

        market_prices = await asyncio.gather(*(comparable_prices(item) for item in remote))
        for item, comparables in zip(remote, market_prices, strict=True):
            photo = item.get("photo") or {}
            db.execute(
                """INSERT INTO products
                (id,vendor_id,title,price,stock,views,sales,image_url,comparable_prices,synced_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,price=excluded.price,
                  stock=excluded.stock,views=excluded.views,sales=excluded.sales,
                  image_url=excluded.image_url,comparable_prices=excluded.comparable_prices,
                  synced_at=excluded.synced_at""",
                (
                    item["id"],
                    account["vendor_id"],
                    item.get("title") or item.get("name", "بدون نام"),
                    int(item.get("price") or item.get("primary_price") or 0),
                    int(item.get("inventory") or item.get("stock") or 0),
                    int(item.get("view_count") or item.get("views") or 0),
                    int(item.get("sales_count") or item.get("sales") or 0),
                    photo.get("md") if isinstance(photo, dict) else "",
                    json.dumps(comparables),
                    now_iso(),
                ),
            )
    return {"ok": True, "synced": len(remote)}


@app.post("/internal/reprice")
async def scheduled_reprice(x_cron_secret: str = Header(default="")) -> dict[str, Any]:
    _legacy_demo_only()
    if not settings.cron_secret or not hmac.compare_digest(x_cron_secret, settings.cron_secret):
        raise HTTPException(401, "Invalid scheduler secret.")
    changed: list[dict[str, int]] = []
    candidates = rows(
        """SELECT p.*,po.* FROM products p JOIN policies po ON po.product_id=p.id
        WHERE po.enabled=1 AND p.stock>0"""
    )
    for item in candidates:
        band = recommend_price(
            item["price"],
            json.loads(item["comparable_prices"]),
            views=item["views"],
            sales=item["sales"],
            objective=item["objective"],
        )
        last = datetime.fromisoformat(item["last_changed_at"]) if item["last_changed_at"] else datetime.min.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - last).days
        decision = decide_reprice(
            current_price=item["price"],
            suggested_price=band.suggested,
            floor_price=item["floor_price"],
            days_since_change=days,
            interval_days=item["interval_days"],
            max_drop_percent=item["max_drop_percent"],
        )
        if decision.should_change:
            # In live mode, changes still flow through the same reviewed adapter.
            if not settings.demo_mode:
                with connection() as db:
                    account = db.execute(
                        "SELECT access_token FROM accounts WHERE marketplace='basalam' LIMIT 1"
                    ).fetchone()
                if not account:
                    continue
                await basalam.update_price(decrypt_token(account["access_token"]), item["id"], decision.new_price)
            with connection() as db:
                db.execute("UPDATE products SET price=? WHERE id=?", (decision.new_price, item["id"]))
                db.execute("UPDATE policies SET last_changed_at=? WHERE product_id=?", (now_iso(), item["id"]))
                db.execute(
                    "INSERT INTO price_history(product_id,old_price,new_price,reason,created_at) VALUES(?,?,?,?,?)",
                    (item["id"], item["price"], decision.new_price, decision.reason, now_iso()),
                )
            changed.append({"product_id": item["id"], "new_price": decision.new_price})
    return {"ok": True, "changed": changed}


@app.post("/webhooks/subscription")
async def subscription_webhook(
    request: Request,
    x_webhook_secret: str = Header(default=""),
) -> dict[str, bool]:
    if not settings.webhook_secret or not hmac.compare_digest(x_webhook_secret, settings.webhook_secret):
        raise HTTPException(401, "Invalid webhook secret.")
    payload = await request.json()
    event = payload.get("event_type", "")
    if event not in {"subscription.created", "subscription.renewed", "subscription.cancelled"}:
        return {"ok": True}
    data = payload.get("data") or {}
    customer_id = payload.get("customer_id") or (data.get("customer") or {}).get("id")
    plan = data.get("plan") or {}
    if not customer_id or not plan.get("id"):
        raise HTTPException(422, "Incomplete subscription payload.")
    with connection() as db:
        db.execute(
            """INSERT INTO subscriptions(customer_id,subscription_id,plan_id,status,period_end,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(customer_id) DO UPDATE SET subscription_id=excluded.subscription_id,
              plan_id=excluded.plan_id,status=excluded.status,period_end=excluded.period_end,
              updated_at=excluded.updated_at""",
            (
                customer_id,
                payload.get("subscription_id") or data.get("id"),
                plan["id"],
                (data.get("status") or {}).get("slug", "cancelled" if event.endswith("cancelled") else "active"),
                data.get("current_period_end"),
                now_iso(),
            ),
        )
    return {"ok": True}
