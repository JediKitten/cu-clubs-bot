"""Template management uses versioned shared commands and durable confirmations."""

import re
from datetime import datetime
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from cu_clubs import legacy
from cu_clubs.events import TEMPLATE_FIELDS, EventService, validate_template_fields
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard

FIELDS = {
    "title": ("Название встречи", "name"),
    "description": ("Описание", "desc"),
    "post_registration_text": ("После записи", "after"),
    "start_time": ("Время начала", "clock"),
    "duration_minutes": ("Длительность", "duration"),
    "venue_type": ("Тип площадки", "venue"),
    "venue_details": ("Площадка / пожелания", "details"),
    "capacity": ("Лимит записи", "limit"),
    "expected_attendance": ("Ожидаемое число людей", "expected"),
    "needs_talk": ("Толк", "talk"),
    "needs_microphones": ("Микрофоны", "mic"),
    "visibility": ("Доступность", "visibility"),
}
PAGE_SIZE = 15


def _screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def _reset(state):
    state.state, state.payload = "idle", {}


def _token():
    return uuid4().hex[:16]


def template_text(template):
    fields = template["fields"]
    lines = [f"<b>Шаблон: {escape(template['name'])}</b>"]
    if template.get("club_name"):
        lines.append(f"Клуб: {escape(template['club_name'])}")
    for field, (label, _) in FIELDS.items():
        value = fields.get(field)
        if field == "venue_type":
            value = {"campus": "Кампус ЦУ", "external": "Другая площадка", "online": "Онлайн"}.get(
                value, "—"
            )
        elif field == "visibility":
            value = "Закрытая по ссылке" if value == "closed" else "Открытая"
        elif field in {"needs_talk", "needs_microphones"}:
            value = "Да" if value else "Нет"
        elif field == "capacity" and value is None:
            value = "Без ограничения"
        elif field == "duration_minutes" and value is not None:
            value = f"{value} мин."
        elif field == "start_time" and value is not None:
            value = f"{value} по Москве"
        lines.append(
            f"\n<b>{label}:</b> {escape(str(value)) if value not in (None, '') else 'Не указано'}"
        )
    lines.append("\nДата, выделенная аудитория, записи и согласие партнёра в шаблон не входят.")
    return "\n".join(lines)


async def _card(service, user_id, state, public_id, *, notice=""):
    template = await service.get_template(user_id, public_id)
    token = _token()
    state.state, state.payload = (
        "template_view",
        {
            "template": public_id,
            "club": template["club_public_id"],
            "version": template["version"],
            "token": token,
        },
    )
    return _screen(
        (notice + "\n\n" if notice else "") + template_text(template),
        [("Создать встречу", f"tmuse:{token}")],
        [("Изменить", f"tmedit:{public_id}"), ("Переименовать", f"tmrename:{public_id}")],
        [("Удалить", f"tmdelask:{public_id}")],
        [(legacy.text("back"), f"tmpls:{template['club_public_id']}")],
    )


async def _current(service, user_id, payload):
    template = await service.get_template(user_id, payload["template"])
    if template["version"] != payload["version"]:
        raise ValidationError("Шаблон изменился. Открой актуальную версию.")
    return template


def _pending(state, expected_state, token):
    if state.state != expected_state or token != state.payload.get("token"):
        raise ValidationError("Эта кнопка устарела. Открой актуальный шаблон.")


def _prompt(state):
    field, token = state.payload["field"], state.payload["token"]
    hints = {
        "title": legacy.text("input_event_name"),
        "description": legacy.text("input_event_description"),
        "post_registration_text": legacy.text("input_after_registration_text"),
        "capacity": legacy.text("input_max_participants"),
        "expected_attendance": "Сколько людей ожидается? Для отсутствующего значения введи 0.",
        "start_time": (
            "Введи время начала в формате HH:MM по Москве. Дату укажешь при создании встречи."
        ),
        "duration_minutes": "Укажи длительность встречи в минутах.",
        "venue_type": "Где пройдёт встреча?",
        "venue_details": "Укажи адрес, инструкцию подключения или пожелания к аудитории.",
        "needs_talk": "Нужен Толк?",
        "needs_microphones": "Нужны микрофоны?",
        "visibility": "Кто увидит встречу?",
    }
    rows = []
    choices = {
        "needs_talk": [("Да", "yes"), ("Нет", "no")],
        "needs_microphones": [("Да", "yes"), ("Нет", "no")],
        "venue_type": [
            ("Кампус ЦУ", "campus"),
            ("Другая площадка", "external"),
            ("Онлайн", "online"),
        ],
        "visibility": [("Открытая", "open"), ("Закрытая по ссылке", "closed")],
    }
    rows += [[(label, f"tmchoice:{token}:{value}")] for label, value in choices.get(field, [])]
    if field in {
        "start_time",
        "duration_minutes",
        "description",
        "post_registration_text",
        "venue_details",
        "expected_attendance",
        "capacity",
    }:
        rows.append([("Убрать", f"tmchoice:{token}:clear")])
    rows.append([(legacy.text("cancel"), f"tmpl:{state.payload['template']}")])
    return _screen(hints[field], *rows)


def _parse(field, raw):
    if raw == "clear":
        return "" if field in {"description", "post_registration_text", "venue_details"} else None
    if field in {"capacity", "expected_attendance", "duration_minutes"}:
        if raw == "0" and field != "duration_minutes":
            return None
        if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
            raise ValidationError("Введи целое положительное число.")
        return int(raw)
    if field in {"needs_talk", "needs_microphones"}:
        if raw not in {"yes", "no", "Да", "Нет"}:
            raise ValidationError("Выбери «Да» или «Нет».")
        return raw in {"yes", "Да"}
    if field == "start_time" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", raw):
        raise ValidationError("Введи время в формате HH:MM по Москве, например 18:30.")
    return raw


async def _edit_preview(service, user_id, state, raw):
    current = await _current(service, user_id, state.payload)
    field = state.payload["field"]
    fields = validate_template_fields({field: _parse(field, raw)}, current["fields"])
    state.state, state.payload = "template_preview", {**state.payload, "value": fields.get(field)}
    preview = {**current, "fields": fields}
    return _screen(
        "<b>Предпросмотр изменений</b>\n\n" + template_text(preview),
        [(legacy.text("confirm"), f"tmsave:{state.payload['token']}")],
        [(legacy.text("cancel"), f"tmpl:{current['public_id']}")],
    )


def _source_fields(event):
    fields = {field: event[field] for field in TEMPLATE_FIELDS if field in event}
    fields["start_time"] = event.get("template_start_time")
    fields["duration_minutes"] = event.get("template_duration_minutes")
    if event.get("starts_at"):
        fields["start_time"] = (
            datetime.fromisoformat(event["starts_at"])
            .astimezone(ZoneInfo("Europe/Moscow"))
            .strftime("%H:%M")
        )
    if event.get("starts_at") and event.get("ends_at"):
        fields["duration_minutes"] = max(
            1,
            int(
                (
                    datetime.fromisoformat(event["ends_at"])
                    - datetime.fromisoformat(event["starts_at"])
                ).total_seconds()
                // 60
            ),
        )
    return validate_template_fields(fields)


async def _source_entry(service, user_id, state, event_id):
    event = await service.get_event(event_id, user_id)
    destinations = await service.template_destinations(user_id, event_id)
    if not destinations:
        raise ValidationError("Нет клуба, в который можно сохранить шаблон этой встречи.")
    state.payload = {
        "event": event_id,
        "source_version": event["version"],
        "token": _token(),
        "destinations": destinations,
        "fields": _source_fields(event),
    }
    if len(destinations) > 1:
        state.state = "template_source_target"
        return _screen(
            "В шаблоны какого клуба сохранить встречу?",
            *[
                [(club["name"][:64], f"tmtarget:{state.payload['token']}:{index}")]
                for index, club in enumerate(destinations)
            ],
            [(legacy.text("cancel"), f"event:{event_id}")],
        )
    state.state = "template_source_name"
    state.payload = {**state.payload, "target": destinations[0]}
    return _screen("Как назвать шаблон?", [(legacy.text("cancel"), f"event:{event_id}")])


async def handle_templates(session, user_id, state, message, data, settings):
    del settings
    service = EventService(session)
    if data.startswith(("tmpls:", "evtemplates:")):
        parts = data.split(":")
        page = int(parts[2]) if len(parts) == 3 else 0
        if page < 0:
            raise ValidationError("Страница недоступна.")
        templates = await service.templates(user_id, parts[1])
        _reset(state)
        start = page * PAGE_SIZE
        if page and start >= len(templates):
            page, start = 0, 0
        rows = [
            [(template["name"][:64], f"tmpl:{template['public_id']}")]
            for template in templates[start : start + PAGE_SIZE]
        ]
        navigation = []
        if page:
            navigation.append(("←", f"tmpls:{parts[1]}:{page - 1}"))
        if start + PAGE_SIZE < len(templates):
            navigation.append(("→", f"tmpls:{parts[1]}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([(legacy.text("back"), f"manageclub:{parts[1]}")])
        return _screen("Шаблоны" if templates else "Шаблонов пока нет.", *rows)
    if data.startswith(("tmpl:", "evfromtemplate:")):
        return await _card(service, user_id, state, data.split(":", 1)[1])
    if data.startswith("tmuse:"):
        _pending(state, "template_view", data.split(":", 1)[1])
        current = await _current(service, user_id, state.payload)
        event = await service.from_template(
            user_id, current["public_id"], expected_version=current["version"]
        )
        _reset(state)
        from cu_clubs.event_ui import _card as event_card

        screen = await event_card(service, user_id, event["public_id"])
        return screen
    if data.startswith("tmedit:"):
        template = await service.get_template(user_id, data.split(":", 1)[1])
        _reset(state)
        return _screen(
            "Изменить шаблон: " + escape(template["name"]),
            *[
                [(label, f"tmfield:{template['public_id']}:{code}")]
                for label, code in FIELDS.values()
            ],
            [(legacy.text("back"), f"tmpl:{template['public_id']}")],
        )
    if data.startswith(("tmfield:", "tmrename:", "tmdelask:")):
        parts = data.split(":")
        if parts[0] == "tmfield" and len(parts) != 3:
            raise ValidationError("Неизвестное поле шаблона.")
        current = await service.get_template(user_id, parts[1])
        state.payload = {
            "template": current["public_id"],
            "club": current["club_public_id"],
            "version": current["version"],
            "token": _token(),
        }
        if parts[0] == "tmfield":
            field = next((field for field, (_, code) in FIELDS.items() if code == parts[2]), None)
            if field is None:
                raise ValidationError("Неизвестное поле шаблона.")
            state.state, state.payload = "template_edit", {**state.payload, "field": field}
            return _prompt(state)
        if parts[0] == "tmrename":
            state.state = "template_rename"
            return _screen(
                "Как назвать шаблон?", [(legacy.text("cancel"), f"tmpl:{current['public_id']}")]
            )
        state.state = "template_delete"
        return _screen(
            "Удалить шаблон «"
            + escape(current["name"])
            + "»?\nСозданные из него встречи сохранятся.",
            [("Да, удалить", f"tmdelete:{state.payload['token']}")],
            [(legacy.text("cancel"), f"tmpl:{current['public_id']}")],
        )
    if data.startswith("tmchoice:"):
        _, token, raw = data.split(":", 2)
        _pending(state, "template_edit", token)
        return await _edit_preview(service, user_id, state, raw)
    if message and state.state == "template_edit":
        return await _edit_preview(service, user_id, state, (message.text or "").strip())
    if message and state.state == "template_rename":
        current = await _current(service, user_id, state.payload)
        name = service._template_name(message.text or "")
        state.state, state.payload = (
            "template_preview",
            {**state.payload, "field": "name", "value": name},
        )
        return _screen(
            "<b>Предпросмотр изменений</b>\n\n" + template_text({**current, "name": name}),
            [(legacy.text("confirm"), f"tmsave:{state.payload['token']}")],
            [(legacy.text("cancel"), f"tmpl:{current['public_id']}")],
        )
    if data.startswith("tmsave:"):
        _pending(state, "template_preview", data.split(":", 1)[1])
        current = await _current(service, user_id, state.payload)
        p = state.payload
        if p["field"] == "name":
            await service.rename_template(
                user_id, current["public_id"], p["value"], expected_version=p["version"]
            )
        else:
            await service.update_template(
                user_id,
                current["public_id"],
                {p["field"]: p["value"]},
                expected_version=p["version"],
            )
        return await _card(service, user_id, state, current["public_id"], notice="Шаблон сохранён.")
    if data.startswith("tmdelete:"):
        _pending(state, "template_delete", data.split(":", 1)[1])
        current = await _current(service, user_id, state.payload)
        await service.delete_template(
            user_id, current["public_id"], expected_version=current["version"]
        )
        _reset(state)
        return _screen(
            "Шаблон удалён. Созданные из него встречи сохранены.",
            [(legacy.text("back"), f"tmpls:{current['club_public_id']}")],
        )
    if data.startswith("evtemplate:"):
        return await _source_entry(service, user_id, state, data.split(":", 1)[1])
    if data.startswith("tmtarget:"):
        _, token, index = data.split(":", 2)
        _pending(state, "template_source_target", token)
        if (
            not index.isascii()
            or not index.isdigit()
            or int(index) >= len(state.payload["destinations"])
        ):
            raise ValidationError("Клуб недоступен.")
        target = state.payload["destinations"][int(index)]
        current_targets = await service.template_destinations(user_id, state.payload["event"])
        if target["public_id"] not in {club["public_id"] for club in current_targets}:
            raise ValidationError("Клуб недоступен.")
        source = await service.get_event(state.payload["event"], user_id)
        if source["version"] != state.payload["source_version"]:
            raise ValidationError("Встреча изменилась. Открой актуальную карточку.")
        state.state, state.payload = (
            "template_source_name",
            {**state.payload, "target": target},
        )
        return _screen(
            "Как назвать шаблон?", [(legacy.text("cancel"), f"event:{state.payload['event']}")]
        )
    if message and state.state in {"template_source_name", "event_template_name"}:
        if state.state == "event_template_name":
            source = state.payload["event"]
            screen = await _source_entry(service, user_id, state, source)
            if state.state == "template_source_target":
                return screen
        event = await service.get_event(state.payload["event"], user_id)
        if event["version"] != state.payload["source_version"]:
            raise ValidationError(
                "Встреча изменилась. Открой актуальную карточку и сохрани шаблон заново."
            )
        destinations = await service.template_destinations(user_id, state.payload["event"])
        if state.payload["target"]["public_id"] not in {club["public_id"] for club in destinations}:
            raise ValidationError("Нет права сохранять шаблон в выбранном клубе.")
        name = service._template_name(message.text or "")
        state.state, state.payload = "template_source_preview", {**state.payload, "name": name}
        preview = {
            "name": name,
            "fields": state.payload["fields"],
            "club_name": state.payload["target"]["name"],
        }
        return _screen(
            "<b>Предпросмотр шаблона</b>\n\n" + template_text(preview),
            [(legacy.text("confirm"), f"tmsave_source:{state.payload['token']}")],
            [(legacy.text("cancel"), f"event:{event['public_id']}")],
        )
    if data.startswith("tmsave_source:"):
        _pending(state, "template_source_preview", data.split(":", 1)[1])
        p = state.payload
        template = await service.save_template(
            user_id,
            p["event"],
            p["name"],
            expected_event_version=p["source_version"],
            target_club_public_id=p["target"]["public_id"],
        )
        return await _card(
            service, user_id, state, template["public_id"], notice="Шаблон сохранён."
        )
    return None
