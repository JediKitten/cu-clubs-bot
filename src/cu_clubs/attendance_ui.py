"""Private attendance dialogues; every confirmation rechecks domain permissions."""

from copy import deepcopy
from html import escape
from uuid import uuid4

from aiogram.types import BufferedInputFile

from cu_clubs import legacy
from cu_clubs.attendance import AttendanceService, qr_photo
from cu_clubs.attendance_xlsx import export_attendance_xlsx, template_attendance_xlsx
from cu_clubs.identity import normalize_login, normalize_name
from cu_clubs.services import PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard


def _screen(text, *rows, photo=None):
    return Screen(text, keyboard(*rows), photo=photo, banner_role=None if photo else "events")


def _state(state, state_name, **payload):
    state.state, state.payload = state_name, {**payload, "nonce": uuid4().hex[:12]}


def _confirm(state, name, nonce):
    if state.state != name or state.payload.get("nonce") != nonce:
        raise ValidationError("Кнопка устарела. Открой актуальный экран")


def _marked(result, *, organizer=False):
    rows = [
        [("Открыть встречу", f"event:{result['event']}:link")],
        [("В меню", "menu")],
    ]
    if organizer:
        rows = [
            [("К посещаемости", f"at:{result['event']}")],
            [("К встрече", f"event:{result['event']}:link")],
            [("Отметить ещё", f"atmanual:{result['event']}")],
        ]
    return _screen(
        "Посещение отмечено" if result["created"] else "Посещение уже отмечено",
        *rows,
    )


def _snapshot(state):
    return state.state, deepcopy(state.payload)


def _restore(state, snapshot):
    state.state, state.payload = snapshot


def _with_notice(screen, notice):
    if notice:
        screen.text = escape(notice) + "\n\n" + screen.text
    return screen


def _is_stale(error):
    return any(word in str(error).lower() for word in ("устар", "изменил", "верси"))


def _manual_name_screen(state, notice=""):
    text = legacy.text("fio_request")
    if state.payload.get("name"):
        text += "\nТекущее ФИО: " + escape(state.payload["name"])
    rows = [[(legacy.text("cancel"), f"at:{state.payload['event']}")]]
    return _with_notice(_screen(text, *rows), notice)


def _manual_login_screen(state, notice=""):
    text = "Введи логин студента в Тайме"
    if state.payload.get("name"):
        text += "\nФИО: " + escape(state.payload["name"])
    if state.payload.get("login"):
        text += "\nТекущий логин: " + escape(state.payload["login"])
    return _with_notice(
        _screen(
            text,
            [("Назад", f"atmanualback:{state.payload['nonce']}:name")],
            [(legacy.text("cancel"), f"at:{state.payload['event']}")],
        ),
        notice,
    )


def _manual_confirm_screen(state, notice=""):
    return _with_notice(
        _screen(
            f"{escape(state.payload['name'])}\n{escape(state.payload['login'])}\n"
            "Отметить посещение?",
            [(legacy.text("confirm"), f"atmanualconfirm:{state.payload['nonce']}")],
            [("Назад", f"atmanualback:{state.payload['nonce']}:login")],
            [(legacy.text("cancel"), f"at:{state.payload['event']}")],
        ),
        notice,
    )


async def _import_screen(service, actor, state, batch_id, notice=""):
    preview = await service.import_preview(actor, batch_id)
    _state(
        state,
        "attendance_import",
        batch=batch_id,
        version=preview["version"],
        event=preview["event"],
    )
    nonce = state.payload["nonce"]
    lines = [
        f"Новых {preview['new']} · уже отмечены {preview['existing']} · "
        f"требуют проверки {len(preview['conflicts'])}"
    ]
    rows = []
    for conflict in preview["conflicts"][:20]:
        number = conflict["row"]
        lines.append(
            f"\nСтрока {number}: {escape(conflict['full_name'])}; "
            f"профиль: {escape(conflict['existing_name'])}"
        )
        rows.append(
            [(f"Строка {number}: использовать существующий профиль", f"atresolve:{nonce}:{number}")]
        )
    for issue in preview["issues"]:
        lines.append(f"\nСтрока {issue.get('row', '?')}: {escape(issue['message'])}")
    if preview["new"] or preview["existing"]:
        rows.append([(f"Подтвердить {preview['new']}", f"atimportconfirm:{nonce}")])
    rows.append([("Назад", f"at:{preview['event']}")])
    return _with_notice(_screen("\n".join(lines), *rows), notice)


def _guest_name_screen(state, notice=""):
    text = legacy.text("fio_request")
    if state.payload.get("name"):
        text += "\nТекущее ФИО: " + escape(state.payload["name"])
    return _with_notice(
        _screen(
            text,
            [("Назад", f"aqi:{state.payload['qr_id']}")],
            [(legacy.text("cancel"), f"aqi:{state.payload['qr_id']}")],
        ),
        notice,
    )


def _guest_confirm_screen(state, notice=""):
    return _with_notice(
        _screen(
            f"Отметить посещение: {escape(state.payload['name'])}?",
            [(legacy.text("confirm"), f"atguestconfirm:{state.payload['nonce']}")],
            [("Назад", f"atguestback:{state.payload['nonce']}")],
            [(legacy.text("cancel"), f"aqi:{state.payload['qr_id']}")],
        ),
        notice,
    )


def _upload_screen(state, notice=""):
    return _with_notice(
        _screen(
            "Пришли .xlsx: ФИО, Логин в Тайме, Telegram-тег (необязательно). "
            "Перед записью будет предпросмотр.",
            [(legacy.text("cancel"), f"at:{state.payload['event']}")],
        ),
        notice,
    )


async def handle_attendance(session, user_id, state, message, data, settings, *, upload=None):
    service = AttendanceService(session, getattr(settings, "verification_secret", None))
    if data in {"aqpersonal", "aqpersonalrotate"}:
        try:
            qr = await service.get_personal_qr(user_id, rotate=data.endswith("rotate"))
        except PermissionDenied as error:
            return _screen(
                str(error), [("Подтвердить почту", "auth:aqpersonal")], [("В меню", "menu")]
            )
        link = f"https://t.me/{settings.telegram_bot_username}?start=a_{qr['token']}"
        return _screen(
            legacy.text("qr_text"),
            [("Обновить QR", "aqpersonalrotate")],
            [("Назад", "profile")],
            photo=qr_photo(link),
        )
    if data.startswith(("aq:", "aqi:")):
        raw = data[3:] if data.startswith("aq:") else await service.resume_raw(user_id, data[4:])
        qr = await service.resolve_qr(user_id, raw)
        if qr["purpose"] == "personal":
            _state(
                state,
                "attendance_person_select",
                qr_id=qr["qr_id"],
                choices=qr["events"],
                name=qr["full_name"],
            )
            return _person_choices(state, 0)
        _state(state, "attendance_self", qr_id=qr["qr_id"], event=qr["event"])
        person = await service._person(user_id)
        if person.kind == "guest" or person.verified_at is not None:
            return _screen(
                escape(qr["title"]),
                [("Отметить посещение", f"atself:{state.payload['nonce']}")],
                [("В меню", "menu")],
            )
        return _screen(
            "Ты студент ЦУ или внешний гость?\n\n" + escape(qr["title"]),
            [("Студент ЦУ", f"auth:aqi:{qr['qr_id']}")],
            [("Внешний гость", f"atguest:{state.payload['nonce']}")],
        )
    if data.startswith("atself:"):
        _confirm(state, "attendance_self", data.split(":")[1])
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                raw = await service.resume_raw(user_id, state.payload["qr_id"])
                result = await service.mark_event_qr(user_id, raw)
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return _with_notice(
                _screen(
                    "Посещение нельзя отметить по этому QR.",
                    [("Назад к QR", f"aqi:{state.payload['qr_id']}")],
                ),
                str(error),
            )
        state.state, state.payload = "idle", {}
        return _marked(result)
    if data.startswith("atguest:"):
        _confirm(state, "attendance_self", data.split(":")[1])
        _state(
            state,
            "attendance_guest_name",
            qr_id=state.payload["qr_id"],
            event=state.payload["event"],
        )
        return _guest_name_screen(state)
    if message and state.state == "attendance_guest_name":
        try:
            name = normalize_name(message.text or "")
        except ValidationError as error:
            return _guest_name_screen(state, str(error))
        _state(
            state,
            "attendance_guest_confirm",
            qr_id=state.payload["qr_id"],
            event=state.payload["event"],
            name=name,
        )
        return _guest_confirm_screen(state)
    if data.startswith("atguestback:"):
        _confirm(state, "attendance_guest_confirm", data.split(":", 1)[1])
        qr_id, event, name = (
            state.payload["qr_id"],
            state.payload["event"],
            state.payload["name"],
        )
        _state(state, "attendance_guest_name", qr_id=qr_id, event=event, name=name)
        return _guest_name_screen(state)
    if data.startswith("atguestconfirm:"):
        _confirm(state, "attendance_guest_confirm", data.split(":")[1])
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                result = await service.register_guest_and_mark(
                    user_id,
                    await service.resume_raw(user_id, state.payload["qr_id"]),
                    state.payload["name"],
                )
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return _guest_confirm_screen(state, str(error))
        state.state, state.payload = "idle", {}
        return _marked(result)
    if data.startswith("atpage:"):
        _, nonce, page = data.split(":")
        _confirm(state, "attendance_person_select", nonce)
        if not page.isascii() or not page.isdigit():
            return _with_notice(
                _person_choices(state, state.payload.get("page", 0)), "Страница недоступна"
            )
        page_number = int(page)
        try:
            screen = _person_choices(state, page_number)
        except ValidationError as error:
            return _with_notice(_person_choices(state, state.payload.get("page", 0)), str(error))
        state.payload["page"] = page_number
        return screen
    if data.startswith("atpick:"):
        _, nonce, index = data.split(":")
        _confirm(state, "attendance_person_select", nonce)
        try:
            event = state.payload["choices"][int(index)]
        except (ValueError, IndexError):
            raise ValidationError("Встреча недоступна") from None
        _state(
            state,
            "attendance_person_confirm",
            event=event["public_id"],
            qr_id=state.payload["qr_id"],
            name=state.payload["name"],
            choices=state.payload["choices"],
            page=state.payload.get("page", 0),
        )
        return _screen(
            f"{escape(state.payload['name'])}\n{escape(event['title'])}\nОтметить посещение?",
            [(legacy.text("confirm"), f"atperson:{state.payload['nonce']}")],
            [("Назад", f"atpersonback:{state.payload['nonce']}")],
            [(legacy.text("cancel"), f"aqi:{state.payload['qr_id']}")],
        )
    if data.startswith("atpersonback:"):
        _confirm(state, "attendance_person_confirm", data.split(":", 1)[1])
        qr_id, choices, name, page = (
            state.payload["qr_id"],
            state.payload["choices"],
            state.payload["name"],
            state.payload["page"],
        )
        _state(
            state,
            "attendance_person_select",
            qr_id=qr_id,
            choices=choices,
            name=name,
            page=page,
        )
        return _person_choices(state, page)
    if data.startswith("atperson:"):
        _confirm(state, "attendance_person_confirm", data.split(":")[1])
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                result = await service.mark_personal_qr(
                    user_id,
                    state.payload["event"],
                    await service.resume_raw(user_id, state.payload["qr_id"]),
                )
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return _screen(
                escape(str(error)),
                [("Назад", f"atpersonback:{state.payload['nonce']}")],
                [(legacy.text("cancel"), f"aqi:{state.payload['qr_id']}")],
            )
        state.state, state.payload = "idle", {}
        return _marked(result, organizer=True)
    if data.startswith("at:"):
        event = await service._event(data[3:])
        await service._authorize(user_id, event)
        rows = [
            [("Показать QR встречи", f"atqr:{event.public_id}")],
            [("Отметить студента вручную", f"atmanual:{event.public_id}")],
            [("Импорт Excel", f"atupload:{event.public_id}")],
            [("Скачать шаблон", f"atblank:{event.public_id}")],
            [("Ведомость", f"atledger:{event.public_id}")],
            [("Отметить проведённой", f"athold:{event.public_id}")],
            [("Назад", f"event:{event.public_id}")],
        ]
        if state.state == "attendance_import" and state.payload.get("event") == event.public_id:
            rows.insert(0, [("К предпросмотру Excel", f"atimportresume:{state.payload['nonce']}")])
        return _screen(
            "Посещаемость · " + escape(event.title),
            *rows,
        )
    if data.startswith("atqr:"):
        qr = await service.event_qr(user_id, data.split(":")[1])
        return _screen(
            legacy.text("event_qr_text") + "\n\nДействует до отзыва, без временного окна.",
            [("Обновить QR", f"atrotate:{qr['event']}")],
            [("Назад", f"at:{qr['event']}")],
            photo=qr_photo(f"https://t.me/{settings.telegram_bot_username}?start=a_{qr['token']}"),
        )
    if data.startswith("atrotate:"):
        event = data.split(":")[1]
        await service.event_qr(user_id, event, rotate=True)
        return await handle_attendance(session, user_id, state, None, f"atqr:{event}", settings)
    if data.startswith("atmanual:"):
        event = await service._event(data.split(":")[1])
        await service._authorize(user_id, event)
        _state(state, "attendance_manual_name", event=event.public_id)
        return _manual_name_screen(state)
    if data.startswith("atmanualback:"):
        _, nonce, target = data.split(":", 2)
        _confirm(
            state,
            "attendance_manual_confirm"
            if state.state == "attendance_manual_confirm"
            else "attendance_manual_login",
            nonce,
        )
        event, name = state.payload["event"], state.payload["name"]
        if target == "name":
            _state(state, "attendance_manual_name", event=event, name=name)
            return _manual_name_screen(state)
        if target == "login":
            login = state.payload.get("login", "")
            _state(
                state,
                "attendance_manual_login",
                event=event,
                name=name,
                login=login,
            )
            return _manual_login_screen(state)
        raise ValidationError("Кнопка устарела. Открой актуальный экран")
    if message and state.state == "attendance_manual_name":
        try:
            name = normalize_name(message.text or "")
        except ValidationError as error:
            return _manual_name_screen(state, str(error))
        _state(
            state,
            "attendance_manual_login",
            event=state.payload["event"],
            name=name,
        )
        return _manual_login_screen(state)
    if message and state.state == "attendance_manual_login":
        try:
            login = normalize_login(message.text or "")
        except ValidationError as error:
            return _manual_login_screen(state, str(error))
        _state(
            state,
            "attendance_manual_confirm",
            event=state.payload["event"],
            name=state.payload["name"],
            login=login,
        )
        return _manual_confirm_screen(state)
    if data.startswith("atmanualconfirm:"):
        _confirm(state, "attendance_manual_confirm", data.split(":")[1])
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                result = await service.mark_student(
                    user_id,
                    state.payload["event"],
                    state.payload["name"],
                    state.payload["login"],
                )
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return _manual_confirm_screen(state, str(error))
        state.state, state.payload = "idle", {}
        return _marked(result, organizer=True)
    if data.startswith("atupload:"):
        event = await service._event(data.split(":")[1])
        await service._authorize(user_id, event)
        _state(state, "attendance_upload", event=event.public_id)
        return _upload_screen(state)
    if upload is not None:
        if state.state != "attendance_upload":
            raise ValidationError("Сначала выбери встречу и открой «Импорт Excel»")
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                result = await service.preview_import(
                    user_id, state.payload["event"], upload["rows"], upload.get("issues")
                )
        except ValidationError as error:
            _restore(state, snapshot)
            return _upload_screen(state, str(error))
        return await _import_screen(service, user_id, state, result["id"])
    if data.startswith("atresolve:"):
        _, nonce, row = data.split(":")
        _confirm(state, "attendance_import", nonce)
        if not row.isascii() or not row.isdigit():
            return await _import_screen(
                service, user_id, state, state.payload["batch"], "Строка не требует проверки"
            )
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                await service.resolve_import(
                    user_id, state.payload["batch"], [int(row)], state.payload["version"]
                )
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return await _import_screen(service, user_id, state, state.payload["batch"], str(error))
        return await _import_screen(service, user_id, state, state.payload["batch"])
    if data.startswith("atimportresume:"):
        _confirm(state, "attendance_import", data.split(":", 1)[1])
        return await _import_screen(service, user_id, state, state.payload["batch"])
    if data.startswith("atimportconfirm:"):
        _confirm(state, "attendance_import", data.split(":")[1])
        snapshot = _snapshot(state)
        try:
            async with session.begin_nested():
                result = await service.confirm_import(
                    user_id, state.payload["batch"], state.payload["version"]
                )
        except ValidationError as error:
            _restore(state, snapshot)
            if _is_stale(error):
                raise
            return await _import_screen(service, user_id, state, state.payload["batch"], str(error))
        state.state, state.payload = "idle", {}
        return _screen(
            f"Добавлено {result['new']} · уже отмечены {result['existing']} · "
            f"пропущено {result['skipped']}",
            [("К посещаемости", f"at:{result['event']}")],
            [("К встрече", f"event:{result['event']}:link")],
            [("Отметить ещё", f"atmanual:{result['event']}")],
        )
    if data.startswith("atledger:"):
        event_id = data.split(":", 1)[1]
        await service.roster(user_id, event_id)
        return _screen(
            "Какую ведомость скачать?",
            [("Посетившие", f"atexport:{event_id}")],
            [("Все участники", f"atexportall:{event_id}")],
            [("Назад", f"at:{event_id}")],
        )
    if data.startswith(("atblank:", "atexport:", "atexportall:")):
        event_id = data.split(":")[1]
        event = await service._event(event_id)
        if data.startswith("atblank:"):
            await service._authorize(user_id, event)
            content, filename = template_attendance_xlsx(), "attendance-template.xlsx"
        else:
            content, filename = (
                export_attendance_xlsx(
                    await service.roster(
                        user_id, event_id, all_participants=data.startswith("atexportall:")
                    )
                ),
                "attendance.xlsx",
            )
        screen = _screen("Файл ведомости", [("Назад", f"at:{event_id}")])
        screen.document = BufferedInputFile(content, filename=filename)
        return screen
    if data.startswith("athold:"):
        event = await service._event(data.split(":")[1])
        await service._authorize(user_id, event, leader=True)
        _state(state, "attendance_held_confirm", event=event.public_id, version=event.version)
        return _screen(
            "Отметить встречу проведённой?",
            [(legacy.text("confirm"), f"atholdconfirm:{state.payload['nonce']}")],
            [(legacy.text("cancel"), f"at:{event.public_id}")],
        )
    if data.startswith("atholdconfirm:"):
        _confirm(state, "attendance_held_confirm", data.split(":")[1])
        result = await service.mark_held(user_id, state.payload["event"], state.payload["version"])
        state.state, state.payload = "idle", {}
        return _screen("Встреча отмечена проведённой", [("Назад", f"event:{result['public_id']}")])
    return None


def _person_choices(state, page):
    choices, token = state.payload["choices"], state.payload["nonce"]
    if page < 0 or page * 15 >= len(choices):
        raise ValidationError("Страница недоступна")
    rows = [
        [(event["title"], f"atpick:{token}:{index}")]
        for index, event in enumerate(choices)
        if page * 15 <= index < (page + 1) * 15
    ]
    navigation = []
    if page:
        navigation.append(("←", f"atpage:{token}:{page - 1}"))
    if (page + 1) * 15 < len(choices):
        navigation.append(("→", f"atpage:{token}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([(legacy.text("cancel"), f"aqi:{state.payload['qr_id']}")])
    return _screen("Выбери встречу · " + escape(state.payload["name"]), *rows)
