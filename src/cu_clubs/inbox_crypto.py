"""Authenticated encryption for durable Telegram input, including email codes."""

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _cipher(secret) -> Fernet:
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not raw or len(raw) < 32:
        raise ValueError("Inbox encryption key is unavailable")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"cu-clubs/inbox/v1").derive(
        raw.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def seal_update(payload: dict, secret) -> dict:
    if not secret:
        return payload
    ciphertext = _cipher(secret).encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return {"version": 1, "update_id": payload["update_id"], "sealed_update": ciphertext}


def open_update(payload: dict, secret) -> dict:
    if "sealed_update" not in payload:
        return payload  # Existing updates and explicitly offline test environments.
    try:
        result = json.loads(_cipher(secret).decrypt(payload["sealed_update"].encode()))
        if payload.get("version") != 1 or result["update_id"] != payload["update_id"]:
            raise ValueError
        return result
    except (InvalidToken, ValueError, TypeError, KeyError):
        raise ValueError("Stored Telegram input could not be authenticated") from None
