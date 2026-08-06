from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.basalam import decrypt_token, encrypt_token
from app.config import settings
from app.db import connection, init_db, now_iso
from app.main import app


def test_seller_login_shows_basalam_and_digikala_connection(monkeypatch):
    monkeypatch.setattr(settings, "client_id", "")
    monkeypatch.setattr(settings, "client_secret", "")

    with TestClient(app) as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert "باسلام" in response.text
    assert 'src="/static/basalam-logo.png"' in response.text
    assert "دیجی‌کالا" in response.text
    assert 'action="/auth/digikala/source"' in response.text
    assert 'name="seller_link"' in response.text
    assert 'name="digikala_token"' in response.text
    assert 'href="https://seller.digikala.com/pwa/"' not in response.text


def test_basalam_connect_route_is_registered(monkeypatch):
    monkeypatch.setattr(settings, "client_id", "")
    monkeypatch.setattr(settings, "client_secret", "")

    with TestClient(app) as client:
        response = client.get("/auth/basalam")

    assert response.status_code == 503


def test_basalam_renewal_redirect_keeps_configured_scopes(monkeypatch):
    monkeypatch.setattr(settings, "client_id", "client-id")
    monkeypatch.setattr(settings, "client_secret", "client-secret")
    monkeypatch.setattr(
        settings,
        "scopes",
        "customer.profile.read vendor.profile.read vendor.product.read",
    )
    monkeypatch.setattr(
        settings,
        "redirect_uri",
        "https://qeimatyar.ir/auth/basalam/callback",
    )

    with TestClient(app) as client:
        response = client.get("/auth/basalam?renew=analytics", follow_redirects=False)

    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["scope"][0].split() == [
        "customer.profile.read", "vendor.profile.read", "vendor.product.read",
        "vendor.parcel.read",
    ]
    assert response.cookies.get("oauth_intent") == "analytics"


def test_regular_login_cannot_downgrade_a_working_analytics_grant(
    tmp_path, monkeypatch
):
    user_id = 771122
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "oauth.db"))
    monkeypatch.setattr(
        settings,
        "scopes",
        "customer.profile.read vendor.profile.read vendor.product.read",
    )
    init_db()
    with connection() as db:
        db.execute(
            """INSERT INTO accounts
            (user_id,vendor_id,vendor_title,user_name,access_token,refresh_token,
             connected_at,sync_status,analytics_status,analytics_consent_at,
             oauth_scopes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                881122,
                "غرفه تست دسترسی",
                "کاربر تست",
                encrypt_token("analytics-access-token"),
                encrypt_token("analytics-refresh-token"),
                now_iso(),
                "idle",
                "ready",
                now_iso(),
                "customer.profile.read vendor.profile.read vendor.product.read vendor.parcel.read",
            ),
        )

    async def fake_exchange(code, *, trace_id=None):
        return {
            "access_token": "narrow-login-token",
            "refresh_token": "narrow-refresh-token",
            "expires_in": 3600,
            "scope": "customer.profile.read vendor.profile.read vendor.product.read",
        }

    async def fake_me(token, *, trace_id=None):
        return {
            "id": user_id,
            "name": "کاربر تست",
            "vendor": {"id": 881122, "title": "غرفه تست دسترسی"},
        }

    async def fake_sync(user_id):
        return {"ok": True}

    monkeypatch.setattr("app.main.basalam.exchange_code", fake_exchange)
    monkeypatch.setattr("app.main.basalam.me", fake_me)
    monkeypatch.setattr("app.main.merchant_sync.sync_user", fake_sync)

    with TestClient(app) as client:
        client.cookies.set("oauth_state", "login-state")
        client.cookies.set("oauth_intent", "login")
        response = client.get(
            "/auth/basalam/callback?code=login-code&state=login-state",
            follow_redirects=False,
        )

    assert response.status_code == 307
    with connection() as db:
        account = db.execute(
            """SELECT access_token,analytics_status,analytics_consent_at,
            oauth_scopes FROM accounts WHERE user_id=?""",
            (user_id,),
        ).fetchone()
    assert decrypt_token(account["access_token"]) == "analytics-access-token"
    assert account["analytics_status"] == "ready"
    assert account["analytics_consent_at"]
    assert "vendor.parcel.read" in account["oauth_scopes"].split()
