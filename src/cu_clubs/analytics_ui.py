"""Private report navigation with Moscow date filters."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import uuid4

from sqlalchemy import select

from cu_clubs.analytics import AnalyticsService, reporting_range
from cu_clubs.event_models import Event, EventClub
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard

PERIODS = {
    "semester_to_date": "С начала семестра",
    "current_semester": "Текущий семестр",
    "previous_semester": "Предыдущий семестр",
    "last_month": "Последний месяц",
    "year": "Год",
    "all": "Всё время",
    "custom": "Свой период",
}


def _screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def report_text(report):
    mean = report["mean_rating"]
    lines = [f"<b>{escape(report.get('title', report.get('name', 'Все клубы')))}</b>"]
    lines.append(
        f"Средняя оценка встречи {mean:.1f} из 5 · {report['rating_count']} оценок".replace(
            ".", ","
        )
        if mean is not None
        else "Пока нет оценок"
    )
    lines.append(
        f"Встреч {report['events']} · посещений {report['visits']} · "
        f"уникальных посетителей {report['unique_visitors']}"
    )
    lines.append(
        f"Записались {report['registrations_at_start']} · "
        f"пришли из записанных {report['attended_registered']} · "
        f"пришли без записи {report['attended_without_registration']} · "
        f"всего посетили {report['visits']}"
    )
    conversion = report["attendance_conversion"]
    lines.append(
        f"Доходимость {conversion:.0%}" if conversion is not None else "Нет предварительных записей"
    )
    lines.append(f"Отменили до начала {report['cancelled_before_start']}")
    if report["feedback_rate"] is not None:
        lines.append(f"Доля посетителей, давших оценку: {report['feedback_rate']:.0%}")
    return "\n\n".join(lines)


def _filter(state):
    nonce = state.payload["nonce"]
    return _screen(
        "Выбери период",
        *[[(label, f"anperiod:{nonce}:{key}")] for key, label in PERIODS.items()],
        [("Назад", "an")],
    )


def _report_screen(state):
    payload = state.payload
    report = payload["report"]
    period_label = payload["period_label"]
    nonce = payload["nonce"]
    return _screen(
        report_text(report) + "\n\nПериод: " + escape(period_label),
        [("Обратная связь", f"anfeedback:{nonce}:0")] if report.get("feedback") else [],
        [("Изменить период", f"anfilter:{nonce}")],
        [("Назад", "an")],
    )


def _check(state, nonce):
    if state.state != "analytics_filter" or state.payload.get("nonce") != nonce:
        raise ValidationError("Кнопка устарела. Открой актуальный экран.")


async def _report(service, actor, state, start, end, period_label):
    payload = state.payload
    report = await (
        service.university(actor, start, end)
        if payload["scope"] == "university"
        else service.club(actor, payload["target"], start, end)
    )
    payload["range_start"] = start.isoformat() if start else None
    payload["range_end"] = end.isoformat() if end else None
    payload["period_label"] = period_label
    payload["nonce"] = uuid4().hex[:10]
    payload["report"] = report
    return _report_screen(state)


async def handle_analytics(session, user_id, state, message, data, settings):
    if message is not None and not isinstance(message, str):
        message = message.text or ""
    state.payload = deepcopy(state.payload)
    service, clubs = AnalyticsService(session), ClubService(session)
    if data == "an":
        admin = await clubs.is_admin(user_id)
        managed = await clubs.managed_clubs(user_id)
        allowed = []
        for club in managed:
            rights = await clubs.permissions(user_id, club["public_id"])
            if rights["is_admin"] or rights["is_leader"]:
                allowed.append(club)
        if not allowed and not admin:
            raise PermissionDenied("Аналитика доступна лидеру клуба и администратору.")
        rows = [[("Все клубы", "anall")]] if admin else []
        rows += [[(club["name"], f"anclub:{club['public_id']}")] for club in allowed[:30]]
        rows.append([("Выбрать встречу", "anevents")])
        rows.append([("Назад", "menu")])
        return _screen("Аналитика", *rows)
    if data == "anall" or data.startswith("anclub:"):
        scope, target = ("university", None) if data == "anall" else ("club", data.split(":", 1)[1])
        if scope == "university":
            if not await clubs.is_admin(user_id):
                raise PermissionDenied("Сводная аналитика доступна администратору.")
        else:
            await service._club_access(user_id, target)
        state.state, state.payload = (
            "analytics_filter",
            {"scope": scope, "target": target, "nonce": uuid4().hex[:10]},
        )
        return _filter(state)
    if data == "anevents":
        admin = await clubs.is_admin(user_id)
        if admin:
            query = (
                select(Event)
                .where(Event.status.in_(["held", "published"]), Event.ends_at <= datetime.now(UTC))
                .order_by(Event.starts_at.desc())
                .limit(30)
            )
        else:
            managed = await clubs.managed_clubs(user_id)
            ids = []
            for club in managed:
                if (await clubs.permissions(user_id, club["public_id"]))["is_leader"]:
                    ids.append(club["id"])
            query = (
                select(Event)
                .where(
                    Event.id.in_(select(EventClub.event_id).where(EventClub.club_id.in_(ids))),
                    Event.status.in_(["held", "published"]),
                    Event.ends_at <= datetime.now(UTC),
                )
                .order_by(Event.starts_at.desc())
                .limit(30)
            )
        events = list(await session.scalars(query))
        return _screen(
            "Выбери прошедшую встречу",
            *[[(event.title or "Без названия", f"anevent:{event.public_id}")] for event in events],
            [("Назад", "an")],
        )
    if data.startswith("anevent:"):
        report = await service.event(user_id, data.split(":", 1)[1])
        state.state, state.payload = (
            "analytics_filter",
            {
                "scope": "event",
                "target": report["event"],
                "nonce": uuid4().hex[:10],
                "report": report,
            },
        )
        return _screen(
            report_text(report),
            [("Обратная связь", f"anfeedback:{state.payload['nonce']}:0")]
            if report.get("feedback")
            else [],
            [("Назад", "an")],
        )
    if data.startswith(("anperiod:", "anfilter:", "anfeedback:", "anreport:")):
        parts = data.split(":")
        _check(state, parts[1])
        payload = state.payload
        # Revoked privileges invalidate retained report buttons as well as fresh queries.
        if payload["scope"] == "university":
            if not await clubs.is_admin(user_id):
                raise PermissionDenied("Нет доступа к аналитике.")
        elif payload["scope"] == "event":
            await service.events._manage(
                user_id, await service.events._event(payload["target"]), leaders_only=True
            )
        else:
            await service._club_access(user_id, payload["target"])
        if parts[0] == "anreport":
            return _report_screen(state)
        if parts[0] == "anfilter":
            return _filter(state)
        if parts[0] == "anfeedback":
            feedback = payload["report"].get("feedback", [])
            page = int(parts[2])
            if not 0 <= page < len(feedback):
                raise ValidationError("Вопрос недоступен.")
            item = feedback[page]
            lines = [f"<b>{escape(item['prompt'])}</b>"]
            # Text answers may be long; paginate each answer independently to fit Telegram.
            answers = item["answers"] or [
                "\n".join(f"{k}: {v}" for k, v in item["distribution"].items()) or "Нет ответов"
            ]
            texts = [
                text[offset : offset + 2200]
                for text in answers
                for offset in range(0, len(text), 2200)
            ]
            answer_index = int(parts[3]) if len(parts) > 3 else 0
            if not 0 <= answer_index < len(texts):
                raise ValidationError("Ответ недоступен.")
            lines += [escape(texts[answer_index]), f"Пропущено: {item['skipped']}"]
            rows = []
            if answer_index + 1 < len(texts):
                rows.append(
                    [("Следующий ответ", f"anfeedback:{parts[1]}:{page}:{answer_index + 1}")]
                )
            if page + 1 < len(feedback):
                rows.append([("Следующий вопрос", f"anfeedback:{parts[1]}:{page + 1}:0")])
            rows.append(
                [
                    (
                        "Назад",
                        "anevent:" + payload["target"]
                        if payload["scope"] == "event"
                        else f"anreport:{payload['nonce']}",
                    )
                ]
            )
            return _screen("\n\n".join(lines), *rows)
        period = parts[2]
        if period == "custom":
            payload["custom"] = True
            return _screen(
                "Введи даты ДД.ММ.ГГГГ ДД.ММ.ГГГГ. Обе даты включаются в отчёт.",
                [("Назад", f"anfilter:{parts[1]}")],
            )
        start, end = reporting_range(period)
        return await _report(service, user_id, state, start, end, PERIODS[period])
    if message and state.state == "analytics_filter" and state.payload.get("custom"):
        try:
            start, last = [datetime.strptime(item, "%d.%m.%Y").date() for item in message.split()]
        except ValueError:
            raise ValidationError("Введи две даты в формате ДД.ММ.ГГГГ ДД.ММ.ГГГГ.") from None
        start, end = reporting_range(
            "custom", starts_on=start, ends_before=last + timedelta(days=1)
        )
        result = await _report(
            service, user_id, state, start, end, f"{start:%d.%m.%Y} — {last:%d.%m.%Y}"
        )
        state.payload["custom"] = False
        return result
    return None
