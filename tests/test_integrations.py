"""Offline integration contracts: no real SMTP, OAuth consent or Google API calls."""

import asyncio
import base64
import hashlib
import json
import ssl
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from cu_clubs.config import Settings
from cu_clubs.integrations import google_oauth as google
from cu_clubs.integrations import mail


def smtp_settings(**overrides):
    values = dict(
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_username="sender@example.test",
        smtp_password=SecretStr("fake-password"),
        smtp_sender="sender@example.test",
        smtp_use_ssl=False,
        smtp_starttls=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSMTP:
    events = []
    failure = False
    refused = False

    def __init__(self, host, port, **kwargs):
        self.events.append(("connect", host, port, kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.events.append(("close",))

    def ehlo(self):
        self.events.append(("ehlo",))

    def starttls(self, *, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        self.events.append(("tls",))
        if self.failure:
            raise mail.smtplib.SMTPException("fake-password and 123456 and recipient")

    def login(self, username, password):
        self.events.append(("login", username, password))

    def send_message(self, message, **kwargs):
        self.events.append(("send", message, kwargs))
        return {"recipient": (550, "rejected")} if self.refused else {}


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.events = []
    FakeSMTP.failure = False
    FakeSMTP.refused = False
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_mail_starttls_before_login_and_code_delivery(smtp, capsys, caplog):
    asyncio.run(mail.send_verification_code(smtp_settings(), "student@example.test", "123456"))
    assert [event[0] for event in smtp.events] == [
        "connect",
        "ehlo",
        "tls",
        "ehlo",
        "login",
        "send",
        "close",
    ]
    message = smtp.events[-2][1]
    assert "123456" in message.get_content()
    assert message["From"] == "sender@example.test"
    assert message["To"] == "student@example.test"
    assert not capsys.readouterr().out
    assert not caplog.text


def test_mail_ssl_supplies_verified_context(smtp):
    asyncio.run(
        mail.send_test_email(
            smtp_settings(smtp_port=465, smtp_use_ssl=True, smtp_starttls=False), "a@example.test"
        )
    )
    context = smtp.events[0][3]["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert "tls" not in [event[0] for event in smtp.events]


@pytest.mark.parametrize(
    "changes",
    [
        {"smtp_password": None},
        {"smtp_password": SecretStr("")},
        {"smtp_host": None},
        {"smtp_starttls": False},
        {"smtp_use_ssl": True},
    ],
)
def test_mail_unconfigured_or_insecure_fails_before_connect(smtp, changes):
    with pytest.raises(mail.MailDeliveryError):
        asyncio.run(mail.send_test_email(smtp_settings(**changes), "a@example.test"))
    assert smtp.events == []


def test_mail_tls_failure_never_authenticates_or_leaks(smtp, capsys, caplog):
    smtp.failure = True
    with pytest.raises(mail.MailDeliveryError) as error:
        asyncio.run(mail.send_verification_code(smtp_settings(), "a@example.test", "123456"))
    assert "login" not in [event[0] for event in smtp.events]
    assert "send" not in [event[0] for event in smtp.events]
    assert "fake-password" not in str(error.value)
    assert "123456" not in str(error.value)
    assert not capsys.readouterr().out and not caplog.text


def test_mail_refused_recipient_is_failure(smtp):
    smtp.refused = True
    with pytest.raises(mail.MailDeliveryError):
        asyncio.run(mail.send_test_email(smtp_settings(), "a@example.test"))


@pytest.mark.parametrize("address", ["a@example.test\r\nBcc: x@example.test", "invalid", "a@b,x@y"])
def test_mail_rejects_bad_recipient(smtp, address):
    with pytest.raises(mail.MailDeliveryError):
        asyncio.run(mail.send_test_email(smtp_settings(), address))
    assert smtp.events == []


def credentials(tmp_path):
    path = tmp_path / "token.json"
    google._save_credentials(
        path,
        {
            "type": "authorized_user",
            "client_id": "fake-client",
            "client_secret": "fake-secret",
            "refresh_token": "fake-refresh",
            "token_uri": google.TOKEN_URL,
            "scopes": [google.SCOPE],
        },
    )
    return path


def install_http(monkeypatch, handler):
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        google.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_oauth_save_outside_git_and_private_mode(tmp_path):
    path = credentials(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(google.GoogleSetupError, match="outside"):
        google._save_credentials(repo / "ignored" / "token.json", {"secret": "fake"})
    assert not (repo / "ignored").exists()


def test_oauth_client_rejects_web_and_non_google_endpoints(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"web": {"client_id": "fake"}}))
    with pytest.raises(google.GoogleSetupError, match="Desktop"):
        google._client(path)
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake",
                    "client_secret": "fake",
                    "auth_uri": google.AUTH_URL,
                    "token_uri": "https://attacker.example/token",
                }
            }
        )
    )
    with pytest.raises(google.GoogleSetupError, match="official"):
        google._client(path)


@pytest.mark.parametrize("auth_uri", [google.AUTH_URL, "https://accounts.google.com/o/oauth2/auth"])
def test_oauth_accepts_official_desktop_client_downloads(tmp_path, auth_uri):
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "synthetic-desktop-client",
                    "client_secret": "synthetic-secret",
                    "auth_uri": auth_uri,
                    "token_uri": google.TOKEN_URL,
                }
            }
        )
    )
    assert google._client(path)["client_id"] == "synthetic-desktop-client"


def test_pkce_s256_known_transform():
    verifier = "a" * 64
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert google._pkce(verifier) == expected.decode()


def test_refresh_and_metadata_request_are_minimal_and_no_secrets_printed(
    tmp_path, monkeypatch, capsys
):
    path = credentials(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url == google.TOKEN_URL:
            assert b"grant_type=refresh_token" in request.content
            assert b"fake-refresh" in request.content
            return httpx.Response(200, json={"access_token": "fake-access", "token_type": "Bearer"})
        assert request.headers["Authorization"] == "Bearer fake-access"
        assert str(request.url).startswith("https://sheets.googleapis.com/v4/spreadsheets/test-id?")
        assert "fake-access" not in str(request.url)
        assert "includeGridData" not in request.url.params
        return httpx.Response(200, json={"spreadsheetId": "test-id", "sheets": []})

    install_http(monkeypatch, handler)
    settings = Settings(
        _env_file=None, google_credentials_path=path, google_spreadsheet_id="test-id"
    )
    assert asyncio.run(google.check_google_access(settings))["spreadsheetId"] == "test-id"
    assert len(requests) == 2
    assert "fake-access" not in path.read_text()
    assert not capsys.readouterr().out


def test_refresh_error_body_never_leaks(tmp_path, monkeypatch):
    path = credentials(tmp_path)
    install_http(monkeypatch, lambda _request: httpx.Response(400, json={"error": "fake-refresh"}))
    with pytest.raises(google.GoogleSetupError) as error:
        asyncio.run(google.refresh_access_token(path))
    assert "fake-refresh" not in str(error.value)


def test_oauth_authorize_requires_offline_refresh_and_saves_only_needed_tokens(
    tmp_path, monkeypatch
):
    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake-client",
                    "client_secret": "fake-secret",
                    "auth_uri": google.AUTH_URL,
                    "token_uri": google.TOKEN_URL,
                }
            }
        )
    )
    monkeypatch.setattr(
        google,
        "_receive_code",
        lambda *args, **kwargs: (
            "fake-code",
            "http://127.0.0.1:1234/oauth2callback",
            "fake-verifier",
        ),
    )

    def handler(request):
        values = parse_qs(request.content.decode())
        assert values["code_verifier"] == ["fake-verifier"]
        assert values["redirect_uri"] == ["http://127.0.0.1:1234/oauth2callback"]
        return httpx.Response(
            200, json={"access_token": "fake-access", "refresh_token": "fake-refresh"}
        )

    install_http(monkeypatch, handler)
    path = tmp_path / "token.json"
    asyncio.run(google.authorize(client, path))
    data = json.loads(path.read_text())
    assert data["refresh_token"] == "fake-refresh"
    assert data["scopes"] == [google.SCOPE]
    assert "fake-access" not in path.read_text()


def test_loopback_pkce_state_rejection_and_single_use(monkeypatch):
    opened = {}
    events = []

    class FakeServer:
        server_port = 1234

        def __init__(self, address, callback):
            assert address == ("127.0.0.1", 0)
            self.callback = callback
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("closed")

        def handle_request(self):
            self.calls += 1
            state = "incorrect" if self.calls == 1 else opened["query"]["state"][0]
            callback = object.__new__(self.callback)
            callback.path = "/oauth2callback?" + google.urlencode(
                {"state": state, "code": "fake-code"}
            )
            callback.send_error = lambda *args: events.append("rejected")
            callback.send_response = lambda *args: events.append("accepted")
            callback.send_header = lambda *args: None
            callback.end_headers = lambda: None
            callback.wfile = SimpleNamespace(write=lambda *args: None)
            callback.do_GET()

    def open_browser(url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "accounts.google.com"
        opened["query"] = parse_qs(parsed.query)
        return True

    monkeypatch.setattr(google, "HTTPServer", FakeServer)
    monkeypatch.setattr(google.webbrowser, "open", open_browser)
    code, redirect, verifier = google._receive_code("fake-client")
    query = opened["query"]
    assert code == "fake-code" and redirect == "http://127.0.0.1:1234/oauth2callback"
    assert query["scope"] == [google.SCOPE]
    assert query["code_challenge"] == [google._pkce(verifier)]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert events == ["rejected", "accepted", "closed"]


def test_google_missing_configuration_fails_without_network(monkeypatch):
    monkeypatch.setattr(
        google.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Network attempted")
    )
    with pytest.raises(google.GoogleSetupError, match="Configure"):
        asyncio.run(google.check_google_access(Settings(_env_file=None)))


def test_loopback_timeout_closes_listener(monkeypatch):
    ticks = iter([0, 181])
    closed = []

    class IdleServer:
        server_port = 1234

        def __init__(self, address, callback):
            assert address == ("127.0.0.1", 0)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)

    monkeypatch.setattr(google, "HTTPServer", IdleServer)
    monkeypatch.setattr(google.webbrowser, "open", lambda *args, **kwargs: True)
    monkeypatch.setattr(google.time, "monotonic", lambda: next(ticks))
    with pytest.raises(google.GoogleSetupError, match="timed out"):
        google._receive_code("fake-client")
    assert closed == [True]


def test_authorize_without_refresh_token_never_overwrites_existing_file(tmp_path, monkeypatch):
    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake",
                    "client_secret": "fake",
                    "auth_uri": google.AUTH_URL,
                    "token_uri": google.TOKEN_URL,
                }
            }
        )
    )
    path = credentials(tmp_path)
    previous = path.read_bytes()
    monkeypatch.setattr(
        google,
        "_receive_code",
        lambda *args, **kwargs: (
            "fake-code",
            "http://127.0.0.1:1234/oauth2callback",
            "fake-verifier",
        ),
    )
    install_http(monkeypatch, lambda request: httpx.Response(200, json={"access_token": "fake"}))
    with pytest.raises(google.GoogleSetupError, match="refresh token"):
        asyncio.run(google.authorize(client, path))
    assert path.read_bytes() == previous
