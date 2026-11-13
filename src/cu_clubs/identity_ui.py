"""Student verification and administrator dialogues over shared application commands."""

from html import escape
from uuid import uuid4

from cu_clubs import legacy
from cu_clubs.identity import AdminService, IdentityService, normalize_login, normalize_name
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard

LEVELS = ("Concept", "Pre-Seed", "Seed", "Pre-IPO", "IPO")


def mail_available(settings) -> bool:
    return bool(
        settings.verification_secret
        and settings.smtp_host
        and settings.smtp_username
        and settings.smtp_password
        and settings.smtp_sender
    )


def _identity(session, settings):
    secret = settings.verification_secret
    return IdentityService(
        session,
        secret.get_secret_value() if secret else "",
        mail_available=mail_available(settings),
    )


def _screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="auth")


def _auth_preview(state):
    return _screen(
        f"Отправить код на {escape(state.payload['login'])}@edu.centraluniversity.ru?",
        [("Отправить код", f"authsend:{state.payload['token']}")],
        [("Исправить логин", f"authlogin:{state.payload['token']}")],
        [(legacy.text("cancel"), "menu")],
    )


def _code_screen(state, text="Введи код из письма"):
    return _screen(
        text,
        [(legacy.text("resend"), f"authresend:{state.payload['token']}")],
        [("Исправить логин", f"authlogin:{state.payload['token']}")],
        [(legacy.text("cancel"), "menu")],
    )


def _validate_token(data, action, state):
    token = state.payload.get("token")
    if not token or data != f"{action}:{token}":
        raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")


def _login_screen():
    return _screen(
        legacy.text("email_request") + "\n\nМожно указать логин в Тайме, например i.ivanov.",
        [(legacy.text("cancel"), "menu")],
    )


async def handle_identity(session, user_id, state, message, data, settings, person=None):
    """Return a screen when this flow owns an action, otherwise leave it untouched."""
    if data == "profile":
        person = person or await _identity(session, settings).profile(user_id)
        verified = bool(person.get("verified_at"))
        guest = person.get("kind") == "guest"
        text = legacy.personal_account_text(
            person["full_name"],
            "external_user"
            if guest
            else "student"
            if verified and person.get("kind") == "student"
            else None,
        )
        rows = []
        rows.append([("Мой QR", "aqpersonal")])
        if guest:
            text += "\n\nПрофиль внешнего гостя. Почта ЦУ не требуется."
            rows.append([("Я студент ЦУ", "auth:profile")])
        elif verified:
            text += (
                f"\n\nПочта подтверждена: {escape(person['time_login'])}@edu.centraluniversity.ru"
            )
        else:
            text += "\n\nПочта не подтверждена."
            rows.append([("Подтвердить почту", "auth:profile")])
        rows.append([("В меню", "menu")])
        return Screen(text, keyboard(*rows), banner_role="personal-account")
    if data.startswith("auth:"):
        state.state, state.payload = (
            "auth_fio",
            {
                "resume": data[5:] or "profile",
                "token": uuid4().hex,
            },
        )
        return _screen(legacy.text("fio_request"), [(legacy.text("cancel"), "menu")])
    if data.split(":", 1)[0] in {"authsend", "authresend", "authlogin"}:
        action = data.split(":", 1)[0]
        _validate_token(data, action, state)
        if not state.state.startswith("auth_"):
            raise ValidationError("Эта кнопка устарела. Открой подтверждение почты заново.")
        data = action
    if data == "authlogin" and state.state.startswith("auth_"):
        state.state = "auth_login"
        return _login_screen()
    if data in {"authsend", "authresend"} and state.state in {"auth_preview", "auth_code"}:
        delivery = await _identity(session, settings).start_verification(
            user_id, state.payload["full_name"], state.payload["login"]
        )
        state.state = "auth_code"
        state.payload = {**state.payload, "challenge_id": delivery["challenge_id"]}
        return _code_screen(state, "Код поставлен в очередь отправки. Введи код из письма.")
    if message and state.state == "auth_fio":
        full_name = normalize_name(message.text or "")
        state.state, state.payload = "auth_login", {**state.payload, "full_name": full_name}
        return _login_screen()
    if message and state.state == "auth_login":
        state.state, state.payload = (
            "auth_preview",
            {**state.payload, "login": normalize_login(message.text or "")},
        )
        return _auth_preview(state)
    if message and state.state == "auth_code":
        result = await _identity(session, settings).verify(user_id, (message.text or "").strip())
        if result["status"] == "verified":
            resume = state.payload.get("resume", "profile")
            state.state, state.payload = "idle", {}
            return _screen("Почта подтверждена", [("Продолжить", resume)], [("В меню", "menu")])
        if result["status"] == "invalid":
            return _code_screen(
                state,
                legacy.text("wrong_code") + f"\n\nОсталось попыток: {result['remaining_attempts']}",
            )
        return _code_screen(state, "Код недоступен или срок его действия истёк. Запроси новый код.")

    if (
        data.startswith(("adminnew", "adminlevel:", "adminleader:"))
        or (message and state.state.startswith("admin_"))
        or data.startswith(("adminpick:", "adminsave"))
    ):
        if not await ClubService(session).is_admin(user_id):
            raise PermissionDenied("Администрирование доступно только администраторам.")
        if data.startswith("adminsave"):
            _validate_token(data, "adminsave", state)
            data = "adminsave"
        elif data.startswith("adminpick:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")
            _validate_token(f"adminpick:{parts[1]}", "adminpick", state)
            data = f"adminpick:{parts[2]}"
        return await _admin(session, user_id, state, message, data, settings)
    return None


def _admin_screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def _level_screen(state):
    return _admin_screen(
        "Выбери уровень клуба",
        *[[(level, f"adminpick:{state.payload['token']}:{level}")] for level in LEVELS],
        [(legacy.text("cancel"), "admin")],
    )


async def _admin(session, user_id, state, message, data, settings):
    if data == "adminnew":
        state.state, state.payload = "admin_name", {"token": uuid4().hex}
        return _admin_screen(legacy.text("input_club_name"), [(legacy.text("cancel"), "admin")])
    if data.startswith(("adminlevel:", "adminleader:")):
        action, public_id = data.split(":", 1)
        club = await ClubService(session).get_club(public_id)
        if not club:
            raise ValidationError("Клуб не найден")
        state.payload = {
            "club": public_id,
            "version": club["version"],
            "action": action,
            "token": uuid4().hex,
        }
        state.state = "admin_level" if action == "adminlevel" else "admin_leader"
        if action == "adminlevel":
            return _level_screen(state)
        return _admin_screen(
            "Пришли @тег или Telegram ID нового лидера. Человек должен сначала открыть бота.",
            [(legacy.text("cancel"), "admin")],
        )
    if message and state.state == "admin_name":
        name = (message.text or "").strip()
        if not 2 <= len(name) <= 100:
            raise ValidationError("Название должно содержать от 2 до 100 символов")
        state.state, state.payload = (
            "admin_level",
            {**state.payload, "name": name, "action": "create"},
        )
        return _level_screen(state)
    if data.startswith("adminpick:") and state.state == "admin_level":
        level = data.split(":", 1)[1]
        if level not in LEVELS:
            raise ValidationError("Неизвестный уровень")
        state.payload = {**state.payload, "level": level}
        if state.payload["action"] == "create":
            state.state = "admin_leader"
            return _admin_screen(
                "Пришли @тег или Telegram ID лидера. Человек должен сначала открыть бота.",
                [(legacy.text("cancel"), "admin")],
            )
        state.state = "admin_preview"
        return _admin_screen(
            f"Установить уровень {escape(level)}?",
            [(legacy.text("confirm"), f"adminsave:{state.payload['token']}")],
            [(legacy.text("cancel"), "admin")],
        )
    if message and state.state == "admin_leader":
        raw = (message.text or "").strip()
        target = await ClubService(session).resolve_telegram_target(raw)
        state.state, state.payload = (
            "admin_preview",
            {
                **state.payload,
                "leader": target["telegram_id"],
                "leader_tag": raw if raw.startswith("@") else None,
            },
        )
        p = state.payload
        text = f"Лидер: {escape(target['full_name'])} · {target['telegram_id']}"
        if target["username"]:
            text += f" · @{escape(target['username'])}"
        if p["action"] == "create":
            text = f"Клуб: {escape(p['name'])}\nУровень: {escape(p['level'])}\n" + text
        return _admin_screen(
            text,
            [(legacy.text("confirm"), f"adminsave:{state.payload['token']}")],
            [(legacy.text("cancel"), "admin")],
        )
    if data == "adminsave" and state.state == "admin_preview":
        p = state.payload
        service = AdminService(
            session,
            allow_unverified_leaders=(
                getattr(settings, "environment", None) == "test"
                and getattr(settings, "test_allow_unverified_leaders", False)
            ),
        )
        if p.get("leader_tag"):
            target = await ClubService(session).resolve_telegram_target(p["leader_tag"])
            if target["telegram_id"] != p["leader"]:
                raise ValidationError("Тег изменился. Выбери лидера заново.")
        if p["action"] == "create":
            club = await service.create_club(user_id, p["name"], p["level"], p["leader"])
            text = legacy.club_created_text(club["name"])
        elif p["action"] == "adminlevel":
            club = await service.set_level(
                user_id, p["club"], p["level"], expected_version=p["version"]
            )
            text = "Уровень клуба изменён."
        else:
            club = await service.set_leader(
                user_id, p["club"], p["leader"], expected_version=p["version"]
            )
            text = "Лидер клуба назначен."
        state.state, state.payload = "idle", {}
        return _admin_screen(
            text, [("Открыть клуб", f"manageclub:{club['public_id']}")], [("В меню", "menu")]
        )
    return _admin_screen("Открой актуальную карточку клуба.", [("В меню", "menu")])
