"""Durable, explicitly confirmed profile batches alongside legacy single edits."""

from copy import deepcopy
from html import escape
from uuid import uuid4

from cu_clubs import legacy
from cu_clubs.services import ClubService, ValidationError
from cu_clubs.ui_common import Screen, keyboard

DRAFT_KEY = "club_profile_batch"
STATE_PREFIX = "club_batch_"
EDITABLE = {"name", "description", "channel", "cover", "public_team", "direction"}


def _screen(text, rows, *, photo=None):
    return Screen(text, keyboard(*rows), photo=photo, banner_role="club-owner")


def _save_state(state, draft, view):
    draft = {**draft, "token": uuid4().hex[:12]}
    state.state, state.payload = STATE_PREFIX + view, {DRAFT_KEY: draft}
    return draft


def _value(field, value):
    if field == "cover":
        return "Задана" if value else "Убрана"
    if field == "public_team":
        return (
            "; ".join(
                item["name"] + (" — " + item["role"] if item.get("role") else "") for item in value
            )
            or "Пусто"
        )
    return str(value) if value not in (None, "") else "Не указано"


def _fields(state, draft, presentation, *, notice=""):
    draft = _save_state(state, draft, "fields")
    token = draft["token"]
    rows = [[(label, f"cb:field:{token}:{field}")] for field, label in presentation.fields.items()]
    rows += [
        [("Общий предпросмотр", f"cb:preview:{token}")],
        [("Отменить", f"cb:cancel:{token}")],
    ]
    text = legacy.text("profile_text") + "\n\nИзменения сохранятся после общего подтверждения."
    if draft["changes"]:
        text += "\nПодготовлено полей: " + str(len(draft["changes"]))
        text += "\n" + escape(", ".join(presentation.fields[field] for field in draft["changes"]))
    return _screen((escape(notice) + "\n\n" if notice else "") + text, rows)


def _prompt(state, draft, presentation, *, notice=""):
    draft = _save_state(state, draft, "input")
    field, token = draft["field"], draft["token"]
    text = presentation.hint(field)
    if field in draft.get("raw_inputs", {}):
        text += "\n\nТвой ввод: " + escape(draft["raw_inputs"][field])
    elif field in draft["changes"]:
        text += "\n\nПодготовлено: " + escape(_value(field, draft["changes"][field]))
    rows = []
    if field == "direction":
        rows += [
            [(label, f"cb:choice:{token}:{code}")]
            for code, label in presentation.directions.items()
        ]
        rows += [[("Убрать направление", f"cb:choice:{token}:none")]]
    rows += [[("Назад", f"cb:fields:{token}")], [("Отменить", f"cb:cancel:{token}")]]
    return _screen((escape(notice) + "\n\n" if notice else "") + text, rows)


def _preview(state, draft, presentation, *, notice=""):
    draft = _save_state(state, draft, "preview")
    candidate = {**draft["base"], **draft["changes"]}
    text = "<b>Предпросмотр изменений</b>\n\n" + presentation.card_text(candidate)
    if draft["changes"]:
        text += "\n\n<b>Подготовленные изменения:</b>"
        for field, value in draft["changes"].items():
            text += "\n" + escape(presentation.fields[field]) + ": " + escape(_value(field, value))
    else:
        text += "\n\nИзменений пока нет."
    if draft.get("invalid_fields"):
        text += "\n\n<b>Нужно исправить ввод:</b> " + escape(
            ", ".join(presentation.fields[field] for field in draft["invalid_fields"])
        )
    token = draft["token"]
    rows = [[("Изменить ещё", f"cb:fields:{token}")]]
    if draft.get("requires_rebase"):
        rows.insert(0, [("Сверить изменения", f"cb:rebase:{token}")])
    elif draft["changes"] and not draft.get("invalid_fields"):
        rows.append([("Сохранить изменения", f"cb:save:{token}")])
    if draft.get("field"):
        rows.append([("Назад", f"cb:field:{token}:{draft['field']}")])
    rows.append([("Отменить", f"cb:cancel:{token}")])
    return _screen(
        (escape(notice) + "\n\n" if notice else "") + text,
        rows,
        photo=candidate.get("cover"),
    )


def _conflict(state, draft, latest, presentation):
    draft["requires_rebase"] = True
    draft = _save_state(state, draft, "conflict")
    changed = [
        presentation.fields[field]
        for field in presentation.fields
        if latest[field] != draft["base"][field]
    ]
    text = (
        "<b>Карточка изменилась</b>\nТвои значения сохранены в черновике. "
        "Проверь их вместе с актуальной карточкой перед новым подтверждением."
    )
    if changed:
        text += "\n\nИзменились: " + escape(", ".join(changed))
        for field in presentation.fields:
            if latest[field] == draft["base"][field]:
                continue
            text += "\n\n<b>" + escape(presentation.fields[field]) + "</b>"
            text += "\nСейчас: " + escape(_value(field, latest[field]))
            if field in draft["changes"]:
                text += "\nТвоя правка: " + escape(_value(field, draft["changes"][field]))
    token = draft["token"]
    return _screen(
        text,
        [
            [("Проверить с актуальной карточкой", f"cb:rebase:{token}")],
            [("Вернуться к своим правкам", f"cb:preview:{token}")],
            [("Отменить", f"cb:cancel:{token}")],
        ],
        photo=latest.get("cover"),
    )


async def handle_club_edits(session, user_id, state, message, data, presentation):
    active = state.state.startswith(STATE_PREFIX)
    if not data.startswith("cb:") and not (message and active):
        return None
    service = ClubService(session)
    if data.startswith("cb:start:"):
        public_id = data.split(":", 2)[2]
        await service.authorize(user_id, public_id, "edit_profile")
        club = await service.get_club(public_id)
        draft = state.payload.get(DRAFT_KEY)
        if (
            draft
            and draft.get("club") != public_id
            and (draft.get("changes") or draft.get("raw_inputs"))
        ):
            raise ValidationError(
                "Сначала сохрани или отмени подготовленные изменения другого клуба."
            )
        if not draft or draft.get("club") != public_id:
            draft = {"club": public_id, "base": club, "changes": {}, "raw_inputs": {}}
        return _fields(state, draft, presentation)
    draft = deepcopy(state.payload.get(DRAFT_KEY))
    if not active or not draft:
        raise ValidationError("Кнопка устарела. Открой редактирование профиля заново.")
    await service.authorize(user_id, draft["club"], "edit_profile")
    if data:
        parts = data.split(":")
        if len(parts) not in {3, 4} or parts[2] != draft["token"]:
            raise ValidationError("Кнопка устарела. Продолжи текущее редактирование.")
        action = parts[1]
    else:
        parts, action = [], "input"
    if action == "cancel" and len(parts) == 3:
        state.state, state.payload = "idle", {}
        return _screen(
            "Изменения отменены.", [[(legacy.text("back"), f"editmenu:{draft['club']}")]]
        )
    if action == "field" and len(parts) == 4:
        field = parts[3]
        if field not in EDITABLE or field not in presentation.fields:
            raise ValidationError("Неизвестное поле профиля клуба.")
        draft["field"] = field
        return _prompt(state, draft, presentation)
    if action == "fields" and len(parts) == 3:
        return _fields(state, draft, presentation)
    if action == "preview" and len(parts) == 3:
        return _preview(state, draft, presentation)
    if action == "rebase" and len(parts) == 3:
        latest = await service.get_club(draft["club"])
        draft["base"] = latest
        draft.pop("requires_rebase", None)
        return _preview(
            state,
            draft,
            presentation,
            notice="Проверь все изменения и подтверди сохранение заново.",
        )
    if action == "save" and len(parts) == 3:
        if draft.get("invalid_fields"):
            draft["field"] = next(iter(draft["invalid_fields"]))
            return _prompt(state, draft, presentation, notice="Сначала исправь сохранённый ввод.")
        latest = await service.get_club(draft["club"])
        if latest["version"] != draft["base"]["version"]:
            return _conflict(state, draft, latest, presentation)
        try:
            club = await service.update_profile_fields(
                user_id, draft["club"], draft["changes"], expected_version=draft["base"]["version"]
            )
        except ValidationError as error:
            latest = await service.get_club(draft["club"])
            if latest["version"] != draft["base"]["version"]:
                return _conflict(state, draft, latest, presentation)
            return _preview(state, draft, presentation, notice=str(error))
        state.state, state.payload = "idle", {}
        return _screen(
            "Изменения сохранены.\n\n" + presentation.card_text(club),
            [
                [("Продолжить редактирование", f"cb:start:{club['public_id']}")],
                [(legacy.text("back"), f"manageclub:{club['public_id']}")],
            ],
            photo=club.get("cover"),
        )
    if action in {"input", "choice"}:
        if state.state != STATE_PREFIX + "input":
            return _preview(
                state, draft, presentation, notice="Выбери действие для подготовленных изменений."
            )
        field = draft["field"]
        if action == "choice":
            if (
                len(parts) != 4
                or field != "direction"
                or parts[3] not in {*presentation.directions, "none"}
            ):
                raise ValidationError("Кнопка устарела. Выбери направление заново.")
            value = presentation.directions.get(parts[3])
            draft["raw_inputs"][field] = value or "Удалить"
        else:
            draft["raw_inputs"][field] = message.text or (
                "Фотография получена" if message.photo else ""
            )
            try:
                value = presentation.parse_value(field, message)
            except ValidationError as error:
                draft["invalid_fields"] = {**draft.get("invalid_fields", {}), field: str(error)}
                return _prompt(state, draft, presentation, notice=str(error))
        draft["invalid_fields"] = {
            key: error for key, error in draft.get("invalid_fields", {}).items() if key != field
        }
        draft["changes"][field] = value
        return _preview(state, draft, presentation)
    raise ValidationError("Кнопка устарела. Продолжи текущее редактирование.")
