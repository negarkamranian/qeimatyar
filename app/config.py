from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    secret: str = os.getenv("APP_SECRET", "development-only-secret-change-me")
    database_path: str = os.getenv("DATABASE_PATH", "data/nerkhban.db")
    client_id: str = os.getenv("BASALAM_CLIENT_ID", "")
    client_secret: str = os.getenv("BASALAM_CLIENT_SECRET", "")
    redirect_uri: str = os.getenv(
        "BASALAM_REDIRECT_URI",
        "http://localhost:8000/auth/basalam/callback",
    )
    scopes: str = os.getenv(
        "BASALAM_SCOPES",
        "customer.profile.read vendor.profile.read vendor.product.read",
    )
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    cron_secret: str = os.getenv("CRON_SECRET", "")
    demo_mode: bool = _bool("DEMO_MODE", True)
    marketplace_trust_env: bool = _bool("MARKETPLACE_TRUST_ENV", False)
    merchant_product_limit: int = int(os.getenv("MERCHANT_PRODUCT_LIMIT", "50"))
    merchant_sync_hours: int = int(os.getenv("MERCHANT_SYNC_HOURS", "6"))
    usdt_notification_enabled: bool = _bool("USDT_NOTIFICATION_ENABLED", True)
    usdt_notification_percent: float = float(os.getenv("USDT_NOTIFICATION_PERCENT", "1"))


settings = Settings()
