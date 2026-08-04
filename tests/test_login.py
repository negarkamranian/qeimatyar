from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import settings
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


def test_basalam_renewal_redirect_requests_sales_history_scope(monkeypatch):
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
    assert "vendor.parcel.read" in query["scope"][0].split()
