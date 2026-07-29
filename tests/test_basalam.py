import asyncio
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.basalam import BasalamClient, BasalamError


def test_authorization_url_contains_only_configured_read_scopes(monkeypatch):
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            scopes="customer.profile.read vendor.profile.read vendor.product.read",
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
        ),
    )
    url = BasalamClient().authorization_url("secure-state")
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [
        "customer.profile.read vendor.profile.read vendor.product.read"
    ]
    assert query["state"] == ["secure-state"]


def test_authorization_url_rejects_write_scope(monkeypatch):
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            scopes=(
                "customer.profile.read vendor.profile.read "
                "vendor.product.read vendor.product.write"
            ),
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
        ),
    )
    with pytest.raises(ValueError, match="vendor.product.write"):
        BasalamClient().authorization_url("secure-state")


def test_authorization_url_rejects_missing_customer_profile_scope(monkeypatch):
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            scopes="vendor.profile.read vendor.product.read",
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
        ),
    )
    with pytest.raises(ValueError, match="customer.profile.read"):
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


def test_oauth_provider_error_is_traced_without_secrets(monkeypatch):
    code = "one-time-authorization-code"
    client_secret = "super-secret-client-secret"
    client = BasalamClient()
    monkeypatch.setattr(
        "app.basalam.settings",
        SimpleNamespace(
            client_id="client-id",
            client_secret=client_secret,
            redirect_uri="https://qeimatyar.ir/auth/basalam/callback",
            marketplace_trust_env=False,
        ),
    )

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(
                401,
                request=request,
                headers={"x-request-id": "provider-request-123"},
                json={
                    "error": "invalid_grant",
                    "error_description": f"bad code {code}",
                    "client_secret": client_secret,
                },
            )

    monkeypatch.setattr("app.basalam.httpx.AsyncClient", FakeAsyncClient)
    messages = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = ListHandler()
    oauth_logger = logging.getLogger("app.basalam")
    oauth_logger.addHandler(handler)

    try:
        with pytest.raises(BasalamError) as caught:
            asyncio.run(client.exchange_code(code, trace_id="trace-test-123"))
    finally:
        oauth_logger.removeHandler(handler)

    assert caught.value.status_code == 401
    assert caught.value.error_kind == "http_status"
    records = " ".join(messages)
    assert "trace-test-123" in records
    assert "provider-request-123" in records
    assert "invalid_grant" in records
    assert code not in records
    assert client_secret not in records
