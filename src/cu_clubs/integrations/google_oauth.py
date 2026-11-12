"""Local Desktop OAuth bootstrap and read-only Sheets connection check.

Run authorization on the owner's computer, never on a public HTTP interface.
No tokens, authorization codes, or Google error bodies are printed.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from cu_clubs.config import Settings

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleSetupError(RuntimeError):
    """An operator-facing message safe to show without leaking credentials."""


def _outside_git(path: Path) -> Path:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise GoogleSetupError("Keep OAuth credentials outside every Git working tree")
    return path


def _read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError):
        raise GoogleSetupError("Cannot read valid OAuth JSON file") from None


def _save_credentials(path: Path, data: dict) -> None:
    path = _outside_git(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    except OSError:
        raise GoogleSetupError("Cannot store OAuth credentials securely") from None


def _client(path: Path) -> dict:
    data = _read_json(_outside_git(path)).get("installed")
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key] for key in ("client_id", "client_secret")
    ):
        raise GoogleSetupError("Download an OAuth client JSON of type Desktop app")
    # Never trust token/auth endpoints supplied by an arbitrary local JSON file.
    if data.get("auth_uri") != AUTH_URL or data.get("token_uri") != TOKEN_URL:
        raise GoogleSetupError("Desktop OAuth JSON must use the official Google endpoints")
    return data


def _pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _receive_code(client_id: str, *, timeout: float = 180) -> tuple[str, str, str]:
    """One valid callback only; invalid state does not consume the pending login."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            # A local connection that never completes its headers must not pin the listener.
            self.request.settimeout(1)
            super().setup()

        def log_message(self, *_args) -> None:
            pass  # BaseHTTPRequestHandler would log the code in the request URL.

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (
                parsed.path == "/oauth2callback"
                and len(query.get("state", [])) == 1
                and secrets.compare_digest(query["state"][0], state)
            )
            if not valid:
                self.send_error(400, "Invalid OAuth callback")
                return
            if len(query.get("code", [])) == 1 and "error" not in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = "denied"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Authorization received. You may close this tab.")

    if not 0 < timeout <= 600:
        raise GoogleSetupError("OAuth timeout must be between 0 and 600 seconds")
    try:
        with HTTPServer(("127.0.0.1", 0), Callback) as server:
            redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
            url = (
                AUTH_URL
                + "?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": SCOPE,
                        "state": state,
                        "code_challenge": _pkce(verifier),
                        "code_challenge_method": "S256",
                        "access_type": "offline",
                        "prompt": "consent",
                    }
                )
            )
            if not webbrowser.open(url, new=1):
                raise GoogleSetupError("Cannot open browser; run locally with a default browser")
            deadline = time.monotonic() + timeout
            while not result and time.monotonic() < deadline:
                server.timeout = min(1, max(0.01, deadline - time.monotonic()))
                server.handle_request()
    except OSError:
        raise GoogleSetupError("Cannot open the local OAuth callback listener") from None
    if "code" not in result:
        raise GoogleSetupError("Google authorization denied or timed out; rerun authorize")
    return result["code"], redirect_uri, verifier


async def _token_request(data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            raise GoogleSetupError("Google token request failed; reauthorize if access was revoked")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"]
        ):
            raise ValueError
        if payload.get("token_type", "Bearer").lower() != "bearer":
            raise ValueError
        if "scope" in payload and SCOPE not in payload["scope"].split():
            raise GoogleSetupError("Google did not grant the required Sheets scope")
        return payload
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise GoogleSetupError("Google token request failed; no credentials were printed") from None


async def authorize(client_file: Path, credentials_file: Path, *, timeout: float = 180) -> None:
    """Interactive explicit action; never called by Settings or the normal worker."""
    client = _client(client_file)
    credentials_file = _outside_git(credentials_file)
    code, redirect_uri, verifier = await asyncio.to_thread(
        _receive_code, client["client_id"], timeout=timeout
    )
    payload = await _token_request(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    )
    if not isinstance(payload.get("refresh_token"), str) or not payload["refresh_token"]:
        raise GoogleSetupError("No offline refresh token received; rerun and grant consent")
    _save_credentials(
        credentials_file,
        {
            "type": "authorized_user",
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "token_uri": TOKEN_URL,
            "refresh_token": payload["refresh_token"],
            "scopes": [SCOPE],
        },
    )


async def refresh_access_token(credentials_file: Path) -> str:
    """Refresh without persisting a short-lived access token."""
    path = _outside_git(credentials_file)
    data = _read_json(path)
    if data.get("type") != "authorized_user" or data.get("token_uri") != TOKEN_URL:
        raise GoogleSetupError("Expected authorized-user credentials from authorize")
    if not isinstance(data.get("scopes"), list) or SCOPE not in data["scopes"]:
        raise GoogleSetupError("Credentials lack the required Sheets scope; rerun authorize")
    if not all(
        isinstance(data.get(key), str) and data[key]
        for key in ("client_id", "client_secret", "refresh_token")
    ):
        raise GoogleSetupError("OAuth credentials are incomplete; rerun authorize")
    try:
        path.chmod(0o600)
    except OSError:
        raise GoogleSetupError("Cannot restrict OAuth credentials permissions") from None
    payload = await _token_request(
        {
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
            "refresh_token": data["refresh_token"],
            "grant_type": "refresh_token",
        }
    )
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
        data["refresh_token"] = payload["refresh_token"]
        _save_credentials(path, data)
    return payload["access_token"]


async def check_google_access(settings: Settings) -> dict:
    """Read spreadsheet metadata only; this does not prove write permissions/template safety."""
    if settings.google_auth_mode != "oauth":
        raise GoogleSetupError("This setup helper supports personal-account OAuth only")
    if not settings.google_credentials_path or not settings.google_spreadsheet_id:
        raise GoogleSetupError("Configure Google credentials path and test spreadsheet ID first")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", settings.google_spreadsheet_id):
        raise GoogleSetupError("Invalid Google spreadsheet ID")
    token = await refresh_access_token(settings.google_credentials_path)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                "https://sheets.googleapis.com/v4/spreadsheets/" + settings.google_spreadsheet_id,
                params={
                    "fields": "spreadsheetId,properties(title),sheets(properties(title,sheetId))"
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            raise GoogleSetupError("Cannot read test spreadsheet; check Sheets API and file access")
        metadata = response.json()
        if not isinstance(metadata, dict) or "spreadsheetId" not in metadata:
            raise ValueError
        return metadata
    except (httpx.HTTPError, ValueError):
        raise GoogleSetupError("Google Sheets connection check failed") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Local personal-account Google Sheets setup")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("authorize", help="Open Google consent locally")
    auth.add_argument("--client-file", type=Path, required=True)
    auth.add_argument("--credentials-file", type=Path, required=True)
    auth.add_argument("--timeout", type=float, default=180)
    commands.add_parser("check", help="Refresh credentials and read test spreadsheet metadata")
    args = parser.parse_args()
    try:
        if args.command == "authorize":
            asyncio.run(authorize(args.client_file, args.credentials_file, timeout=args.timeout))
            print("OAuth credentials saved securely; tokens were not printed.")
        else:
            metadata = asyncio.run(check_google_access(Settings()))
            print(f"Google Sheets metadata available; {len(metadata.get('sheets', []))} tabs.")
    except GoogleSetupError as exc:
        parser.exit(1, f"Setup failed: {exc}\n")


if __name__ == "__main__":
    main()
