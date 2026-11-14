"""Opaque credentials, purpose-separated encryption and pinned public HTTPS targets."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class ApiError(Exception):
    def __init__(self, status, code, message, *, field=None):
        self.status, self.code, self.message, self.field = status, code, message, field
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _cipher(secret):
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not raw or len(raw) < 32:
        raise ApiError(503, "INTEGRATION_UNAVAILABLE", "Integration encryption is not configured")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"cu-clubs/api/v1").derive(
        raw.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value, secret):
    return (
        _cipher(secret)
        .encrypt(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        .decode()
    )


def decrypt(value, secret):
    return json.loads(_cipher(secret).decrypt(value.encode()))


async def public_target(url):
    try:
        if (
            not isinstance(url, str)
            or len(url.encode()) > 2000
            or any(ord(c) < 33 or ord(c) == 127 for c in url)
        ):
            raise ValueError
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or parts.port not in (None, 443)
        ):
            raise ValueError
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
        addresses = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM),
            timeout=10,
        )
        ips = sorted({item[4][0] for item in addresses})
        if not ips or any(not ipaddress.ip_address(ip).is_global or "%" in ip for ip in ips):
            raise ValueError
    except OSError:
        raise ApiError(
            503, "WEBHOOK_DNS_UNAVAILABLE", "Cannot resolve webhook host; retry later"
        ) from None
    except (ValueError, UnicodeError):
        raise ApiError(
            422,
            "INVALID_WEBHOOK_URL",
            "Webhook requires a public HTTPS host on port 443",
            field="url",
        ) from None
    return hostname, ips[0]
