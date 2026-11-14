"""Meeting screens backed by the shared EventService and durable dialogue state."""

from datetime import datetime, timedelta
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram.types import Message

from cu_clubs import event_schedule_ui as schedule
from cu_clubs import legacy
from cu_clubs.events import EventService
from cu_clubs.models import Conversation
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard

MOSCOW = ZoneInfo("Europe/Moscow")
FIELDS = [
    "title",
    "description",
    "post_registration_text",
    "starts_at",
    "ends_at",
    "registration_deadline",
    "venue_type",
    "room",
    "venue_details",
    "capacity",
    "expected_attendance",
    "needs_talk",
    "needs_microphones",
    "visibility",
]
FIELD_CODES = {
    "title": "name",
    "description": "desc",
    "post_registration_text": "after",
    "starts_at": "start",
    "ends_at": "end",
    "registration_deadline": "deadline",
    "venue_type": "venue",
    "room": "room",
    "venue_details": "details",
    "capacity": "limit",
    "expected_attendance": "expected",
    "needs_talk": "talk",
    "needs_microphones": "mic",
    "visibility": "visibility",
}
LABELS = {
    "title": "Название",
    "description": "Описание",
    "post_registration_text": "После записи",
    "starts_at": "Начало",
    "ends_at": "Окончание",
    "registration_deadline": "Срок записи",
    "venue_type": "Тип площадки",
    "room": "Аудитория",
    "venue_details": "Площадка / пожелания",
    "capacity": "Лимит записи",
    "expected_attendance": "Ожидаемое число людей",
    "needs_talk": "Толк",
    "needs_microphones": "Микрофоны",
    "visibility": "Доступность",
}
EDIT_GROUPS = {
    "text": ("Название и описание", ("title", "description", "post_registration_text")),
    "time": ("Дата и время", ("starts_at", "ends_at", "registration_deadline")),
    "place": ("Место проведения", ("venue_type", "room", "venue_details")),
    "people": ("Участники и доступ", ("capacity", "expected_attendance", "visibility")),
    "tech": ("Техника", ("needs_talk", "needs_microphones")),
}


def _edit_route(public_id, field):
    group = next(key for key, (_, fields) in EDIT_GROUPS.items() if field in fields)
    return f"evedit:{public_id}:{group}"


def _date_text(value: str | None) -> str:
    return (
        datetime.fromisoformat(value).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        if value
        else "—"
    )


def event_text(event: dict) -> str:
    """Keep the original public card block; append the new availability fields."""
    location = event.get("room") or event.get("venue_details") or "Аудиторию уточняем"
    after_expression = (
        "{{if .IsRegistered}}{{if .AfterRegistrationText}}<b>Текст после регистрации:</b>\n"
        "<blockquote>{{html .AfterRegistrationText}}</blockquote>{{end}}{{end}}"
    )
    after = ""
    if (
        event.get("registered") or event.get("can_manage") or event.get("can_review")
    ) and event.get("post_registration_text"):
        after = (
            "<b>Текст после регистрации:</b>\n<blockquote>"
            + escape(event["post_registration_text"])
            + "</blockquote>"
        )
    text = legacy._render(
        "event_text",
        {
            "{{.Name}}": escape(event["title"] or "Без названия"),
            "{{html .ClubName}}": escape(", ".join(club["name"] for club in event["clubs"])),
            "{{if .Description}}{{html .Description}}{{else}}<i>Не указано</i>{{end}}": (
                escape(event["description"]) if event.get("description") else "<i>Не указано</i>"
            ),
            "{{html .Location}}": escape(location),
            "{{.StartTime}}": _date_text(event["starts_at"]),
            "{{if .EndTime}}{{.EndTime}}{{else}}<i>Не указано</i>{{end}}": (
                _date_text(event["ends_at"]) if event["ends_at"] else "<i>Не указано</i>"
            ),
            "{{.RegistrationEnd}}": _date_text(
                event["registration_deadline"] or event["starts_at"]
            ),
            "{{if .MaxParticipants}}{{.ParticipantsCount}}/{{.MaxParticipants}}"
            "{{else}}<i>Не ограничено</i>{{end}}": (
                f"{event['registered_count']}/{event['capacity']}"
                if event.get("capacity")
                else "<i>Не ограничено</i>"
            ),
            after_expression: after,
        },
    )
    text += "\n\n" + (
        "Закрытая · по приглашению" if event["visibility"] == "closed" else "Открытая встреча"
    )
    status = {
        "draft": "Черновик",
        "pending_partner": "Ожидает подтверждения партнёра",
        "cancelled": "Встреча отменена",
        "held": "Встреча проведена",
    }.get(event["status"])
    if status:
        text += f"\n<b>{status}</b>"
    if event.get("registered"):
        text += "\n" + legacy.text("registered")
    if event.get("can_manage") or event.get("can_review"):
        text += (
            f"\n\nТолк: {'да' if event['needs_talk'] else 'нет'}"
            f"\nМикрофоны: {'да' if event['needs_microphones'] else 'нет'}"
            f"\nОжидаемое число людей: {event.get('expected_attendance') or 'не указано'}"
        )
    return text


def _screen(text, rows=None, *, role="events") -> Screen:
    return Screen(text, keyboard(*(rows or [[(legacy.text("back"), "events")]])), banner_role=role)


def _reset(state):
    state.state, state.payload = "idle", {}


async def _card(service, user_id, public_id, *, via_link=False) -> Screen:
    event = await service.get_event(public_id, user_id, via_link=via_link)
    suffix = ":link" if via_link else ""
    rows = []
    if event.get("can_attend") and event["status"] in {"published", "held"}:
        rows.append([("Отметить посещение", f"at:{public_id}")])
    if event["status"] == "published":
        if event["registered"]:
            rows.append([(legacy.text("cancel_registration"), f"evunregask:{public_id}{suffix}")])
        else:
            rows.append([(legacy.text("register"), f"evreg:{public_id}{suffix}")])
    if event["can_manage"]:
        if event["status"] == "draft" and (
            event.get("template_start_time") or not event["starts_at"]
        ):
            label = "Выбрать дату" if event.get("template_start_time") else "Указать дату и время"
            rows.append([(label, f"evf:{public_id}:start")])
        if event["status"] in {"draft", "pending_partner", "published"}:
            rows.append([("Изменить поле", f"evedit:{public_id}")])
        if event["status"] == "draft":
            rows.append([("Опубликовать", f"evpublish:{public_id}:{event['version']}")])
            rows.append([("Предложить коллаборацию", f"evpartner:{public_id}")])
        elif event["status"] == "pending_partner":
            rows.append([("Отозвать приглашение", f"evwithdraw:{public_id}:{event['version']}")])
        elif event["status"] == "published":
            rows.append([("Отменить встречу", f"evcancelask:{public_id}:{event['version']}")])
        rows.append([("Сохранить как шаблон", f"evtemplate:{public_id}")])
    if event["status"] in {"published", "held"}:
        rows.append([("Поделиться", f"evshare:{public_id}{suffix}")])
    for club in event["clubs"]:
        rows.append([(club["name"], f"club:{club['public_id']}")])
    rows.append(
        [
            (legacy.text("back"), "events"),
            (legacy.text("main_menu_text").replace("<b>", "").replace("</b>", ""), "menu"),
        ]
    )
    return _screen(event_text(event), rows)


def _prompt(state, event) -> Screen:
    field = state.payload["field"]
    token = state.payload["token"]
    rows = []
    if state.payload.get("wizard") and FIELDS.index(field):
        rows.append([("Назад", f"evprev:{token}")])
    hints = {
        "title": legacy.text("input_event_name"),
        "description": legacy.text("input_event_description"),
        "post_registration_text": legacy.text("input_after_registration_text"),
        "starts_at": legacy.text("input_event_start_time"),
        "ends_at": legacy.text("input_event_end_time"),
        "registration_deadline": legacy._render(
            "input_event_registered_end_time",
            {"{{.MaxRegisteredEndTime}}": _date_text(event["starts_at"])},
        ),
        "capacity": legacy.text("input_max_participants"),
        "expected_attendance": "Сколько людей ожидается? Для отсутствующего значения введи 0.",
        "venue_type": "Где пройдёт встреча?",
        "room": "Укажи аудиторию, если она уже выделена и свободна.",
        "venue_details": "Укажи адрес, инструкцию подключения или пожелания к аудитории.",
        "needs_talk": "Нужен Толк?",
        "needs_microphones": "Нужны микрофоны?",
        "visibility": "Кто увидит встречу?",
    }
    if field in {"needs_talk", "needs_microphones"}:
        rows.append([("Да", f"evchoice:{token}:yes"), ("Нет", f"evchoice:{token}:no")])
    if field == "venue_type":
        rows += [
            [("Кампус ЦУ", f"evchoice:{token}:campus")],
            [
                ("Другая площадка", f"evchoice:{token}:external"),
                ("Онлайн", f"evchoice:{token}:online"),
            ],
        ]
    if field == "visibility":
        rows += [
            [("Открытая", f"evchoice:{token}:open")],
            [("Закрытая по ссылке", f"evchoice:{token}:closed")],
        ]
    if field in {"post_registration_text", "room", "venue_details", "expected_attendance"}:
        rows.append([("Пропустить", f"evchoice:{token}:skip")])
    if field == "registration_deadline":
        rows.append([("До начала встречи", f"evchoice:{token}:default")])
    if field == "starts_at" and event.get("template_start_time") and event["status"] == "draft":
        clock = (
            datetime.fromisoformat(event["starts_at"]).astimezone(MOSCOW).strftime("%H:%M")
            if event.get("starts_at")
            else event["template_start_time"]
        )
        hints[field] += (
            f"\n\nНачало в {clock} по Москве. "
            "Пришли только дату DD.MM.YYYY или укажи новые дату и время полностью."
        )
        if event.get("template_duration_minutes"):
            hints[field] += f"\nДлительность: {event['template_duration_minutes']} мин."
    rows.append(
        [
            (
                "Сохранить черновик и выйти"
                if state.payload.get("wizard")
                else "Выбрать другое поле",
                f"event:{event['public_id']}"
                if state.payload.get("wizard")
                else _edit_route(event["public_id"], field),
            )
        ]
    )
    return _screen(hints[field], rows, role="club-owner")


def _parse(field, raw, event):
    if field in {"starts_at", "ends_at", "registration_deadline"}:
        if field == "registration_deadline" and raw == "default":
            return event["starts_at"]
        try:
            if (
                field == "starts_at"
                and event.get("template_start_time")
                and event["status"] == "draft"
                and len(raw) == 10
            ):
                clock = (
                    datetime.fromisoformat(event["starts_at"]).astimezone(MOSCOW).strftime("%H:%M")
                    if event.get("starts_at")
                    else event["template_start_time"]
                )
                return datetime.strptime(f"{raw} {clock}", "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW)
            return datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW)
        except ValueError:
            raise ValidationError(
                "Введи дату и время в формате DD.MM.YYYY HH:MM по Москве."
            ) from None
    if field in {"capacity", "expected_attendance"}:
        if raw in {"skip", "0"}:
            return None
        if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
            raise ValidationError("Введи целое положительное число или 0.")
        return int(raw)
    if field in {"needs_talk", "needs_microphones"}:
        if raw not in {"yes", "no", "Да", "Нет"}:
            raise ValidationError("Выбери «Да» или «Нет».")
        return raw in {"yes", "Да"}
    if raw == "skip" and field in {"post_registration_text", "room", "venue_details"}:
        return None if field == "room" else ""
    return raw


async def _edit_value(service, user_id, state, raw):
    payload = state.payload
    event = await service.get_event(payload["event"], user_id)
    field = payload["field"]
    value = _parse(field, raw, event)
    method = service.update_event if event["status"] == "published" else service.update_draft
    changes = {field: value}
    if (
        field == "starts_at"
        and len(raw) == 10
        and event.get("template_start_time")
        and event.get("starts_at")
        and event.get("ends_at")
        and event["status"] == "draft"
    ):
        duration = datetime.fromisoformat(event["ends_at"]) - datetime.fromisoformat(
            event["starts_at"]
        )
        try:
            changes["ends_at"] = value + duration
        except OverflowError:
            raise ValidationError("Дата окончания выходит за допустимый диапазон.") from None
    event = await method(user_id, event["public_id"], changes, payload["version"])
    if payload.get("schedule_draft"):
        payload = {
            **payload,
            "schedule_draft": {**payload["schedule_draft"], "version": event["version"]},
        }
        state.payload = payload
    if payload.get("wizard") and FIELDS.index(field) < len(FIELDS) - 1:
        next_field = FIELDS[FIELDS.index(field) + 1]
        if next_field in schedule.TIME_FIELDS:
            return schedule.begin(state, event, next_field, wizard=True)
        state.payload = {
            **payload,
            "field": FIELDS[FIELDS.index(field) + 1],
            "version": event["version"],
            "token": uuid4().hex[:10],
        }
        return _prompt(state, event)
    _reset(state)
    return await _card(service, user_id, event["public_id"])


async def handle_events(
    session, user_id: int, state: Conversation, message: Message | None, data: str, settings
) -> Screen | None:
    service = EventService(session)
    data = {"future:events": "events", "future:mine": "my_events"}.get(data, data)
    if state.state == "event_edit" and state.payload.get("field") in schedule.TIME_FIELDS:
        event = await service.get_event(state.payload["event"], user_id)
        screen = schedule.begin(
            state, event, state.payload["field"], wizard=state.payload.get("wizard", False)
        )
        if data.startswith("evchoice:"):
            return screen
    scheduled = await schedule.handle(service, user_id, state, message, data)
    if scheduled is not None:
        return scheduled
    if message and state.state == "event_edit":
        try:
            return await _edit_value(service, user_id, state, (message.text or "").strip())
        except ValidationError as error:
            event = await service.get_event(state.payload["event"], user_id)
            screen = _prompt(state, event)
            screen.text = escape(str(error)) + "\n\n" + screen.text
            return screen
    if message and state.state == "event_template_name":
        template = await service.save_template(user_id, state.payload["event"], message.text or "")
        event_id = state.payload["event"]
        _reset(state)
        return _screen(
            "Шаблон сохранён: " + escape(template["name"]),
            [[(legacy.text("back"), f"event:{event_id}")]],
        )
    if not data:
        return None
    if data.startswith("evprev:"):
        if state.state != "event_edit" or data.split(":")[1] != state.payload.get("token"):
            raise ValidationError("Кнопка устарела. Открой актуальную карточку.")
        event = await service.get_event(state.payload["event"], user_id)
        previous = FIELDS[max(0, FIELDS.index(state.payload["field"]) - 1)]
        if previous in schedule.TIME_FIELDS:
            return schedule.begin(state, event, previous, wizard=True)
        state.payload = {
            **state.payload,
            "field": previous,
            "version": event["version"],
            "token": uuid4().hex[:10],
        }
        return _prompt(state, event)
    if data.startswith("evchoice:"):
        _, token, value = data.split(":", 2)
        if state.state != "event_edit" or token != state.payload.get("token"):
            raise ValidationError("Эта кнопка устарела. Открой актуальную карточку.")
        return await _edit_value(service, user_id, state, value)
    if data == "my_events":
        events = await service.my_events(user_id)
        rows = [[(e["title"], f"event:{e['public_id']}")] for e in events]
        rows.append([(legacy.text("back"), "menu")])
        return _screen(legacy.text("my_events_list"), rows)
    if data == "events" or data.startswith("eventsweek:"):
        week = int(data.split(":")[1]) if data.startswith("eventsweek:") else 0
        if not -260 <= week <= 260:
            raise ValidationError("Неделя вне доступного диапазона.")
        now = datetime.now(MOSCOW)
        start = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=now.weekday())
            + timedelta(weeks=week)
        )
        end = start + timedelta(days=7)
        events = await service.public_events(start, end)
        rows = [
            [(f"{_date_text(e['starts_at'])} · {e['title']}", f"event:{e['public_id']}")]
            for e in events
        ]
        rows += [
            [
                ("← Неделя", f"eventsweek:{week - 1}"),
                ("Эта неделя", "events"),
                ("Неделя →", f"eventsweek:{week + 1}"),
            ],
            [(legacy.text("back"), "menu")],
        ]
        text = legacy.text("events_list") + f"\n{start:%d.%m}–{end - timedelta(days=1):%d.%m.%Y}"
        if not events:
            text += "\n\nНа этой неделе открытых встреч пока нет."
        return _screen(text, rows)
    if data.startswith("event:"):
        parts = data.split(":")
        _reset(state)
        return await _card(
            service, user_id, parts[1], via_link=len(parts) == 3 and parts[2] == "link"
        )
    if data.startswith("eventnew:"):
        club_id = data.split(":", 1)[1]
        await ClubService(session).authorize(user_id, club_id, "manage_events")
        return _screen(
            "Как создаём встречу?",
            [
                [("С нуля", f"evblank:{club_id}")],
                [("Из шаблона", f"evtemplates:{club_id}")],
                [("Продолжить черновик", f"eventdrafts:{club_id}")],
                [(legacy.text("back"), f"manageclub:{club_id}")],
            ],
            role="club-owner",
        )
    if data.startswith(("evblank:", "evfromtemplate:")):
        key = data.split(":", 1)[1]
        event = await (
            service.from_template(user_id, key)
            if data.startswith("evfromtemplate:")
            else service.create_draft(user_id, key)
        )
        if data.startswith("evfromtemplate:"):
            _reset(state)
            screen = await _card(service, user_id, event["public_id"])
            screen.text = "Черновик создан из шаблона.\n\n" + screen.text
            return screen
        state.state, state.payload = (
            "event_edit",
            {
                "event": event["public_id"],
                "field": "title",
                "version": event["version"],
                "wizard": True,
                "token": uuid4().hex[:10],
            },
        )
        return _prompt(state, event)
    if data.startswith(("eventlist:", "eventdrafts:")):
        club_id = data.split(":", 1)[1]
        events = await service.managed_events(user_id, club_id)
        if data.startswith("eventdrafts:"):
            events = [e for e in events if e["status"] in {"draft", "pending_partner"}]
        rows = [[(e["title"] or "Без названия", f"event:{e['public_id']}")] for e in events]
        rows.append([(legacy.text("back"), f"manageclub:{club_id}")])
        return _screen(
            "Черновики" if data.startswith("eventdrafts:") else legacy.text("events_list"),
            rows,
            role="club-owner",
        )
    if data.startswith("evedit:"):
        parts = data.split(":")
        event = await service.get_event(parts[1], user_id)
        if not event["can_manage"]:
            raise PermissionDenied("Нет права редактировать встречу.")
        _reset(state)
        if len(parts) == 2:
            rows = [
                [(title, f"evedit:{event['public_id']}:{key}")]
                for key, (title, _) in EDIT_GROUPS.items()
            ]
            rows.append([("Назад", f"event:{event['public_id']}")])
            return _screen("Что изменить во встрече?", rows, role="club-owner")
        if parts[2] not in EDIT_GROUPS:
            raise ValidationError("Раздел редактирования не найден.")
        title, fields = EDIT_GROUPS[parts[2]]
        rows = [
            [(LABELS[field], f"evf:{event['public_id']}:{FIELD_CODES[field]}")]
            for field in fields
            if event["status"] != "published"
            or event["can_reschedule"]
            or field not in {"starts_at", "ends_at", "registration_deadline"}
        ]
        rows.append([(legacy.text("back"), f"evedit:{event['public_id']}")])
        return _screen(title + " · выбери поле.", rows, role="club-owner")
    if data.startswith("evf:"):
        _, public_id, code = data.split(":")
        field = next((k for k, v in FIELD_CODES.items() if v == code), None)
        if field is None:
            raise ValidationError("Неизвестное поле встречи.")
        event = await service.get_event(public_id, user_id)
        if not event["can_manage"]:
            raise PermissionDenied("Нет права редактировать встречу.")
        if field in schedule.TIME_FIELDS:
            return schedule.begin(state, event, field)
        state.state, state.payload = (
            "event_edit",
            {
                "event": public_id,
                "field": field,
                "version": event["version"],
                "wizard": False,
                "token": uuid4().hex[:10],
            },
        )
        return _prompt(state, event)
    if data.startswith("evpublish:"):
        _, public_id, version = data.split(":")
        event = await service.publish(user_id, public_id, int(version))
        _reset(state)
        screen = await _card(service, user_id, public_id)
        screen.text = (
            legacy._render("event_created", {"{{.Name}}": escape(event["title"])})
            + "\n\n"
            + screen.text
        )
        return screen
    if data.startswith("evreg:"):
        parts = data.split(":")
        public_id, via_link = parts[1], len(parts) == 3 and parts[2] == "link"
        event = await service.get_event(public_id, user_id, via_link=via_link)
        account = await ClubService(session)._account(user_id)
        if not account or account.person.verified_at is None or account.person.kind != "student":
            resume = f"event:{public_id}" + (":link" if via_link else "")
            return _screen(
                "Подтверди студенческую почту, чтобы записаться.",
                [[("Подтвердить почту", f"auth:{resume}")], [(legacy.text("back"), resume)]],
                role="auth",
            )
        await service.register(user_id, public_id, via_link=via_link)
        return await _card(service, user_id, public_id, via_link=via_link)
    if data.startswith("evunregask:"):
        parts = data.split(":")
        suffix = ":link" if len(parts) == 3 and parts[2] == "link" else ""
        event = await service.get_event(parts[1], user_id, via_link=bool(suffix))
        if not event["registered"]:
            raise ValidationError("Нет активной записи на встречу.")
        token = uuid4().hex[:10]
        state.state, state.payload = (
            "event_unreg_confirm",
            {
                "event": parts[1],
                "token": token,
                "registered_at": event["registered_at"],
                "via_link": bool(suffix),
            },
        )
        return _screen(
            "Освободить твоё место?",
            [
                [("Да, отменить", f"evunreg:{token}")],
                [("Оставить запись", f"event:{parts[1]}{suffix}")],
            ],
        )
    if data.startswith("evunreg:"):
        token = data.split(":")[1]
        if state.state != "event_unreg_confirm" or token != state.payload.get("token"):
            raise ValidationError("Эта кнопка устарела. Открой актуальную карточку встречи.")
        public_id = state.payload["event"]
        await service.cancel_registration(
            user_id, public_id, expected_registered_at=state.payload["registered_at"]
        )
        _reset(state)
        # A closed event remains accessible using the same capability the user already had.
        screen = await _card(service, user_id, public_id, via_link=True)
        screen.text = "Запись отменена.\n\n" + screen.text
        return screen
    if data.startswith("evcancelask:"):
        _, public_id, version = data.split(":")
        event = await service.get_event(public_id, user_id)
        if not event["can_manage"]:
            raise PermissionDenied("Нет права отменять встречу.")
        return _screen(
            f"Отменить встречу для {event['registered_count']} записанных?",
            [
                [("Да, уведомить участников", f"evcancel:{public_id}:{version}")],
                [("Оставить встречу", f"event:{public_id}")],
            ],
            role="club-owner",
        )
    if data.startswith("evcancel:"):
        _, public_id, version = data.split(":")
        await service.cancel_event(user_id, public_id, int(version))
        return await _card(service, user_id, public_id)
    if data.startswith("evpartner:"):
        event = await service.get_event(data.split(":")[1], user_id)
        if not event["can_manage"]:
            raise PermissionDenied("Нет права управлять встречей.")
        clubs = await ClubService(session).list_clubs()
        partners = [c for c in clubs if c["id"] != event["initiator_club_id"]]
        token = uuid4().hex[:10]
        state.state, state.payload = (
            "event_partner",
            {
                "event": event["public_id"],
                "version": event["version"],
                "token": token,
                "partners": [c["public_id"] for c in partners],
            },
        )
        rows = [[(c["name"], f"evinvite:{token}:{index}")] for index, c in enumerate(partners)]
        rows.append([(legacy.text("back"), f"event:{event['public_id']}")])
        return _screen("Выбери клуб-партнёр.", rows, role="club-owner")
    if data.startswith("evinvite:"):
        _, token, index = data.split(":")
        if state.state != "event_partner" or token != state.payload.get("token"):
            raise ValidationError("Эта кнопка устарела. Открой актуальную карточку.")
        if (
            not index.isascii()
            or not index.isdigit()
            or int(index) >= len(state.payload["partners"])
        ):
            raise ValidationError("Клуб-партнёр не найден.")
        public_id = state.payload["event"]
        await service.invite_partner(
            user_id, public_id, state.payload["partners"][int(index)], state.payload["version"]
        )
        _reset(state)
        return await _card(service, user_id, public_id)
    if data.startswith("evwithdraw:"):
        _, public_id, version = data.split(":")
        await service.withdraw_invitation(user_id, public_id, int(version))
        return await _card(service, user_id, public_id)
    if data == "eventinvitations":
        invitations = await service.pending_invitations(user_id)
        rows = [[(e["title"], f"evreview:{e['invitation_id']}")] for e in invitations]
        rows.append([(legacy.text("back"), "menu")])
        return _screen("Входящие коллаборации", rows, role="club-owner")
    if data.startswith("evreview:"):
        invitation_id = data.split(":")[1]
        event = next(
            (
                e
                for e in await service.pending_invitations(user_id)
                if e["invitation_id"] == invitation_id
            ),
            None,
        )
        if not event:
            raise ValidationError("Приглашение устарело. Открой актуальную карточку.")
        return _screen(
            event_text(event),
            [
                [("Принять", f"evaccept:{invitation_id}")],
                [("Отклонить", f"evdecline:{invitation_id}")],
                [(legacy.text("back"), "eventinvitations")],
            ],
            role="club-owner",
        )
    if data.startswith(("evaccept:", "evdecline:")):
        event = await service.respond_invitation(
            user_id, data.split(":")[1], data.startswith("evaccept:")
        )
        if event["status"] == "draft":
            return _screen(
                "Приглашение отклонено.",
                [[(legacy.text("back"), "eventinvitations")]],
                role="club-owner",
            )
        return await _card(service, user_id, event["public_id"])
    if data.startswith("evtemplate:"):
        public_id = data.split(":")[1]
        event = await service.get_event(public_id, user_id)
        if not event["can_manage"]:
            raise PermissionDenied("Нет права сохранять шаблон встречи.")
        state.state, state.payload = "event_template_name", {"event": public_id}
        return _screen(
            "Как назвать шаблон?",
            [[(legacy.text("back"), f"event:{public_id}")]],
            role="club-owner",
        )
    if data.startswith("evtemplates:"):
        club_id = data.split(":")[1]
        templates = await service.templates(user_id, club_id)
        rows = [[(t["name"], f"evfromtemplate:{t['public_id']}")] for t in templates]
        rows.append([(legacy.text("back"), f"eventnew:{club_id}")])
        return _screen("Шаблоны" if templates else "Шаблонов пока нет.", rows, role="club-owner")
    if data.startswith("evshare:"):
        parts = data.split(":")
        event = await service.get_event(
            parts[1], user_id, via_link=len(parts) == 3 and parts[2] == "link"
        )
        url = f"https://t.me/{settings.telegram_bot_username}?start=e_{event['public_id']}"
        return _screen(
            "Постоянная ссылка на встречу:\n" + escape(url),
            [[(legacy.text("back"), f"event:{event['public_id']}:link")]],
        )
    return None
