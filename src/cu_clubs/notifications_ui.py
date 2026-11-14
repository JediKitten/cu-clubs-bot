"""Explicit audience → message → preview → confirmation event mailing flow."""

from html import escape
from uuid import uuid4

from cu_clubs import legacy
from cu_clubs.notifications import NotificationsService
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard


def screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def _preview(preview, nonce):
    return screen(
        "<b>Предпросмотр рассылки</b>\n\n"
        + escape(preview["body"])
        + f"\n\nУникальных получателей сейчас: {preview['count']}.\n"
        "Перед отправкой список будет пересчитан.",
        [("Отправить", f"ntc:{nonce}")],
        [
            (
                "Изменить текст",
                f"nta:{preview['event']}:" + ("r" if preview["audience"] == "registered" else "a"),
            )
        ],
        [("Отмена", f"nt:{preview['event']}")],
    )


async def handle_notifications(session, user_id, state, message, data, settings):
    service = NotificationsService(session)
    if data and data.startswith("nt:"):
        event_id = data.split(":", 1)[1]
        event = await service.events._event(event_id)
        await service.events._manage(user_id, event)
        state.state, state.payload = "idle", {}
        return screen(
            "<b>Уведомления встречи</b>",
            [("Написать записавшимся", f"nta:{event_id}:r")],
            [("Написать посетившим", f"nta:{event_id}:a")],
            [("Статус доставки", f"ntq:{event_id}")],
            [("Назад", f"event:{event_id}")],
        )
    if data and data.startswith("nta:"):
        _, event_id, audience = data.split(":")
        if audience not in {"r", "a"}:
            raise ValidationError("Неизвестная аудитория.")
        event = await service.events._event(event_id)
        await service.events._manage(user_id, event)
        state.state = "notification_text"
        state.payload = {
            "event": event_id,
            "audience": "registered" if audience == "r" else "attended",
        }
        key = "event_input_registered_mailing" if audience == "r" else "event_input_visited_mailing"
        return screen(legacy.text(key), [("Отмена", f"nt:{event_id}")])
    if data and data.startswith("ntc:"):
        nonce = data.split(":", 1)[1]
        if state.state != "notification_preview" or state.payload.get("nonce") != nonce:
            raise ValidationError("Кнопка устарела. Открой актуальный предпросмотр.")
        result = await service.confirm(user_id, state.payload["broadcast"])
        event_id = state.payload["event"]
        state.state, state.payload = "idle", {}
        return screen(
            f"Рассылка поставлена в очередь. Получателей: {result['queued']}.",
            [("Статус доставки", f"ntq:{event_id}")],
            [("Назад", f"event:{event_id}")],
        )
    if data and data.startswith(("ntq:", "ntr:")):
        action, event_id = data.split(":", 1)
        if action == "ntr":
            await service.retry(user_id, event_id)
        queue = await service.queue(user_id, event_id)
        labels = {
            "pending": "В очереди",
            "processing": "Отправляются",
            "sent": "Обработаны",
            "failed": "Ошибки",
            "uncertain": "Результат неизвестен",
        }
        text = "<b>Доставка уведомлений</b>\nПоследние 100 заданий:\n" + "\n".join(
            f"{label}: {queue['counts'][key]}" for key, label in labels.items()
        )
        text += (
            "\n\nОбработаны: доставлены или пропущены после проверки актуальности. "
            "Неизвестный результат автоматически не повторяется."
        )
        rows = []
        if queue["retry"]:
            rows.append([("Повторить ошибки", f"ntr:{event_id}")])
        return screen(text, *rows, [("Обновить", f"ntq:{event_id}")], [("Назад", f"nt:{event_id}")])
    if not data and message is not None and state.state == "notification_text":
        body = message.text if hasattr(message, "text") else message
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 500:
            event = await service.events._event(state.payload["event"])
            await service.events._manage(user_id, event)
            return screen(
                legacy.text("invalid_mailing_text"), [("Отмена", f"nt:{event.public_id}")]
            )
        preview = await service.prepare(
            user_id, state.payload["event"], state.payload["audience"], body
        )
        nonce = uuid4().hex[:12]
        state.state = "notification_preview"
        state.payload = {
            "event": preview["event"],
            "broadcast": preview["id"],
            "nonce": nonce,
            "audience": preview["audience"],
        }
        return _preview(preview, nonce)
    if not data and message is not None and state.state == "notification_preview":
        row, event = await service._broadcast(user_id, state.payload["broadcast"])
        count = len(await service._audience(event, row.audience))
        return _preview(
            {"body": row.body, "count": count, "event": event.public_id, "audience": row.audience},
            state.payload["nonce"],
        )
    return None
