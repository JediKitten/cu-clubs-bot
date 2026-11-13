"""Exact event copy and hidden participant instructions remain presentation invariants."""

from copy import deepcopy

from cu_clubs import legacy
from cu_clubs.event_ui import event_text


def sample_event():
    return {
        "title": "{{.Name}} <Фильм>",
        "clubs": [{"name": "Кино & клуб"}],
        "description": "Смотрим <b>фильм</b>.",
        "room": "Гашека & 305",
        "venue_details": "",
        "starts_at": "2030-01-02T15:00:00+00:00",
        "ends_at": "2030-01-02T17:00:00+00:00",
        "registration_deadline": "2030-01-02T15:00:00+00:00",
        "capacity": 10,
        "registered_count": 3,
        "registered": False,
        "can_manage": False,
        "visibility": "open",
        "status": "published",
        "post_registration_text": "Секрет {{.Name}} <инструкция>",
        "needs_talk": False,
        "needs_microphones": True,
        "expected_attendance": None,
    }


def test_public_card_keeps_exact_original_block_without_private_instructions():
    event = sample_event()
    assert event_text(event) == (
        "<b>{{.Name}} &lt;Фильм&gt;</b>\n<i>Клуб</i>: Кино &amp; клуб\n\n"
        "<b>Описание:</b>\n<blockquote>Смотрим &lt;b&gt;фильм&lt;/b&gt;.</blockquote>\n"
        "<b>Локация:</b> Гашека &amp; 305\n\n"
        "<b>Начало:</b> 02.01.2030 18:00\n<b>Окончание:</b> 02.01.2030 20:00\n"
        "<b>Завершение регистрации:</b> 02.01.2030 18:00\n"
        "<b>Количество участников:</b> 3/10\n\n\n\nОткрытая встреча"
    )
    assert "Секрет" not in event_text(event)


def test_only_registered_or_manager_see_after_registration_text():
    event = sample_event()
    event["registered"] = True
    rendered = event_text(event)
    assert "Секрет {{.Name}} &lt;инструкция&gt;" in rendered
    assert legacy.text("registered") in rendered
    event["registered"] = False
    event["can_manage"] = True
    assert "Секрет {{.Name}} &lt;инструкция&gt;" in event_text(event)
    event["registered"] = False
    event["can_manage"] = False
    assert "Секрет" not in event_text(event)


def test_cancelled_closed_card_marks_status_without_exposing_private_text():
    event = deepcopy(sample_event())
    event["visibility"], event["status"] = "closed", "cancelled"
    rendered = event_text(event)
    assert rendered.endswith("Закрытая · по приглашению\n<b>Встреча отменена</b>")
    assert "Секрет" not in rendered
