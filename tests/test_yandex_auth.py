"""Single-use browser proof never binds another Telegram account or trusts input email."""

import asyncio
import base64
import hashlib
import logging
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from pydantic import ValidationError as ConfigError
from test_identity import NOW, OTHER, SECRET, STUDENT, seed
from test_identity import sessions as sessions

from cu_clubs.access import can_register
from cu_clubs.app import create_app
from cu_clubs.config import Settings
from cu_clubs.identity_models import YandexChallenge, YandexIdentity
from cu_clubs.logging_privacy import OAuthQueryFilter
from cu_clubs.models import Conversation, Person, TelegramAccount
from cu_clubs.services import ValidationError
from cu_clubs.yandex_auth import YandexAuth, exchange, signed, ticket
from cu_clubs.yandex_ui import yandex_gate


def settings(**values):
    return Settings(
        _env_file=None,
        **{
            "yandex_auth_enabled": True,
            "yandex_client_id": "test-client",
            "yandex_client_secret": SecretStr("offline-test-client-secret"),
            "verification_secret": SecretStr(SECRET),
            "yandex_redirect_uri": "https://clubs-api.example/auth/yandex/callback",
            "telegram_bot_username": "offline_bot",
            **values,
        },
    )


def profile(**patch):
    return {
        "id": "123456",
        "client_id": "test-client",
        "default_email": "i.ivanov@edu.centraluniversity.ru",
        **patch,
    }


async def candidate(factory, actor=STUDENT, *, config=None, provider=None):
    config = config or settings()
    async with factory.begin() as session:
        row = await YandexAuth(session, config, now=NOW).begin(actor, "Иван Иванов", "student")
        key = row.id
        url, cookie = await YandexAuth(session, config, now=NOW).start_browser(ticket(config, key))
        state = parse_qs(urlsplit(url).query)["state"][0]
    async with factory.begin() as session:
        await YandexAuth(session, config, now=NOW).claim_callback(state, cookie)
    async with factory.begin() as session:
        ok = await YandexAuth(session, config, now=NOW).record_result(key, provider or profile())
    return key, state, cookie, ok


def test_config_disabled_by_default_and_requires_complete_https_settings():
    assert not Settings(_env_file=None).yandex_auth_enabled
    for patch in [
        {"yandex_client_secret": None},
        {"verification_secret": None},
        {"yandex_redirect_uri": "http://example/auth/yandex/callback"},
        {"yandex_redirect_uri": "https://example/auth/yandex/callback?open=evil"},
    ]:
        with pytest.raises(ConfigError):
            settings(**patch)


def test_provider_proof_requires_original_telegram_confirmation(sessions):
    async def scenario():
        await seed(sessions)
        key, state, cookie, ok = await candidate(sessions)
        assert ok
        async with sessions.begin() as session:
            account = await session.get(TelegramAccount, STUDENT)
            assert account.person.verified_at is None and account.person.time_login is None
            session.info["yandex_auth_enabled"] = True
            assert not can_register(session, account.person)
            with pytest.raises(ValidationError):
                await YandexAuth(session, settings(), now=NOW).confirm(OTHER, key)
            with pytest.raises(ValidationError):
                await YandexAuth(session, settings(), now=NOW).claim_callback(state, cookie)
        async with sessions.begin() as session:
            await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, key)
        async with sessions.begin() as session:
            account = await session.get(TelegramAccount, STUDENT)
            session.info["yandex_auth_enabled"] = True
            assert can_register(session, account.person)
            assert account.person.full_name == "Иван Иванов"
            assert account.person.time_login == "i.ivanov"
            assert account.person.verified_at == NOW
            assert (await session.get(YandexIdentity, "123456")).person_id == account.person_id
            with pytest.raises(ValidationError):
                await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, key)
        # Another numeric Telegram ID cannot claim the same provider identity.
        second, *_ = await candidate(sessions, OTHER)
        async with sessions.begin() as session:
            with pytest.raises(ValidationError):
                await YandexAuth(session, settings(), now=NOW).confirm(OTHER, second)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "patch",
    [
        {"default_email": "i.ivanov@yandex.ru"},
        {"default_email": "i@edu.centraluniversity.ru.evil.org"},
        {"default_email": "i@@edu.centraluniversity.ru"},
        {"default_email": None, "emails": ["i@edu.centraluniversity.ru"]},
        {"client_id": "another-app"},
        {"id": None},
    ],
)
def test_university_domain_is_checked_on_provider_default_email(sessions, patch):
    async def scenario():
        await seed(sessions)
        key, *_, ok = await candidate(sessions, provider=profile(**patch))
        assert not ok
        async with sessions.begin() as session:
            assert (await session.get(YandexChallenge, key)).status == "failed"
            with pytest.raises(ValidationError):
                await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, key)

    asyncio.run(scenario())


def test_state_cookie_expiry_cancellation_and_pkce(sessions):
    async def scenario():
        await seed(sessions)
        config = settings()
        async with sessions.begin() as session:
            auth = YandexAuth(session, config, now=NOW)
            row = await auth.begin(STUDENT, "Иван Иванов", "staff")
            key = row.id
            url, cookie = await auth.start_browser(ticket(config, key))
            query = parse_qs(urlsplit(url).query)
            assert query["scope"] == ["login:email"] and query["force_confirm"] == ["yes"]
            assert query["code_challenge_method"] == ["S256"]
            verifier = signed(config, "pkce", key.hex)
            expected = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            assert query["code_challenge"] == [expected]
            state = query["state"][0]
            assert cookie not in str(row.__dict__) and verifier not in str(row.__dict__)
            for bad_state, bad_cookie in [(state, "wrong-browser"), (state + "x", cookie)]:
                with pytest.raises(ValidationError):
                    await auth.claim_callback(bad_state, bad_cookie)
        async with sessions.begin() as session:
            with pytest.raises(ValidationError):
                await YandexAuth(session, config, now=NOW + timedelta(minutes=11)).claim_callback(
                    state, cookie
                )
            replacement = await YandexAuth(session, config, now=NOW).begin(
                STUDENT, "Иван Иванов", "staff"
            )
            assert replacement.id != key
            assert (await session.get(YandexChallenge, key)).status == "cancelled"
            with pytest.raises(ValidationError):
                await YandexAuth(session, config, now=NOW).claim_callback(state, cookie)

    asyncio.run(scenario())


def test_provider_requests_use_basic_auth_pkce_and_header_token_only():
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/token":
            assert "code_verifier=" in request.content.decode()
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200, json={"access_token": "offline-access", "scope": "login:email"}
            )
        assert request.headers["authorization"] == "OAuth offline-access"
        assert "oauth_token" not in str(request.url)
        return httpx.Response(200, json=profile())

    async def scenario():
        from uuid import uuid4

        result = await exchange(
            settings(), uuid4(), "offline-code", transport=httpx.MockTransport(respond)
        )
        assert result == profile() and len(seen) == 2

    asyncio.run(scenario())


def test_callback_is_no_store_browser_bound_and_does_not_verify_until_bot(sessions, monkeypatch):
    async def scenario():
        await seed(sessions)
        config = settings()
        async with sessions.begin() as session:
            row = await YandexAuth(session, config).begin(STUDENT, "Иван Иванов", "student")
            raw = ticket(config, row.id)
        app = create_app(config)
        app.state.sessions = sessions
        calls = []

        async def fake_exchange(*args):
            calls.append(True)
            return profile()

        monkeypatch.setattr("cu_clubs.yandex_routes.exchange", fake_exchange)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://clubs-api.example"
        ) as client:
            start = await client.get("/auth/yandex/start", params={"ticket": raw})
            assert start.status_code == 303
            assert (
                "Secure" in start.headers["set-cookie"]
                and "HttpOnly" in start.headers["set-cookie"]
            )
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            wrong = await client.get(
                "/auth/yandex/callback", params={"state": "bad", "code": "secret"}
            )
            assert wrong.status_code == 400 and not calls
            # Wrong-state response clears the cookie, so resume using the original browser cookie.
            browser_cookie = start.cookies.get("cu_clubs_yandex")
            client.cookies.set("cu_clubs_yandex", browser_cookie)
            result = await client.get(
                "/auth/yandex/callback", params={"state": state, "code": "secret"}
            )
            assert result.status_code == 200 and calls == [True]
            assert result.headers["cache-control"] == "no-store"
            assert result.headers["referrer-policy"] == "no-referrer"
            assert "secret" not in result.text and "i.ivanov" not in result.text
        async with sessions.begin() as session:
            assert (await session.get(TelegramAccount, STUDENT)).person.verified_at is None

    asyncio.run(scenario())


def test_imported_identity_is_preserved_after_real_email_proof(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            imported = Person(full_name="Импорт Иванов", kind="student", time_login="i.ivanov")
            session.add(imported)
            await session.flush()
            canonical = imported.id
        key, *_ = await candidate(sessions)
        async with sessions.begin() as session:
            await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, key)
        async with sessions.begin() as session:
            account = await session.get(TelegramAccount, STUDENT)
            assert account.person_id == canonical

    asyncio.run(scenario())


def test_ui_retains_name_back_and_cannot_confirm_before_provider(sessions):
    async def scenario():
        await seed(sessions)
        config = settings()
        async with sessions.begin() as session:
            state = Conversation(telegram_id=STUDENT, state="idle", payload={})
            session.add(state)
            person = {"verified_at": None}
            result = await yandex_gate(session, STUDENT, state, person, [], "profile", config)
            assert "Яндексе" in result.screen.text
            data = result.screen.markup.inline_keyboard[0][0].callback_data
            await yandex_gate(session, STUDENT, state, person, [], data, config)
            result = await yandex_gate(
                session, STUDENT, state, person, [], "", config, SimpleNamespace(text="Иван Иванов")
            )
            assert state.state == "yandex_link" and result.screen.markup.inline_keyboard[0][0].url
            nonce = state.payload["nonce"]
            with pytest.raises(ValidationError):
                await yandex_gate(
                    session, STUDENT, state, person, [], f"ya:confirm:{nonce}", config
                )
            await yandex_gate(session, STUDENT, state, person, [], f"ya:back:{nonce}", config)
            assert state.state == "yandex_name" and state.payload["full_name"] == "Иван Иванов"

    asyncio.run(scenario())


def test_access_logging_strips_oauth_queries():
    record = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/auth/yandex/callback?code=SECRET&state=STATE", "1.1", 200),
        None,
    )
    OAuthQueryFilter().filter(record)
    assert "SECRET" not in record.getMessage() and "STATE" not in record.getMessage()


@pytest.mark.parametrize(
    "route,origin",
    [
        ("auth:profile", "profile"),
        ("auth:event:meeting_id:link", "event:meeting_id:link"),
        ("category:profile", "profile"),
    ],
)
def test_reauthentication_retains_origin_and_can_be_cancelled(route, origin):
    async def scenario():
        state = SimpleNamespace(state="idle", payload={})
        person = {"verified_at": NOW}
        result = await yandex_gate(None, STUDENT, state, person, [], route, settings())
        assert state.payload["resume"] == origin
        back = result.screen.markup.inline_keyboard[-1][0].callback_data
        assert back.startswith("ya:cancel:")
        result = await yandex_gate(None, STUDENT, state, person, [], back, settings())
        assert result.resume == origin and state.state == "idle"

    asyncio.run(scenario())


def test_guest_confirmation_never_grants_university_registration(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            session.info["yandex_auth_enabled"] = True
            state = SimpleNamespace(state="idle", payload={})
            person = {"verified_at": None}
            await yandex_gate(session, STUDENT, state, person, [], "menu", settings())
            await yandex_gate(
                session,
                STUDENT,
                state,
                person,
                [],
                f"ya:pick:{state.payload['nonce']}:external",
                settings(),
            )
            result = await yandex_gate(
                session,
                STUDENT,
                state,
                person,
                [],
                "",
                settings(),
                SimpleNamespace(text="Гость Иванов"),
            )
            assert state.state == "yandex_guest"
            confirm = result.screen.markup.inline_keyboard[0][0].callback_data
            result = await yandex_gate(session, STUDENT, state, person, [], confirm, settings())
            assert result.resume == "menu"
            p = (await session.get(TelegramAccount, STUDENT)).person
            assert p.kind == "guest" and p.verified_at is None and not can_register(session, p)

    asyncio.run(scenario())


def test_existing_yandex_binding_survives_shared_identity_merge_and_reauthentication(sessions):
    async def scenario():
        from cu_clubs.identity_merge import merge_into_imported

        await seed(sessions)
        key, *_ = await candidate(sessions)
        async with sessions.begin() as session:
            await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, key)
        async with sessions.begin() as session:
            old = (await session.get(TelegramAccount, STUDENT)).person
            # Exercise the shared merge contract: its caller resolves the canonical
            # login first and assigns verified fields after references are transferred.
            old.time_login = None
            await session.flush()
            canonical = Person(full_name="Импорт Иванов", kind="student", time_login="i.ivanov")
            session.add(canonical)
            await session.flush()
            canonical = await merge_into_imported(session, old, canonical, STUDENT)
            canonical.verified_at = NOW
            expected_id = canonical.id
        async with sessions.begin() as session:
            assert (await session.get(YandexIdentity, "123456")).person_id == expected_id
        second, *_ = await candidate(sessions)
        async with sessions.begin() as session:
            await YandexAuth(session, settings(), now=NOW).confirm(STUDENT, second)
            assert (await session.get(TelegramAccount, STUDENT)).person_id == expected_id

    asyncio.run(scenario())
