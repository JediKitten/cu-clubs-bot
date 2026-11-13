"""Calendar status and explicit room-conflict decisions for administrators."""

from html import escape
from uuid import uuid4

from cu_clubs import legacy
from cu_clubs.calendar import CalendarService
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard


def _screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


async def handle_calendar(session, user_id, state, message, data, settings):
    service = CalendarService(session)
    if data == "calendar" or data.startswith("calpage:"):
        items = await service.status(user_id)
        page = int(data.split(":")[1]) if data.startswith("calpage:") else 0
        if page < 0:
            raise ValidationError("Страница недоступна")
        page = min(page, max(0, (len(items) - 1) // 12))
        labels = {"synced": "✓", "conflict": "⚠", "error": "!", "pending": "…"}
        rows = [
            [(f"{labels.get(item['status'], '…')} {item['title'][:45]}", f"cal:{item['id']}")]
            for item in items[page * 12 : (page + 1) * 12]
        ]
        nav = []
        if page:
            nav.append(("←", f"calpage:{page - 1}"))
        if (page + 1) * 12 < len(items):
            nav.append(("→", f"calpage:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([(legacy.text("back"), "admin")])
        state.state, state.payload = "idle", {}
        text = "<b>Календарь</b>\n" + (
            "Синхронизация включена."
            if settings.calendar_sync_enabled
            else "Синхронизация выключена в настройках сервера."
        )
        if not items:
            text += "\nОпубликованные встречи появятся после первой сверки с таблицей."
        return _screen(text, *rows)
    if data.startswith("cal:"):
        binding_id = data.split(":", 1)[1]
        item = next(
            (item for item in await service.status(user_id) if item["id"] == binding_id), None
        )
        if not item:
            raise ValidationError("Запись календаря не найдена")
        token = uuid4().hex[:16]
        state.state, state.payload = (
            "calendar_action",
            {"binding": binding_id, "revision": item["revision"], "token": token},
        )
        title = escape(item["title"])
        status = {
            "synced": "Синхронизировано",
            "conflict": "Нужна проверка",
            "error": "Ошибка соединения или шаблона",
            "pending": "Ожидает синхронизации",
        }.get(item["status"], item["status"])
        text = (
            f"<b>{title}</b>\n{escape(status)}\nАудитория: {escape(item['room'] or 'Уточняется')}"
        )
        if item["sheet_title"] and item["row"]:
            text += f"\n{escape(item['sheet_title'])} · {item['column']}{item['row']}"
        if item["error"]:
            text += f"\nПричина: {escape(item['error'])}"
        rows = []
        conflict = item.get("conflict")
        if conflict and conflict.get("kind") == "room":
            text += (
                f"\n\nВ боте: {escape(conflict.get('local_room') or 'Не указана')}"
                f"\nВ таблице: {escape(conflict.get('sheet_room') or 'Не указана')}"
            )
            rows.extend(
                [
                    [("Оставить аудиторию из бота", f"calchoose:{token}:local")],
                    [("Принять аудиторию из таблицы", f"calchoose:{token}:sheet")],
                ]
            )
        rows.extend(
            [
                [("Повторить сверку", f"calretry:{token}")],
                [("Указать аудиторию", f"calroom:{item['event']}")],
                [(legacy.text("back"), "calendar")],
            ]
        )
        return _screen(text, *rows)
    if data.startswith(("calchoose:", "calretry:")):
        parts = data.split(":")
        if state.state != "calendar_action" or parts[1] != state.payload.get("token"):
            raise ValidationError("Кнопка устарела. Открой актуальное состояние календаря.")
        p = state.payload
        if parts[0] == "calchoose":
            await service.resolve(user_id, p["binding"], parts[2], p["revision"])
        else:
            await service.retry(user_id, p["binding"], p["revision"])
        state.state, state.payload = "idle", {}
        return _screen(
            "Решение сохранено. Фоновая сверка проверит актуальную ячейку ещё раз.",
            [("Календарь", "calendar")],
        )
    if data.startswith("calroom:"):
        await service._admin(user_id)
        from cu_clubs.events import EventService

        event = await EventService(session).get_event(data.split(":", 1)[1], user_id)
        state.state, state.payload = (
            "calendar_room",
            {"event": event["public_id"], "version": event["version"]},
        )
        return _screen(
            "Укажи выделенную и свободную аудиторию. Для очистки — «Удалить».",
            [(legacy.text("cancel"), "calendar")],
        )
    if message and state.state == "calendar_room":
        room = (message.text or "").strip()
        if not room or len(room) > 300:
            raise ValidationError("Пришли аудиторию текстом до 300 символов")
        await service.assign_room(
            user_id,
            state.payload["event"],
            None if room.lower() == "удалить" else room,
            state.payload["version"],
        )
        state.state, state.payload = "idle", {}
        return _screen(
            "Аудитория сохранена. Участники получат актуальные сведения.",
            [("Календарь", "calendar")],
        )
    return None
