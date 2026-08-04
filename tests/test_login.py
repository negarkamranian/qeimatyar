from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_seller_login_shows_basalam_and_official_digikala_panel(monkeypatch):
    monkeypatch.setattr(settings, "client_id", "")
    monkeypatch.setattr(settings, "client_secret", "")

    with TestClient(app) as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert "باسلام" in response.text
    assert "دیجی‌کالا" in response.text
    assert 'href="https://seller.digikala.com/pwa/"' in response.text
    assert 'rel="noopener noreferrer"' in response.text


def test_basalam_connect_route_is_registered(monkeypatch):
    monkeypatch.setattr(settings, "client_id", "")
    monkeypatch.setattr(settings, "client_secret", "")

    with TestClient(app) as client:
        response = client.get("/auth/basalam")

    assert response.status_code == 503
