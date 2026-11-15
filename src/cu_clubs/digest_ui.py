"""Durable administrator selection, preview and explicitly requested JSON export."""

from copy import deepcopy
from datetime import datetime, timedelta
from html import escape
from uuid import uuid4

from aiogram.types import BufferedInputFile

from cu_clubs.digest import MAX_EVENTS, MOSCOW, DigestService, week_bounds
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard


def _screen(text, rows, document=None):
    return Screen(text, keyboard(*rows), document=document)


def _callback(state, action):
    return f"dg:{state.payload['nonce']}:{action}"


def _weeks(state):
    today = datetime.now(MOSCOW).date()
    monday = today - timedelta(days=today.weekday())
    rows = [
        [(label, _callback(state, "week:" + (monday + timedelta(days=offset)).isoformat()))]
        for label, offset in [
            ("Текущая неделя", 0),
            ("Следующая неделя", 7),
            ("Прошлая неделя", -7),
        ]
    ]
    rows += [
        [("Другая неделя", _callback(state, "custom"))],
        [("Отмена", _callback(state, "cancel"))],
    ]
    return _screen("Создать дайджест · выбери неделю", rows)


def _mode(state, notice=""):
    state.payload["view"] = "mode"
    day = week_bounds(state.payload["week"])[0]
    return _screen(
        (escape(notice) + "\n\n" if notice else "")
        + f"Неделя с {day:%d.%m.%Y}. Какие встречи включить?",
        [
            [("Все открытые встречи", _callback(state, "all"))],
            [("Выбрать вручную", _callback(state, "manual:0"))],
            [("Изменить неделю", _callback(state, "weeks"))],
            [("Отмена", _callback(state, "cancel"))],
        ],
    )


async def _preview(service, actor, state, notice="", page=0):
    events = await service.candidates(actor, state.payload["week"])
    selected = state.payload["selected"]
    lines = [
        notice,
        "<b>Предпросмотр дайджеста</b>",
        "Неделя с " + state.payload["week"],
        f"Выбрано встреч: {len(selected)}",
        "Нет подходящих встреч. Выбери другую неделю." if not events else "",
    ]
    chosen = [e for e in events if e.public_id in selected]
    if page < 0 or page > max(0, (len(chosen) - 1) // 8):
        raise ValidationError("Страница недоступна.")
    lines += [
        escape(f"{e.starts_at.astimezone(MOSCOW):%d.%m %H:%M} · {e.title}")
        for e in chosen[page * 8 : page * 8 + 8]
    ]
    state.payload["view"] = "preview"
    rows = [
        [("Изменить выбор", _callback(state, "manual:0"))],
        [("Изменить неделю", _callback(state, "weeks"))],
        [("Отмена", _callback(state, "cancel"))],
    ]
    if len(chosen) > 8:
        lines.append(f"Страница {page + 1}/{(len(chosen) + 7) // 8}")
        rows.insert(
            0,
            [
                (label, _callback(state, f"preview:{target}"))
                for label, target in [("←", page - 1), ("→", page + 1)]
                if 0 <= target <= (len(chosen) - 1) // 8
            ],
        )
    if selected:
        rows.insert(0, [("Скачать JSON для Figma", _callback(state, "export"))])
    return _screen("\n\n".join(filter(None, lines)), rows)


async def handle_digest(session, user_id, state, message, data, settings):
    if not (data == "digest" or data.startswith("dg:") or (message and state.state == "digest")):
        return None
    service = DigestService(session, settings)
    await service.access(user_id)
    state.payload = deepcopy(state.payload)
    if data == "digest":
        state.state, state.payload = (
            "digest",
            {"nonce": uuid4().hex[:10], "selected": {}, "view": "weeks"},
        )
        return _weeks(state)
    if message and state.payload.get("view") == "custom":
        day, _, _ = week_bounds((message.text or "").strip())
        action = "week:" + day.isoformat()
    else:
        parts = data.split(":", 2)
        if len(parts) != 3 or state.state != "digest" or parts[1] != state.payload.get("nonce"):
            raise ValidationError("Кнопка устарела. Открой создание дайджеста заново.")
        action = parts[2]
    state.payload["nonce"] = uuid4().hex[:10]
    if action == "cancel":
        state.state, state.payload = "idle", {}
        return _screen("Создание дайджеста отменено.", [[("Админ-меню", "admin")]])
    if action == "weeks":
        state.payload["view"] = "weeks"
        return _weeks(state)
    if action == "custom":
        state.payload["view"] = "custom"
        return _screen(
            "Введи любую дату нужной недели: ГГГГ-ММ-ДД. Неделя начинается в понедельник.",
            [[("Назад", _callback(state, "weeks"))], [("Отмена", _callback(state, "cancel"))]],
        )
    if action == "mode":
        action = "week:" + state.payload["week"]
    if action.startswith("week:"):
        day, _, _ = week_bounds(action.split(":", 1)[1])
        state.payload.update(
            week=day.isoformat(),
            selected=state.payload["selected"]
            if state.payload.get("week") == day.isoformat()
            else {},
            view="mode",
        )
        return _mode(state)
    if "week" not in state.payload:
        raise ValidationError("Сначала выбери неделю.")
    events = await service.candidates(user_id, state.payload["week"])
    if action == "all":
        if len(events) > MAX_EVENTS:
            return _mode(state, "За неделю больше 500 встреч. Выбери встречи вручную.")
        state.payload["mode"] = "all"
        state.payload["selected"] = {e.public_id: e.version for e in events}
        return await _preview(service, user_id, state)
    if action.startswith(("manual:", "toggle:")):
        notice = ""
        state.payload["mode"] = "manual"
        parts = action.split(":")
        try:
            page = int(parts[-1])
        except ValueError:
            raise ValidationError("Некорректная страница.") from None
        if page < 0 or page > max(0, (len(events) - 1) // 8):
            raise ValidationError("Страница недоступна.")
        if parts[0] == "toggle":
            event = next((e for e in events if e.public_id == parts[1]), None)
            if event is None:
                return await _preview(
                    service, user_id, state, "Встреча больше недоступна. Обнови выбор."
                )
            selected = state.payload["selected"]
            if event.public_id in selected:
                del selected[event.public_id]
            else:
                if len(selected) >= MAX_EVENTS:
                    notice = "Можно выбрать не больше 500 встреч. Сними другую галочку.\n\n"
                else:
                    selected[event.public_id] = event.version
        state.payload["view"] = "manual"
        rows = [
            [
                (
                    ("✓ " if e.public_id in state.payload["selected"] else "□ ")
                    + f"{e.starts_at.astimezone(MOSCOW):%d.%m %H:%M} {e.title}"[:80],
                    _callback(state, f"toggle:{e.public_id}:{page}"),
                )
            ]
            for e in events[page * 8 : page * 8 + 8]
        ]
        rows += [
            [
                (label, _callback(state, f"manual:{target}"))
                for label, target in [("←", page - 1), ("→", page + 1)]
                if 0 <= target <= (len(events) - 1) // 8
            ]
        ]
        rows += [
            [("Предпросмотр", _callback(state, "preview"))],
            [("Назад", _callback(state, "mode"))],
            [("Отмена", _callback(state, "cancel"))],
        ]
        if not events:
            rows.insert(0, [("Изменить неделю", _callback(state, "weeks"))])
        return _screen(
            (
                notice + f"Выбери встречи · выбрано {len(state.payload['selected'])}"
                if events
                else "Нет подходящих встреч. Выбери другую неделю."
            ),
            rows,
        )
    if action == "preview" or action.startswith("preview:"):
        try:
            page = int(action.split(":")[1]) if ":" in action else 0
        except ValueError:
            raise ValidationError("Страница недоступна.") from None
        return await _preview(service, user_id, state, page=page)
    if action == "export":
        if state.payload.get("view") != "preview" or not state.payload["selected"]:
            raise ValidationError("Сначала проверь предпросмотр и выбери встречи.")
        try:
            encoded, fresh = await service.snapshot(
                user_id,
                state.payload["week"],
                state.payload["selected"],
                all_events=state.payload.get("mode") == "all",
            )
        except ValidationError as error:
            return await _preview(service, user_id, state, escape(str(error)))
        if encoded is None:
            state.payload["selected"] = fresh
            return await _preview(
                service,
                user_id,
                state,
                "Встречи изменились или стали недоступны. "
                "Проверь обновлённый предпросмотр и подтверди экспорт ещё раз.",
            )
        screen = await _preview(service, user_id, state)
        screen.document = BufferedInputFile(
            encoded, filename=f"cu-clubs-digest-{state.payload['week']}.json"
        )
        return screen
    raise ValidationError("Неизвестное действие дайджеста.")
