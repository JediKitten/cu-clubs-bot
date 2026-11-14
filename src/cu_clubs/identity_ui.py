"""Student verification and administrator dialogues over shared application commands."""

import re
from html import escape
from uuid import uuid4

from cu_clubs import legacy
from cu_clubs.access import CATEGORIES, self_registration_enabled
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


def _auth_preview(state, notice=""):
    return _screen(
        (escape(notice) + "\n\n" if notice else "")
        + f"Отправить код на {escape(state.payload['login'])}@edu.centraluniversity.ru?",
        [("Отправить код", f"authsend:{state.payload['token']}")],
        [("Назад к логину", f"authback:{state.payload['token']}:login")],
        [(legacy.text("cancel"), f"authcancel:{state.payload['token']}")],
    )


def _code_screen(state, text="Введи код из письма"):
    return _screen(
        text,
        [(legacy.text("resend"), f"authresend:{state.payload['token']}")],
        [(legacy.text("back"), f"authback:{state.payload['token']}:preview")],
        [(legacy.text("cancel"), f"authcancel:{state.payload['token']}")],
    )


def _validate_token(data, action, state):
    token = state.payload.get("token")
    if not token or data != f"{action}:{token}":
        raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")


def _login_screen(state, notice=""):
    text = legacy.text("email_request") + "\n\nМожно указать логин в Тайме, например i.ivanov."
    if state.payload.get("full_name"):
        text += "\nФИО: " + escape(state.payload["full_name"])
    if state.payload.get("login"):
        text += "\nТекущий логин: " + escape(state.payload["login"])
    if notice:
        text = escape(notice) + "\n\n" + text
    return _screen(
        text,
        [(legacy.text("back"), f"authback:{state.payload['token']}:fio")],
        [(legacy.text("cancel"), f"authcancel:{state.payload['token']}")],
    )


def _fio_screen(state, notice=""):
    text = legacy.text("fio_request")
    if state.payload.get("full_name"):
        text += "\nТекущее ФИО: " + escape(state.payload["full_name"])
    if notice:
        text = escape(notice) + "\n\n" + text
    return _screen(text, [(legacy.text("cancel"), f"authcancel:{state.payload['token']}")])


def _origin(value):
    """Accept only routes that can safely resume the interrupted user flow."""
    value = value or "profile"
    if value in {"profile", "aqpersonal"}:
        return value
    if re.fullmatch(r"apilink:[0-9a-fA-F]{32}", value):
        return value
    if re.fullmatch(r"event:[a-zA-Z0-9_-]{1,64}(?::link)?", value):
        return value
    if re.fullmatch(r"aqi:[0-9a-fA-F-]{32,36}", value):
        return value
    return "profile"


async def _validated_origin(session, user_id, state, settings):
    origin = _origin(state.payload.get("resume"))
    if origin.startswith("aqi:"):
        from cu_clubs.attendance import AttendanceService

        try:
            service = AttendanceService(session, settings.verification_secret)
            raw = await service.resume_raw(user_id, origin[4:])
            await service.resolve_qr(user_id, raw)
        except (PermissionDenied, ValidationError):
            return "profile"
    return origin


def _auth_back(state, target):
    expected = {"preview": "auth_code", "login": "auth_preview", "fio": "auth_login"}
    if state.state != expected.get(target):
        raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")
    if target == "preview":
        state.state = "auth_preview"
        return _auth_preview(state)
    if target == "login":
        state.state = "auth_login"
        return _login_screen(state)
    if target == "fio":
        state.state = "auth_fio"
        return _fio_screen(state)
    raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")


async def _auth_cancel(session, user_id, state, settings):
    origin = await _validated_origin(session, user_id, state, settings)
    state.state, state.payload = "idle", {}
    if origin == "profile":
        return await handle_identity(session, user_id, state, None, "profile", settings)
    if origin == "aqpersonal":
        from cu_clubs.attendance_ui import handle_attendance

        return await handle_attendance(session, user_id, state, None, origin, settings)
    if origin.startswith("apilink:"):
        from cu_clubs.api_ui import handle_integrations

        return await handle_integrations(session, user_id, state, None, origin, settings)
    if origin.startswith("aqi:"):
        from cu_clubs.attendance_ui import handle_attendance

        return await handle_attendance(session, user_id, state, None, origin, settings)
    if origin.startswith("event:"):
        from cu_clubs.event_ui import handle_events

        return await handle_events(session, user_id, state, None, origin, settings)
    return await handle_identity(session, user_id, state, None, "profile", settings)


async def _auth_success(session, user_id, state, settings):
    origin = await _validated_origin(session, user_id, state, settings)
    state.state, state.payload = "idle", {}
    if origin == "profile":
        screen = await handle_identity(session, user_id, state, None, "profile", settings)
        screen.text = "Почта подтверждена.\n\n" + screen.text
        return screen
    if origin.startswith("apilink:"):
        from cu_clubs.api_ui import handle_integrations

        return await handle_integrations(session, user_id, state, None, origin, settings)
    if origin == "aqpersonal":
        from cu_clubs.attendance_ui import handle_attendance

        screen = await handle_attendance(session, user_id, state, None, origin, settings)
        screen.text = "Почта подтверждена.\n\n" + screen.text
        return screen
    if origin.startswith("aqi:"):
        from cu_clubs.attendance_ui import handle_attendance

        screen = await handle_attendance(session, user_id, state, None, origin, settings)
        screen.text = "Почта подтверждена.\n\n" + screen.text
        return screen
    if origin.startswith("event:"):
        from cu_clubs.event_ui import handle_events

        try:
            screen = await handle_events(session, user_id, state, None, origin, settings)
        except (PermissionDenied, ValidationError):
            return _screen("Почта подтверждена", [("Продолжить", origin)])
        screen.text = "Почта подтверждена.\n\n" + screen.text
        return screen
    return _screen(
        "Почта подтверждена. Теперь можно продолжить исходное действие.",
        [("Продолжить", origin)],
        [("Личный кабинет", "profile")],
    )


async def handle_identity(session, user_id, state, message, data, settings, person=None):
    """Return a screen when this flow owns an action, otherwise leave it untouched."""
    temporary = self_registration_enabled(session)
    if temporary and (data.startswith("auth") or state.state.startswith("auth_")):
        from cu_clubs.category_ui import begin_category

        origin = _origin(data[5:] if data.startswith("auth:") else state.payload.get("resume"))
        return begin_category(state, origin)
    if data == "profile":
        person = person or await _identity(session, settings).profile(user_id)
        if temporary:
            category = person.get("declared_category")
            role = {"student": "student", "applicant": "grant_user", "external": "external_user"}
            text = legacy.personal_account_text(person["full_name"], role.get(category))
            if category == "staff":
                text = text.replace("<b>не определена</b>", "<b>сотрудник</b>")
            text += f"\n\nКатегория: {escape(CATEGORIES.get(category, 'Не выбрана'))}"
            text += "\nПодтверждение почты пока не требуется."
            return Screen(
                text,
                keyboard(
                    [("Мой QR", "aqpersonal")],
                    [("Изменить категорию", "category:profile")],
                    [("Подключённые боты", "apigrants")],
                    [("В меню", "menu")],
                ),
                banner_role="personal-account",
            )
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
        rows.append([("Подключённые боты", "apigrants")])
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
        origin = _origin(data[5:])
        state.state, state.payload = (
            "auth_fio",
            {
                "resume": origin,
                "token": uuid4().hex,
            },
        )
        return _fio_screen(state)
    if data.startswith(("authback:", "authcancel:")):
        action, token, *target = data.split(":")
        _validate_token(f"{action}:{token}", action, state)
        if action == "authcancel":
            return await _auth_cancel(session, user_id, state, settings)
        if len(target) != 1:
            raise ValidationError("Эта кнопка устарела. Продолжи текущий диалог.")
        return _auth_back(state, target[0])
    if data.split(":", 1)[0] in {"authsend", "authresend", "authlogin"}:
        action = data.split(":", 1)[0]
        _validate_token(data, action, state)
        if not state.state.startswith("auth_"):
            raise ValidationError("Эта кнопка устарела. Открой подтверждение почты заново.")
        data = action
    if data == "authlogin" and state.state.startswith("auth_"):
        if state.state == "auth_code":
            state.state = "auth_preview"
        return _auth_back(state, "login")
    if data in {"authsend", "authresend"} and state.state in {"auth_preview", "auth_code"}:
        delivery = await _identity(session, settings).start_verification(
            user_id, state.payload["full_name"], state.payload["login"]
        )
        state.state = "auth_code"
        state.payload = {**state.payload, "challenge_id": delivery["challenge_id"]}
        return _code_screen(state, "Код поставлен в очередь отправки. Введи код из письма.")
    if message and state.state == "auth_fio":
        try:
            full_name = normalize_name(message.text or "")
        except ValidationError as error:
            return _fio_screen(state, str(error))
        state.state, state.payload = "auth_login", {**state.payload, "full_name": full_name}
        return _login_screen(state)
    if message and state.state == "auth_login":
        try:
            login = normalize_login(message.text or "")
        except ValidationError as error:
            return _login_screen(state, str(error))
        state.state, state.payload = "auth_preview", {**state.payload, "login": login}
        return _auth_preview(state)
    if message and state.state == "auth_code":
        result = await _identity(session, settings).verify(user_id, (message.text or "").strip())
        if result["status"] == "verified":
            return await _auth_success(session, user_id, state, settings)
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
