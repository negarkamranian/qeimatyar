from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.app_env: str = os.getenv("APP_ENV", "development")
        self.log_level: str = os.getenv("APP_LOG_LEVEL", "INFO").upper()
        self.base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        self.secret: str = os.getenv("APP_SECRET", "development-only-secret-change-me")
        self.database_path: str = os.getenv("DATABASE_PATH", "data/nerkhban.db")
        self.client_id: str = os.getenv("BASALAM_CLIENT_ID", "")
        self.client_secret: str = os.getenv("BASALAM_CLIENT_SECRET", "")
        self.redirect_uri: str = os.getenv(
            "BASALAM_REDIRECT_URI",
            "http://localhost:8000/auth/basalam/callback",
        )
        self.scopes: str = os.getenv(
            "BASALAM_SCOPES",
            "customer.profile.read vendor.profile.read vendor.product.read",
        )
        self.webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
        self.cron_secret: str = os.getenv("CRON_SECRET", "")
        self.demo_mode: bool = _bool("DEMO_MODE", True)
        self.marketplace_trust_env: bool = _bool("MARKETPLACE_TRUST_ENV", False)
        self.merchant_product_limit: int = int(os.getenv("MERCHANT_PRODUCT_LIMIT", "50"))
        self.merchant_sync_hours: int = int(os.getenv("MERCHANT_SYNC_HOURS", "6"))
        self.usdt_notification_enabled: bool = _bool("USDT_NOTIFICATION_ENABLED", True)
        self.usdt_notification_percent: float = float(os.getenv("USDT_NOTIFICATION_PERCENT", "1"))
        self.usdt_check_interval_minutes: int = int(os.getenv("USDT_CHECK_INTERVAL_MINUTES", "30"))
        self.admin_password: str = os.getenv("ADMIN_PASSWORD", "")
        self.admin_settings_file: str = os.getenv("ADMIN_SETTINGS_FILE", "data/admin_settings.json")
        self.admin_enabled: bool = _bool("ADMIN_ENABLED", bool(os.getenv("ADMIN_PASSWORD", "")))


def _load_admin_overrides() -> dict[str, Any]:
    path = Path(os.getenv("ADMIN_SETTINGS_FILE", "data/admin_settings.json"))
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _apply_admin_overrides(target: Settings, overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        if key == "MERCHANT_SYNC_HOURS":
            target.merchant_sync_hours = int(value)
        elif key == "USDT_NOTIFICATION_ENABLED":
            target.usdt_notification_enabled = str(value).lower() in {"1", "true", "yes", "on"}
        elif key == "USDT_NOTIFICATION_PERCENT":
            target.usdt_notification_percent = float(value)
        elif key == "USDT_CHECK_INTERVAL_MINUTES":
            target.usdt_check_interval_minutes = int(value)


def _env_file_paths() -> list[Path]:
    """Return env file paths to write to: always the primary .env plus .env.production if it exists."""
    paths = [Path(os.getenv("ENV_FILE", ".env"))]
    prod = Path(os.getenv("ENV_FILE_PRODUCTION", ".env.production"))
    if prod.is_file():
        paths.append(prod)
    return paths


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from an env file into os.environ, overriding existing values for those keys."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            os.environ[key] = value


def refresh_settings() -> Settings:
    global settings
    prod_env = Path(os.getenv("ENV_FILE_PRODUCTION", ".env.production"))
    _load_env_file(prod_env)
    new_settings = Settings()
    _apply_admin_overrides(new_settings, _load_admin_overrides())
    for field_name, value in new_settings.__dict__.items():
        setattr(settings, field_name, value)
    return settings


def _write_env_file_to_path(env_path: Path, overrides: dict[str, Any]) -> None:
    env_path.touch(exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            updated.append(line)
            continue
        key = stripped.split("=", 1)[0]
        if key in overrides:
            updated.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in overrides.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _write_env_file(overrides: dict[str, Any]) -> None:
    for env_path in _env_file_paths():
        _write_env_file_to_path(env_path, overrides)


def save_admin_overrides(overrides: dict[str, Any]) -> Settings:
    path = Path(os.getenv("ADMIN_SETTINGS_FILE", "data/admin_settings.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_env_file(overrides)
    return refresh_settings()


settings = Settings()
refresh_settings()
