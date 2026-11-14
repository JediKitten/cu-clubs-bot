"""Durable schedule proposal. All related timestamps commit in one command."""

from datetime import datetime, timedelta
from html import escape
from uuid import uuid4

from cu_clubs.datetime_input import MOSCOW, parse_clock, parse_combined, parse_date
from cu_clubs.services import PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard

TIME_FIELDS = ("starts_at", "ends_at", "registration_deadline")
LABELS = {
    "starts_at": "Начало",
    "ends_at": "Окончание",
    "registration_deadline": "Срок регистрации",
}


def _dt(value):
    return datetime.fromisoformat(value).astimezone(MOSCOW) if value else None


def _steps(payload):
    fields = (
        TIME_FIELDS if payload["wizard"] or payload["field"] == "starts_at" else (payload["field"],)
    )
    return [(field, part) for field in fields for part in ("date", "time")]


def _set_default(payload, field, value):
    if field in payload["explicit"] or value is None:
        return
    payload["dates"][field] = value.date().isoformat()
    payload["clocks"][field] = value.strftime("%H:%M")
    payload["changes"][field] = value.isoformat()


def _defaults(payload, event, start):
    old_start, old_end = _dt(event.get("starts_at")), _dt(event.get("ends_at"))
    old_deadline = _dt(event.get("registration_deadline"))
    try:
        duration = (
            old_end - old_start
            if old_start and old_end
            else timedelta(minutes=event.get("template_duration_minutes") or 120)
        )
        _set_default(payload, "ends_at", start + duration)
        _set_default(
            payload,
            "registration_deadline",
            start - (old_start - old_deadline) if old_start and old_deadline else start,
        )
    except (OverflowError, ValueError):
        raise ValidationError("Дата окончания выходит за допустимый диапазон.") from None


def _validate(payload, event):
    values = {
        field: _dt(payload["changes"].get(field) or event.get(field)) for field in TIME_FIELDS
    }
    if not values["starts_at"] or not values["ends_at"]:
        raise ValidationError("Укажи начало и окончание встречи.")
    if values["ends_at"] <= values["starts_at"]:
        raise ValidationError(
            "Окончание должно быть позже начала. Для ночной встречи выбери следующий день."
        )
    if values["registration_deadline"] and values["registration_deadline"] > values["starts_at"]:
        raise ValidationError("Срок регистрации не может быть позже начала встречи.")
    return values


def prompt(state, event, error=None):
    p = state.payload
    token, cursor = p["token"], p["cursor"]
    steps = _steps(p)
    rows = []
    if cursor >= len(steps):
        values = _validate(p, event)
        text = "<b>Проверь расписание</b>\n" + escape(event["title"] or "Новая встреча")
        for field in TIME_FIELDS:
            value = values[field]
            text += f"\n{LABELS[field]}: " + (
                value.strftime("%d.%m.%Y · %H:%M") if value else "до начала"
            )
        text += "\n\nВремя московское. Изменения ещё не сохранены."
        rows.append([("Сохранить расписание", f"evtime:{token}:save")])
    else:
        field, part = steps[cursor]
        day = p["dates"].get(field)
        clock = p["clocks"].get(field)
        anchor = _dt(p["changes"].get("starts_at") or event.get("starts_at")) or datetime.now(
            MOSCOW
        )
        text = f"<b>{LABELS[field]} · {'дата' if part == 'date' else 'время'}</b>\n"
        if part == "date":
            text += (
                "Пришли дату: 14.11.2026, 14/11, 2026-11-14, 14 ноября или завтра."
                f"\nБез года — {anchor.year}."
            )
            if day:
                label = datetime.fromisoformat(day).strftime("%d.%m.%Y")
                rows.append([(f"Оставить {label}", f"evtime:{token}:keep")])
            elif field != "starts_at":
                rows.append([("В день начала", f"evtime:{token}:same")])
            if field == "ends_at":
                rows.append([("На следующий день после начала", f"evtime:{token}:nextday")])
        else:
            text += (
                f"Дата: {datetime.fromisoformat(day).strftime('%d.%m.%Y')}"
                "\nПришли время: 19:30, 19.30, 1930 или 19.\nВремя московское."
            )
            if clock:
                text += f"\nПредложенное время: {clock}."
                rows.append([(f"Оставить {clock}", f"evtime:{token}:keep")])
        if field == "registration_deadline":
            rows.append([("До начала встречи", f"evtime:{token}:start")])
    if cursor:
        rows.append([("Назад", f"evtime:{token}:back")])
    else:
        rows.append([("Назад", f"evtime:{token}:previous" if p["wizard"] else p["return_to"])])
    rows.append([("Отменить изменение расписания", f"evtime:{token}:cancel")])
    return Screen(
        (escape(error) + "\n\n" if error else "") + text, keyboard(*rows), banner_role="club-owner"
    )


def begin(state, event, field, *, wizard=False):
    if not event["can_manage"] or (
        event["status"] == "published" and not event.get("can_reschedule")
    ):
        raise PermissionDenied("Менять расписание может лидер клуба или администратор.")
    saved = state.payload.get("schedule_draft")
    if (
        wizard
        and saved
        and saved["event"] == event["public_id"]
        and saved["field"] == field
        and saved["version"] == event["version"]
    ):
        state.state, state.payload = (
            "event_schedule",
            {**saved, "cursor": 0, "token": uuid4().hex[:12]},
        )
        return prompt(state, event)
    dates, clocks, changes = {}, {}, {}
    for key in TIME_FIELDS:
        current = _dt(event.get(key))
        if current:
            dates[key], clocks[key], changes[key] = (
                current.date().isoformat(),
                current.strftime("%H:%M"),
                current.isoformat(),
            )
    if not clocks.get("starts_at") and event.get("template_start_time"):
        clocks["starts_at"] = event["template_start_time"]
    state.state, state.payload = (
        "event_schedule",
        {
            "event": event["public_id"],
            "field": "starts_at" if wizard else field,
            "version": event["version"],
            "wizard": wizard,
            "token": uuid4().hex[:12],
            "cursor": TIME_FIELDS.index(field) * 2 if wizard else 0,
            "dates": dates,
            "clocks": clocks,
            "changes": changes,
            "explicit": [],
            "return_to": f"evedit:{event['public_id']}:time",
        },
    )
    return prompt(state, event)


async def handle(service, user_id, state, message, data):
    if state.state != "event_schedule" or (not message and not data.startswith("evtime:")):
        return None
    event = await service.get_event(state.payload["event"], user_id)
    if not event["can_manage"] or (
        event["status"] == "published" and not event.get("can_reschedule")
    ):
        raise PermissionDenied("Нет права менять расписание встречи.")
    p = state.payload.copy()
    # JSON fields must be reassigned, never mutated in place across requests.
    p = {
        **p,
        "changes": dict(p["changes"]),
        "dates": dict(p["dates"]),
        "clocks": dict(p["clocks"]),
        "explicit": list(p["explicit"]),
    }
    action = "input"
    if data.startswith("evtime:"):
        _, token, action = data.split(":", 2)
        if token != p["token"]:
            return prompt(state, event, "Кнопка устарела. Продолжи на текущем экране.")
    from cu_clubs.event_ui import _card, _prompt

    if action == "cancel":
        state.state, state.payload = "idle", {}
        return await _card(service, user_id, event["public_id"])
    if action == "previous" and p["wizard"]:
        previous = {
            "starts_at": "post_registration_text",
            "ends_at": "starts_at",
            "registration_deadline": "ends_at",
        }[p["field"]]
        if previous in TIME_FIELDS:
            return begin(state, event, previous, wizard=True)
        state.state, state.payload = (
            "event_edit",
            {
                "event": event["public_id"],
                "field": previous,
                "version": p["version"],
                "wizard": True,
                "token": uuid4().hex[:10],
                "schedule_draft": p,
            },
        )
        return _prompt(state, event)
    if action == "back":
        p["cursor"] = max(0, p["cursor"] - 1)
        p["token"] = uuid4().hex[:12]
        state.payload = p
        return prompt(state, event)
    try:
        if event["version"] != p["version"]:
            return Screen(
                "Встречу уже изменили. Открой актуальную карточку и повтори правку.",
                keyboard([("Открыть встречу", f"event:{event['public_id']}")]),
                banner_role="club-owner",
            )
        steps = _steps(p)
        if action == "save":
            if p["cursor"] != len(steps):
                return prompt(state, event, "Сначала проверь все шаги расписания.")
            _validate(p, event)
            fields = {key: p["changes"][key] for key in {field for field, _ in steps}}
            method = (
                service.update_event if event["status"] == "published" else service.update_draft
            )
            updated = await method(user_id, event["public_id"], fields, p["version"])
            if p["wizard"]:
                state.state, state.payload = (
                    "event_edit",
                    {
                        "event": updated["public_id"],
                        "field": "venue_type",
                        "version": updated["version"],
                        "wizard": True,
                        "token": uuid4().hex[:10],
                    },
                )
                return _prompt(state, updated)
            state.state, state.payload = "idle", {}
            screen = await _card(service, user_id, updated["public_id"])
            screen.text = "Расписание сохранено.\n\n" + screen.text
            return screen
        if p["cursor"] >= len(steps):
            return prompt(state, event, "Сохрани расписание или вернись к предыдущему шагу.")
        field, part = steps[p["cursor"]]
        raw = (message.text or "").strip() if message else ""
        anchor = _dt(p["changes"].get("starts_at") or event.get("starts_at")) or datetime.now(
            MOSCOW
        )
        if action == "start" and field == "registration_deadline":
            start = _dt(p["changes"].get("starts_at") or event.get("starts_at"))
            if start is None:
                raise ValidationError("Сначала укажи начало встречи.")
            value = start
            p["cursor"] += 2 if part == "date" else 1
        elif part == "date":
            combined = None
            if action == "keep":
                day = datetime.fromisoformat(p["dates"][field]).date()
            elif action in {"same", "nextday"}:
                day = anchor.date() + timedelta(days=int(action == "nextday"))
            elif action == "input":
                try:
                    day = parse_date(raw, anchor=anchor.date())
                except ValidationError as date_error:
                    try:
                        combined = parse_combined(raw, anchor=anchor.date())
                        day = combined.date()
                    except ValidationError:
                        raise date_error from None
            else:
                raise ValidationError("Выбери дату или пришли её сообщением.")
            p["dates"][field] = day.isoformat()
            if field not in p["explicit"]:
                p["explicit"].append(field)
            p["cursor"] += 1
            if combined is None:
                p["token"] = uuid4().hex[:12]
                state.payload = p
                return prompt(state, event)
            value = combined
            p["cursor"] += 1
        else:
            clock = parse_clock(p["clocks"][field] if action == "keep" else raw)
            value = datetime.combine(
                datetime.fromisoformat(p["dates"][field]).date(), clock, MOSCOW
            )
            p["cursor"] += 1
        if field == "starts_at":
            _defaults(p, event, value)
        p["changes"][field] = value.isoformat()
        p["dates"][field], p["clocks"][field] = value.date().isoformat(), value.strftime("%H:%M")
        if field not in p["explicit"]:
            p["explicit"].append(field)
        if field == "ends_at":
            start = _dt(p["changes"].get("starts_at") or event.get("starts_at"))
            if start and value <= start:
                raise ValidationError(
                    "Окончание должно быть позже начала. Можно вернуться и выбрать следующий день."
                )
        if field == "registration_deadline" and value > anchor:
            raise ValidationError("Регистрация должна завершаться не позже начала встречи.")
        if p["cursor"] == len(steps):
            _validate(p, event)
        p["token"] = uuid4().hex[:12]
        state.payload = p
        return prompt(state, event)
    except (ValidationError, OverflowError, ValueError, KeyError) as error:
        return prompt(
            state,
            event,
            str(error)
            if isinstance(error, ValidationError)
            else "Не удалось распознать значение. Проверь дату и время.",
        )
