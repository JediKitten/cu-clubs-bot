#!/usr/bin/env python3
r"""Runnable club API client; credentials come exclusively from environment/files.

    export CU_CLUBS_API_URL=https://clubs.example.org
    # Load CU_CLUBS_API_KEY securely; never commit it or pass it on the command line.
    python examples/club_api_client.py create --title 'Встреча клуба' \
        --description 'Описание встречи' --starts-at 2026-12-01T18:00:00+03:00 \
        --ends-at 2026-12-01T20:00:00+03:00 --idempotency-key create-december-meeting
    python examples/club_api_client.py publish EVENT_ID --version 1
    # Or invite EVENT_ID --club PARTNER_ID --version 1; its leader accepts in Telegram.
    python examples/club_api_client.py link --telegram-id NUMERIC_ID --link-file link.json
    # Student opens verification_uri, verifies email and explicitly agrees in Telegram.
    python examples/club_api_client.py exchange --link-file link.json --token-file user-token.txt
    # Load the file's token into CU_CLUBS_USER_TOKEN via your secret manager.
    python examples/club_api_client.py register EVENT_ID
    python examples/club_api_client.py unregister EVENT_ID
    python examples/club_api_client.py changes --cursor 0

Requires Python 3.12 and httpx (already installed in this repository). Reuse a
mutation's --idempotency-key when retrying an uncertain outcome. Do not reuse it
with different bodies or If-Match versions. Link and token files use mode 0600
and are never overwritten. CLI output omits API keys and user tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


def verify_webhook_signature(secret, raw_body, headers, *, now=None, tolerance=300):
    """Verify before JSON decoding; persist/dedupe delivery_id separately afterward.

    A valid signature authenticates the exact bytes and timestamp, not freshness
    of the event itself. Return False for timestamps more than 300 seconds from
    the receiver clock (past or future), malformed signatures or modified bytes.
    Header names are case insensitive. Keep server clocks synchronized.
    """
    if not secret or not isinstance(raw_body, bytes) or tolerance < 0:
        return False
    try:
        normalized = {key.lower(): value for key, value in headers.items()}
        timestamp = normalized.get("x-cu-clubs-timestamp", "")
        signature = normalized.get("x-cu-clubs-signature", "")
        if not timestamp.isascii() or not timestamp.isdecimal():
            return False
        if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
            return False
        message = timestamp.encode("ascii") + b"." + raw_body
        expected = "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


class ClubApiClient:
    def __init__(self, base_url, api_key, user_token=None, *, transport=None):
        if not base_url or not api_key:
            raise ValueError("Set CU_CLUBS_API_URL and CU_CLUBS_API_KEY")
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/api/v1"):
            base_url += "/api/v1"
        self.user_token = user_token
        self.http = httpx.Client(
            base_url=base_url + "/",
            headers={"Authorization": "Bearer " + api_key},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def request(
        self,
        method,
        path,
        *,
        body=None,
        version=None,
        user=False,
        idempotency_key=None,
        params=None,
    ):
        headers = {}
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            headers["Idempotency-Key"] = idempotency_key or uuid4().hex
        if version is not None:
            headers["If-Match"] = f'"{int(version)}"'
        if user:
            if not self.user_token:
                raise ValueError("Set CU_CLUBS_USER_TOKEN after explicit student consent")
            headers["X-User-Token"] = self.user_token
        response = self.http.request(
            method, path.lstrip("/"), json=body, headers=headers, params=params
        )
        response.raise_for_status()
        return response.json()

    def create(self, fields, *, idempotency_key=None):
        return self.request("POST", "events", body=fields, idempotency_key=idempotency_key)

    def publish(self, event_id, version, *, idempotency_key=None):
        return self.request(
            "POST",
            f"events/{event_id}/publish",
            body={},
            version=version,
            idempotency_key=idempotency_key,
        )

    def invite(self, event_id, club_id, version, *, idempotency_key=None):
        return self.request(
            "POST",
            f"events/{event_id}/invite",
            body={"club_id": club_id},
            version=version,
            idempotency_key=idempotency_key,
        )

    def link(self, telegram_id, *, idempotency_key=None):
        return self.request(
            "POST", "links", body={"telegram_id": telegram_id}, idempotency_key=idempotency_key
        )

    def exchange(self, link_id, code, *, idempotency_key=None):
        result = self.request(
            "POST",
            f"links/{link_id}/exchange",
            body={"exchange_code": code},
            idempotency_key=idempotency_key,
        )
        self.user_token = result["user_token"]
        return result

    def register(self, event_id, *, idempotency_key=None):
        return self.request(
            "PUT",
            f"events/{event_id}/registrations/me",
            body={},
            user=True,
            idempotency_key=idempotency_key,
        )

    def unregister(self, event_id, *, idempotency_key=None):
        return self.request(
            "DELETE",
            f"events/{event_id}/registrations/me",
            user=True,
            idempotency_key=idempotency_key,
        )

    def changes(self, cursor=0, limit=100):
        return self.request("GET", "changes", params={"cursor": cursor, "limit": limit})


def private_file(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(value)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a draft in the key's club")
    for name in ("title", "description", "starts-at", "ends-at"):
        create.add_argument("--" + name, required=True)
    create.add_argument("--capacity", type=int)
    for name in ("publish", "invite", "register", "unregister"):
        command = commands.add_parser(name)
        command.add_argument("event_id")
        if name in {"publish", "invite"}:
            command.add_argument("--version", type=int, required=True)
        if name == "invite":
            command.add_argument("--club", required=True)
    link = commands.add_parser("link")
    link.add_argument("--telegram-id", required=True, type=int)
    link.add_argument("--link-file", required=True, type=Path)
    exchange = commands.add_parser("exchange")
    exchange.add_argument("--link-file", required=True, type=Path)
    exchange.add_argument("--token-file", required=True, type=Path)
    changes = commands.add_parser("changes")
    changes.add_argument("--cursor", default=0, type=int)
    for name, command in commands.choices.items():
        if name != "changes":
            command.add_argument(
                "--idempotency-key", help="Reuse unchanged after an uncertain response"
            )
    args = parser.parse_args(argv)
    try:
        with ClubApiClient(
            os.environ.get("CU_CLUBS_API_URL"),
            os.environ.get("CU_CLUBS_API_KEY"),
            os.environ.get("CU_CLUBS_USER_TOKEN"),
        ) as api:
            key = getattr(args, "idempotency_key", None)
            if args.command == "create":
                fields = {
                    name: getattr(args, name)
                    for name in ("title", "description", "starts_at", "ends_at")
                }
                if args.capacity is not None:
                    fields["capacity"] = args.capacity
                result = api.create(fields, idempotency_key=key)
            elif args.command == "publish":
                result = api.publish(args.event_id, args.version, idempotency_key=key)
            elif args.command == "invite":
                result = api.invite(args.event_id, args.club, args.version, idempotency_key=key)
            elif args.command == "link":
                result = api.link(args.telegram_id, idempotency_key=key)
                private_file(args.link_file, json.dumps(result))
                result = {name: value for name, value in result.items() if name != "exchange_code"}
            elif args.command == "exchange":
                link_data = json.loads(args.link_file.read_text())
                if args.token_file.exists():
                    raise FileExistsError("Token file already exists")
                result = api.exchange(
                    link_data["id"],
                    link_data["exchange_code"],
                    idempotency_key=key or "exchange-" + link_data["id"],
                )
                private_file(args.token_file, result["user_token"] + "\n")
                result = {"token_file": str(args.token_file), "expires_at": result["expires_at"]}
            elif args.command in {"register", "unregister"}:
                result = getattr(api, args.command)(args.event_id, idempotency_key=key)
            else:
                result = api.changes(args.cursor)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except httpx.HTTPStatusError as error:
        # Print API's stable error, never authentication headers or request bodies.
        try:
            failure = error.response.json().get("error", {})
        except ValueError:
            failure = {}
        parser.exit(
            1,
            f"API {error.response.status_code}: {failure.get('code', 'HTTP_ERROR')} "
            f"{failure.get('message', '')}\n",
        )
    except (httpx.TransportError, ValueError, OSError, KeyError) as error:
        parser.exit(
            1, f"Client failed: {type(error).__name__}. Check configuration and private files.\n"
        )


if __name__ == "__main__":
    main()
