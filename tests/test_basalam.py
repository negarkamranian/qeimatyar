import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.basalam import BasalamClient


def test_authorization_url_contains_only_configured_read_scopes(monkeypatch):
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            scopes="vendor.profile.read vendor.product.read",
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
        ),
    )
    url = BasalamClient().authorization_url("secure-state")
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["vendor.profile.read vendor.product.read"]
    assert query["state"] == ["secure-state"]


def test_authorization_url_rejects_write_scope(monkeypatch):
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            scopes="vendor.profile.read vendor.product.write",
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
        ),
    )
    with pytest.raises(ValueError, match="vendor.product.write"):
        BasalamClient().authorization_url("secure-state")


def test_products_reads_all_api_pages(monkeypatch):
    client = BasalamClient()
    requested_pages = []

    async def fake_request(method, url, *, token=None, **kwargs):
        page = kwargs["params"]["page"]
        requested_pages.append(page)
        return {
            "data": [{"id": page}],
            "page": page,
            "total_page": 3,
        }

    monkeypatch.setattr(client, "_request", fake_request)
    products = asyncio.run(client.products("token", 123))
    assert requested_pages == [1, 2, 3]
    assert [product["id"] for product in products] == [1, 2, 3]
