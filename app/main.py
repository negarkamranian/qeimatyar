from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import secrets
import time
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.basalam import BasalamError, basalam, decrypt_token, encrypt_token
from app.config import refresh_settings, save_admin_overrides, settings
from app.currency_notifications import check_usdt_rate_change
from app.db import connection, init_db, now_iso, rows, seed_demo
from app.llm import score_product_similarity
from app.marketplaces import (
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


app = FastAPI(title="قیمت‌یار", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _admin_session(request: Request | None) -> bool:
    if request is None:
        return False
    return request.cookies.get("admin_session") == settings.secret


def admin_dashboard_context(request: Request | None) -> dict[str, Any]:
    users = rows(
        """SELECT a.user_id, a.vendor_id, a.vendor_title, a.user_name, a.sync_status,
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
        user["token_expired"] = _is_token_expired(user.get("token_expires_at"))
    return {
        "settings": settings,
        "admin_settings_file": settings.admin_settings_file,
        "users": users,
        "user_count": len(users),
        "active_users": sum(1 for user in users if user["is_active"]),
        "synced_users": sum(1 for user in users if user["products_synced"]),
    }


def _is_token_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
    except ValueError:
        return True


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request) -> HTMLResponse:
    if _admin_session(request):
        return RedirectResponse("/admin")
    return templates.TemplateResponse(request=request, name="admin_login.html", context={})


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)) -> RedirectResponse:
    if not settings.admin_enabled or not settings.admin_password:
        raise HTTPException(403, "پنل مدیریت فعال نیست.")
    if password != settings.admin_password:
        raise HTTPException(401, "رمزعبور اشتباه است.")
    response = RedirectResponse("/admin", status_code=303)
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
    from app.config import _env_file_paths
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
    product_name: str = Field(min_length=2, max_length=1000)
    exclude_basalam_product_id: int | None = Field(default=None, gt=0)


class RangeOverrideInput(BaseModel):
    min_price: int | None = Field(default=None, gt=0)
    max_price: int | None = Field(default=None, gt=0)


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
        context={"oauth_ready": bool(settings.client_id and settings.client_secret)},
    )


@app.get("/merchant", response_class=HTMLResponse)
def merchant_page(request: Request):
    user_id = read_session(request.cookies.get(COOKIE_NAME))
    if not user_id:
        return RedirectResponse("/login")
    account = rows(
        "SELECT vendor_title,user_name FROM accounts WHERE user_id=?",
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


@app.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "demo" if settings.demo_mode else "live"}


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
    if merchant_user_id and payload.exclude_basalam_product_id:
        merchant_rows = rows(
            """SELECT product_id,title,current_price FROM merchant_products
            WHERE user_id=? AND product_id=?""",
            (merchant_user_id, payload.exclude_basalam_product_id),
        )
        if merchant_rows:
            merchant_product = merchant_rows[0]
    try:
        crawl = await market_crawler.search(query)
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
                "basalam",
                excluded_product_id,
            )
        llm_scores: dict[str, float] = {}
        if settings.llm_similarity_enabled:
            llm_scores = await score_product_similarity(query, listings)
        analysis = analyze_listings(listings, llm_scores)
    except ValueError as exc:
        raise HTTPException(
            422,
            {
                "message": str(exc),
                "sources": statuses,
                "raw_count": crawl["raw_count"],
            },
        ) from exc
    return {
        "query": query,
        "resolved_from_url": resolved_from_url,
        "merchant_product": merchant_product,
        "analysis": analysis,
        "sources": statuses,
        "raw_count": crawl["raw_count"],
        "disclaimer": "این بازه از قیمت‌های فعلی فروش ساخته شده، نه تراکنش‌های قطعی‌شده.",
    }


@app.get("/auth/basalam")
def connect_basalam() -> RedirectResponse:
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
        "oauth_authorization_started trace_id=%s redirect_uri=%s scopes=%s "
        "state_fingerprint=%s elapsed_ms=%s",
        trace_id,
        settings.redirect_uri,
        settings.scopes.split(),
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
                 token_expires_at,connected_at,sync_status)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  vendor_id=excluded.vendor_id, vendor_title=excluded.vendor_title,
                  user_name=excluded.user_name,
                  access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                  token_expires_at=excluded.token_expires_at,
                  connected_at=excluded.connected_at,sync_status='queued',
                  sync_error=NULL""",
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
            body="قیمت‌یار محصولات غرفه را دریافت می‌کند و پیشنهادهای قیمت را در همین داشبورد نشان می‌دهد.",
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
        sync_status,sync_error FROM accounts WHERE user_id=?""",
        (user_id,),
    )
    if not account_rows:
        raise HTTPException(401, "اتصال غرفه پیدا نشد.")
    account = account_rows[0]
    products = rows(
        """SELECT * FROM merchant_products WHERE user_id=?
        ORDER BY stock > 0 DESC, estimate_error IS NULL DESC, title""",
        (user_id,),
    )
    ready = 0
    for product in products:
        product["source_counts"] = json.loads(product.get("source_counts") or "{}")
        product["effective_min"] = product["user_min"] or product["market_low"]
        product["effective_max"] = product["user_max"] or product["market_high"]
        product["customized"] = bool(product["user_min"] or product["user_max"])
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
        "products": products,
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
            "این مسیر قدیمی غیرفعال است؛ از داشبورد پیشنهاد دهنده هوشمند قیمت استفاده کنید.",
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
        "vendor": {"title": "غرفه نمونه قیمت‌یار" if settings.demo_mode else "غرفه متصل"},
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
            account = db.execute("SELECT access_token FROM accounts LIMIT 1").fetchone()
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
        account = db.execute("SELECT * FROM accounts LIMIT 1").fetchone()
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
                    account = db.execute("SELECT access_token FROM accounts LIMIT 1").fetchone()
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
