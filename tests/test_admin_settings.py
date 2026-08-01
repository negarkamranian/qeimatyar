from app.config import refresh_settings, save_admin_overrides, settings
from app.main import admin_login, admin_update_settings


def _call_admin_update_settings(**kwargs):
    return admin_update_settings(None, **kwargs)


def test_admin_panel_updates_runtime_settings(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "super-secret")
    monkeypatch.setenv("ADMIN_ENABLED", "true")
    refresh_settings()

    response = admin_login(None, password="super-secret")
    assert response.status_code == 303

    response = _call_admin_update_settings(
        merchant_sync_hours=9,
        usdt_notification_enabled="false",
        usdt_notification_percent=1.5,
        usdt_check_interval_minutes=60,
    )
    assert response.status_code == 303

    assert settings.merchant_sync_hours == 9
    assert settings.usdt_notification_enabled is False
    assert settings.usdt_notification_percent == 1.5
    assert settings.usdt_check_interval_minutes == 60

    save_admin_overrides({})
