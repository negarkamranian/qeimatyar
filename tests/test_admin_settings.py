from app.config import (
    _env_file_paths,
    refresh_settings,
    save_admin_overrides,
    settings,
)
from app.db import connection, init_db, now_iso
from app.main import (
    app,
    admin_dashboard_context,
    admin_login,
    admin_update_settings,
    clear_all_merchant_notifications,
)
from pathlib import Path
from fastapi.testclient import TestClient


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


def test_admin_users_links_merchant_name_to_basalam_and_expands_products():
    init_db()
    user_id = 9010
    with connection() as db:
        db.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,connected_at,sync_status)
            VALUES(?,?,?,?,?,?,?)""",
            (user_id, 10010, "غرفه لینک تست", "ehsansb44", "token", now_iso(), "idle"),
        )
        db.execute(
            """INSERT INTO merchant_products
            (user_id,product_id,title,current_price,stock,product_url,synced_at)
            VALUES(?,?,?,?,?,?,?)""",
            (user_id, 5010, "محصول لینک تست", 125000, 3, "https://basalam.com/p/5010", now_iso()),
        )

    try:
        with TestClient(app) as client:
            client.cookies.set("admin_session", settings.secret)
            response = client.get("/admin/users")

        assert response.status_code == 200
        assert f'href="https://basalam.com/ehsansb44"' in response.text
        assert "غرفه لینک تست ↗" in response.text
        assert f'data-products-toggle="{user_id}"' in response.text
        assert "محصول لینک تست" in response.text
        assert "<th>جزئیات</th>" not in response.text
        assert "مشاهده ←" not in response.text
    finally:
        with connection() as db:
            db.execute("DELETE FROM merchant_products WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM accounts WHERE user_id=?", (user_id,))


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


def test_admin_settings_redirect_has_toast_param():
    response = _call_admin_update_settings(
        merchant_sync_hours=9,
        usdt_notification_enabled="false",
        usdt_notification_percent=1.5,
        usdt_check_interval_minutes=60,
    )
    assert response.status_code == 303
    assert "?saved=1" in response.headers["location"]


def test_settings_write_to_env_production(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    prod_file = tmp_path / ".env.production"
    env_file.write_text("MERCHANT_SYNC_HOURS=6\nUSDT_NOTIFICATION_ENABLED=true\n", encoding="utf-8")
    prod_file.write_text("MERCHANT_SYNC_HOURS=6\nUSDT_NOTIFICATION_ENABLED=true\n", encoding="utf-8")

    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("ENV_FILE_PRODUCTION", str(prod_file))
    monkeypatch.setenv("ADMIN_SETTINGS_FILE", str(tmp_path / "overrides.json"))

    save_admin_overrides(
        {
            "MERCHANT_SYNC_HOURS": 12,
            "USDT_NOTIFICATION_ENABLED": "false",
            "USDT_NOTIFICATION_PERCENT": 2.5,
            "USDT_CHECK_INTERVAL_MINUTES": 45,
        }
    )

    prod_content = prod_file.read_text(encoding="utf-8")
    assert "MERCHANT_SYNC_HOURS=12" in prod_content
    assert "USDT_NOTIFICATION_ENABLED=false" in prod_content
    assert "USDT_NOTIFICATION_PERCENT=2.5" in prod_content
    assert "USDT_CHECK_INTERVAL_MINUTES=45" in prod_content


def test_settings_load_from_env_production(tmp_path, monkeypatch):
    prod_file = tmp_path / ".env.production"
    prod_file.write_text("MERCHANT_SYNC_HOURS=3\nUSDT_NOTIFICATION_PERCENT=0.5\n", encoding="utf-8")

    monkeypatch.setenv("ENV_FILE_PRODUCTION", str(prod_file))
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("ADMIN_SETTINGS_FILE", str(tmp_path / "overrides.json"))

    saved_hours = settings.merchant_sync_hours
    saved_percent = settings.usdt_notification_percent

    refresh_settings()

    assert settings.merchant_sync_hours == 3
    assert settings.usdt_notification_percent == 0.5


def test_env_file_paths_includes_production_when_exists(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    prod_file = tmp_path / ".env.production"
    env_file.touch()
    prod_file.touch()

    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("ENV_FILE_PRODUCTION", str(prod_file))

    paths = _env_file_paths()
    assert str(env_file) in [str(p) for p in paths]
    assert str(prod_file) in [str(p) for p in paths]
