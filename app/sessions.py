from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

from app.config import settings

COOKIE_NAME = "qeimatyar_session"
SESSION_SECONDS = 60 * 60 * 24 * 30


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(user_id: int) -> str:
    payload = f"{user_id}:{int(time.time()) + SESSION_SECONDS}".encode()
    signature = hmac.new(settings.secret.encode(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def read_session(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode(encoded_payload)
        signature = _decode(encoded_signature)
        expected = hmac.new(settings.secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        user_id, expires_at = payload.decode().split(":", 1)
        if int(expires_at) < int(time.time()):
            return None
        return int(user_id)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
