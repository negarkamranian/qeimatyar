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
