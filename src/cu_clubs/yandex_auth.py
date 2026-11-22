"""Provider email proof is staged until the originating Telegram user confirms it.

No provider access/refresh token is persisted. Browser state is single-use, bound
to a secure cookie, and exchanged outside database transactions with S256 PKCE.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select, update

from cu_clubs.identity import EMAIL_DOMAIN, IdentityService, normalize_login, normalize_name
from cu_clubs.identity_models import YandexChallenge, YandexIdentity
from cu_clubs.models import Audit
from cu_clubs.services import ValidationError

TTL = timedelta(minutes=10)
logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def signed(settings, label, value):
    return (
        base64.urlsafe_b64encode(
            hmac.new(
                settings.verification_secret.get_secret_value().encode(),
                f"yandex:{label}:{value}".encode(),
                hashlib.sha256,
            ).digest()
        )
        .decode()
        .rstrip("=")
    )


def ticket(settings, challenge_id):
    key = UUID(str(challenge_id)).hex
    return key + "." + signed(settings, "ticket", key)


def ticket_id(settings, value):
    try:
        key, signature = value.split(".")
        if not hmac.compare_digest(signed(settings, "ticket", key), signature):
            raise ValueError
        return UUID(hex=key)
    except (ValueError, AttributeError) as error:
        raise ValidationError("Ссылка входа недействительна. Начни заново в боте.") from error


def login_url(settings, challenge_id):
    base = settings.yandex_redirect_uri.removesuffix("/callback")
    return base + "/start?" + urlencode({"ticket": ticket(settings, challenge_id)})


class YandexAuth:
    def __init__(self, session, settings, *, now=None):
        self.session, self.settings = session, settings
        self.now = now or datetime.now(UTC)

    async def begin(self, user_id, full_name, category):
        if not self.settings.yandex_auth_enabled or category not in {"student", "staff"}:
            raise ValidationError("Вход через Яндекс пока недоступен.")
        await IdentityService(self.session, "", mail_available=False)._lock_person(user_id)
        count = await self.session.scalar(
            select(func.count())
            .select_from(YandexChallenge)
            .where(
                YandexChallenge.telegram_id == user_id,
                YandexChallenge.created_at > self.now - timedelta(hours=1),
            )
        )
        if count >= 5:
            raise ValidationError("Слишком много попыток входа. Попробуй позже.")
        await self.session.execute(
            update(YandexChallenge)
            .where(
                YandexChallenge.telegram_id == user_id,
                YandexChallenge.status.notin_(["confirmed", "failed", "cancelled"]),
            )
            .values(status="cancelled")
        )
        challenge = YandexChallenge(
            telegram_id=user_id,
            full_name=normalize_name(full_name),
            category=category,
            status="new",
            expires_at=self.now + TTL,
            created_at=self.now,
        )
        self.session.add(challenge)
        await self.session.flush()
        return challenge

    async def owned(self, user_id, challenge_id):
        try:
            key = UUID(str(challenge_id))
        except (ValueError, TypeError) as error:
            raise ValidationError("Открой вход заново.") from error
        row = await self.session.get(YandexChallenge, key, with_for_update=True)
        if row is None or row.telegram_id != user_id:
            raise ValidationError("Открой свою ссылку входа в боте.")
        return row

    async def start_browser(self, raw_ticket):
        key = ticket_id(self.settings, raw_ticket)
        row = await self.session.get(YandexChallenge, key, with_for_update=True)
        if row is None or row.status != "new" or row.expires_at <= self.now:
            raise ValidationError("Ссылка уже использована или истекла. Начни заново в боте.")
        browser = secrets.token_urlsafe(32)
        state = row.id.hex + "." + signed(self.settings, "state", row.id.hex)
        row.state_digest, row.browser_digest, row.status = digest(state), digest(browser), "started"
        verifier = signed(self.settings, "pkce", row.id.hex)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        url = "https://oauth.yandex.ru/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.yandex_client_id,
                "redirect_uri": self.settings.yandex_redirect_uri,
                "scope": "login:email",
                "force_confirm": "yes",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return url, browser

    async def claim_callback(self, state, browser):
        if not state or not browser or len(state) > 200 or len(browser) > 200:
            raise ValidationError("Вход не подтверждён в этом браузере. Начни заново в боте.")
        row = await self.session.scalar(
            select(YandexChallenge)
            .where(YandexChallenge.state_digest == digest(state))
            .with_for_update()
        )
        if (
            row is None
            or row.status != "started"
            or row.expires_at <= self.now
            or not hmac.compare_digest(row.browser_digest or "", digest(browser))
        ):
            raise ValidationError("Ссылка входа истекла или уже использована.")
        row.status = "exchanging"
        return row.id

    async def record_result(self, challenge_id, profile):
        row = await self.session.get(YandexChallenge, challenge_id, with_for_update=True)
        if row is None or row.status != "exchanging" or row.expires_at <= self.now:
            raise ValidationError("Вход истёк. Начни заново в боте.")
        row.status = "failed"
        if profile is None:
            logger.warning("Yandex sign-in rejected: provider response unavailable")
            return False
        email = profile.get("default_email")
        subject = profile.get("id")
        if (
            profile.get("client_id") != self.settings.yandex_client_id
            or not isinstance(email, str)
            or email != email.strip()
            or email.lower().count("@") != 1
            or email.lower().rsplit("@", 1)[-1] != EMAIL_DOMAIN
            or not isinstance(subject, str)
            or not re.fullmatch(r"[0-9]{1,100}", subject)
        ):
            logger.warning(
                "Yandex sign-in rejected: profile validation; client_matches=%s "
                "university_email=%s subject_valid=%s",
                profile.get("client_id") == self.settings.yandex_client_id,
                isinstance(email, str)
                and email == email.strip()
                and email.lower().count("@") == 1
                and email.lower().rsplit("@", 1)[-1] == EMAIL_DOMAIN,
                isinstance(subject, str) and bool(re.fullmatch(r"[0-9]{1,100}", subject)),
            )
            return False
        try:
            login = normalize_login(email)
        except ValidationError:
            return False
        row.subject, row.email, row.status = subject, f"{login}@{EMAIL_DOMAIN}", "candidate"
        return True

    async def confirm(self, user_id, challenge_id):
        identity = IdentityService(self.session, "", mail_available=False)
        person = await identity._lock_person(user_id)
        row = await self.owned(user_id, challenge_id)
        if row.status != "candidate" or row.expires_at <= self.now:
            raise ValidationError("Сначала заверши вход через Яндекс или начни его заново.")
        login = normalize_login(row.email)
        if person.verified_at and person.time_login != login:
            raise ValidationError("Смена подтверждённой почты доступна через администратора.")
        # All identity mutations take the same global lock as email/attendance merging.
        previous = await self.session.scalar(
            select(YandexIdentity)
            .where(
                or_(
                    YandexIdentity.subject == row.subject,
                    YandexIdentity.email == row.email,
                    YandexIdentity.person_id == person.id,
                )
            )
            .with_for_update()
        )
        if previous and (
            previous.person_id != person.id
            or previous.subject != row.subject
            or previous.email != row.email
        ):
            raise ValidationError(
                "Эта почта уже связана с другим профилем. Обратись к администратору."
            )
        from cu_clubs.identity_merge import imported_person, merge_into_imported

        existing, occupied = await imported_person(self.session, login, person.id)
        if occupied:
            raise ValidationError(
                "Эта почта уже связана с другим профилем. Обратись к администратору."
            )
        if existing is not None:
            person = await merge_into_imported(self.session, person, existing, user_id)
            if previous is not None:
                previous.person_id = person.id
        person.full_name, person.time_login = row.full_name, login
        person.kind, person.verified_at = "student", self.now
        person.declared_category, person.category_selected_at = row.category, self.now
        if previous is None:
            self.session.add(
                YandexIdentity(
                    subject=row.subject,
                    person_id=person.id,
                    email=row.email,
                    verified_at=self.now,
                )
            )
        else:
            previous.verified_at = self.now
        row.status = "confirmed"
        self.session.add(
            Audit(
                actor_telegram_id=user_id,
                action="identity.yandex.verified",
                entity_id=person.id,
                details={"challenge_id": str(row.id)},
            )
        )
        await self.session.flush()


async def exchange(settings, challenge_id, code, *, transport=None):
    if not code or len(code) > 2048:
        return None
    stage = "token"
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, transport=transport
        ) as client:
            response = await client.post(
                "https://oauth.yandex.ru/token",
                auth=(settings.yandex_client_id, settings.yandex_client_secret.get_secret_value()),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": signed(settings, "pkce", challenge_id.hex),
                },
            )
            response.raise_for_status()
            token = response.json()
            scope = token.get("scope")
            # Yandex omits scope when the token has all requested permissions.
            if (
                (
                    "scope" in token
                    and (not isinstance(scope, str) or "login:email" not in scope.split())
                )
                or not isinstance(token.get("access_token"), str)
                or not token["access_token"]
            ):
                logger.warning("Yandex sign-in rejected: token permissions or format")
                return None
            stage = "userinfo"
            response = await client.get(
                "https://login.yandex.ru/info",
                params={"format": "json"},
                headers={"Authorization": "OAuth " + token["access_token"]},
            )
            response.raise_for_status()
            profile = response.json()
            return profile if isinstance(profile, dict) else None
    except httpx.HTTPStatusError as error:
        logger.warning(
            "Yandex sign-in failed: stage=%s status=%s", stage, error.response.status_code
        )
        return None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        logger.warning("Yandex sign-in failed: stage=%s", stage)
        return None
