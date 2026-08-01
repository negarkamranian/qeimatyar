from app.config import refresh_settings, save_admin_overrides, settings
from app.db import connection, init_db, now_iso
from app.main import (
    admin_dashboard_context,
    admin_login,
    admin_update_settings,
    clear_all_merchant_notifications,
)


def _call_admin_update_settings(**kwargs):
    return admin_update_settings(None, **kwargs)


def test_admin_panel_exposes_connected_users():
    init_db()
    with connection() as db:
        db.execute("DELETE FROM accounts WHERE user_id IN (9001, 9002)")
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,token_expires_at,sync_status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (9001, 10001, "فروشگاه تست", "کاربر تست", "token", now_iso(), now_iso(), "idle"),
        )
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,token_expires_at,sync_status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (9002, 10002, "فروشگاه دوم", "کاربر دوم", "token2", now_iso(), now_iso(), "running"),
        )

    context = admin_dashboard_context(None)
    assert context["user_count"] >= 2
    assert any(user["user_id"] == 9001 for user in context["users"])
    assert any(user["user_id"] == 9002 for user in context["users"])

    with connection() as db:
        db.execute("DELETE FROM accounts WHERE user_id IN (9001, 9002)")


def test_admin_panel_clears_all_merchant_notifications():
    init_db()
    with connection() as db:
        db.execute("DELETE FROM merchant_notifications")
        db.execute("DELETE FROM accounts WHERE user_id IN (8001, 8002)")
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,token_expires_at,sync_status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (8001, 18001, "فروشگاه تست", "کاربر تست", "token", now_iso(), now_iso(), "idle"),
        )
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,token_expires_at,sync_status)
            VALUES(?,?,?,?,?,?,?,?)""",
            (8002, 18002, "فروشگاه دوم", "کاربر دوم", "token2", now_iso(), now_iso(), "running"),
        )
        db.execute(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,created_at)
            VALUES(?,?,?,?,?,?)""",
            (8001, "price_opportunity", "عنوان ۱", "بدنه ۱", "/merchant", now_iso()),
        )
        db.execute(
            """INSERT INTO merchant_notifications
            (user_id,kind,title,body,target_url,created_at)
            VALUES(?,?,?,?,?,?)""",
            (8002, "price_opportunity", "عنوان ۲", "بدنه ۲", "/merchant", now_iso()),
        )

    cleared_count = clear_all_merchant_notifications()
    assert cleared_count == 2

    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM merchant_notifications").fetchone()[0] == 0
        db.execute("DELETE FROM accounts WHERE user_id IN (8001, 8002)")


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
