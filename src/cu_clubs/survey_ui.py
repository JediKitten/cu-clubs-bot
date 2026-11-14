"""Private survey builder and resumable visitor questionnaire."""

from copy import deepcopy
from html import escape
from uuid import uuid4

from cu_clubs.services import ValidationError
from cu_clubs.surveys import SurveysService, first_question
from cu_clubs.ui_common import Screen, keyboard

KINDS = {
    "text": "Текст",
    "single": "Одиночный выбор",
    "multiple": "Множественный выбор",
    "scale": "Числовая шкала",
}


def _screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def _token():
    return uuid4().hex[:10]


def _rotate(state):
    state.payload["nonce"] = _token()
    return state.payload["nonce"]


def _check(state, prefix, nonce):
    if state.state != prefix or state.payload.get("nonce") != nonce:
        raise ValidationError("Кнопка устарела. Открой актуальный экран.")


def _parent(payload):
    return (
        f"manageclub:{payload['target']}"
        if payload["scope"] == "club"
        else f"event:{payload['target']}"
    )


def _editor(state, notice=""):
    state.state = "survey_edit"
    _rotate(state)
    payload, nonce = state.payload, state.payload["nonce"]
    lines = [
        "<b>Опрос клуба</b>" if payload["scope"] == "club" else "<b>Опрос встречи</b>",
        "1. " + first_question()["prompt"] + " · шкала 1–5",
    ]
    rows = []
    page = min(max(0, payload.get("page", 0)), max(0, (len(payload["questions"]) - 2) // 8))
    payload["page"] = page
    for index, q in enumerate(payload["questions"][1 + page * 8 : 9 + page * 8], 1 + page * 8):
        lines.append(
            f"{index + 1}. {escape(q['prompt'][:120])} · {KINDS[q['kind']]} · "
            + ("обязательный" if q["required"] else "необязательный")
        )
        rows.append([(f"Вопрос {index + 1}", f"suq:{nonce}:{index}")])
    navigation = []
    if page:
        navigation.append(("‹", f"supage:{nonce}:{page - 1}"))
    if 9 + page * 8 < len(payload["questions"]):
        navigation.append(("›", f"supage:{nonce}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    if notice:
        lines.append("\n" + notice)
    rows += [
        [("Добавить вопрос", f"suadd:{nonce}")],
        [("Предпросмотр", f"supreview:{nonce}")],
        [
            (
                "Сохранить для клуба"
                if payload["scope"] == "club"
                else "Сохранить для этой встречи",
                f"susave:{nonce}",
            )
        ],
        [("Назад", _parent(payload))],
    ]
    return _screen("\n\n".join(lines), *rows)


def _question_menu(state, index):
    if not 0 < index < len(state.payload["questions"]):
        raise ValidationError("Первый вопрос нельзя изменить.")
    state.payload["index"] = index
    nonce = _rotate(state)
    q = state.payload["questions"][index]
    rows = [
        [("Изменить", f"suchange:{nonce}:{index}")],
        [
            ("Переместить выше", f"sumove:{nonce}:{index}:-1"),
            ("Переместить ниже", f"sumove:{nonce}:{index}:1"),
        ],
        [("Удалить", f"sudelete:{nonce}:{index}")],
        [("Назад", f"subuilder:{nonce}")],
    ]
    return _screen(escape(q["prompt"]), *rows)


def _types(state):
    nonce = _rotate(state)
    return _screen(
        "Выбери тип дополнительного вопроса",
        *[[(label, f"sutype:{nonce}:{kind}")] for kind, label in KINDS.items()],
        [("Назад", f"subuilder:{nonce}")],
    )


def _input(state):
    stage, nonce = state.payload["stage"], _rotate(state)
    q = state.payload.get("draft", {})
    current = q.get({"prompt": "prompt", "choices": "choices", "scale": "scale_max"}.get(stage))
    text = {
        "prompt": "Введи формулировку вопроса (до 1000 символов)",
        "choices": "Введи от 2 до 30 вариантов, каждый с новой строки",
        "scale": "Введи целую верхнюю границу шкалы N ≥ 2",
        "required": "Дополнительный ответ обязателен?",
    }[stage]
    if current:
        display = (
            "\n".join(c["label"] if isinstance(c, dict) else c for c in current)
            if isinstance(current, list)
            else str(current)
        )
        text += "\n\nСейчас: " + escape(display[:2000])
    rows = []
    if stage == "required":
        rows.append(
            [("Обязательный", f"surequired:{nonce}:1"), ("Необязательный", f"surequired:{nonce}:0")]
        )
    rows.append([("Назад", f"sustepback:{nonce}")])
    rows.append([("Отменить изменение", f"subuilder:{nonce}")])
    return _screen(text, *rows)


def _render_question(state, question, title=""):
    nonce = state.payload["nonce"]
    preview = state.state == "survey_preview"
    rows = []
    kind = question["kind"]
    if kind in {"rating", "scale"}:
        maximum = question["scale_max"]
        if maximum <= 20:
            rows += [
                [(str(i), f"suans:{nonce}:{i}") for i in range(start, min(start + 5, maximum + 1))]
                for start in range(1, maximum + 1, 5)
            ]
    elif kind in {"single", "multiple"}:
        selected = state.payload.get("selected", [])
        for index, choice in enumerate(question["choices"]):
            label = choice["label"] if isinstance(choice, dict) else choice
            rows.append([(("✓ " if index in selected else "") + label, f"suans:{nonce}:{index}")])
        if kind == "multiple":
            rows.append([("Готово", f"sudone:{nonce}")])
    if not question["required"]:
        rows.append([("Пропустить", f"suskip:{nonce}")])
    rows.append([("Назад", f"suexitpreview:{nonce}" if preview else "menu")])
    hint = (
        "\n\nВведи ответ текстом"
        if kind == "text"
        else f"\n\nВыбери значение от 1 до {question['scale_max']}"
        if kind == "scale" and question["scale_max"] > 20
        else ""
    )
    return _screen(
        (escape(title) + "\n\n" if title else "") + escape(question["prompt"]) + hint, *rows
    )


def _response_screen(state, response):
    question = response["next_question"]
    if question is None:
        state.state, state.payload = "idle", {}
        return _screen(
            "Спасибо! Ответы сохранены",
            [("К встрече", f"event:{response['event']}:link")],
            [("Мои опросы", "supending")],
        )
    state.state, state.payload = (
        "survey_answer",
        {
            "event": response["event"],
            "question": question,
            "title": response["title"],
            "selected": [],
            "nonce": _token(),
        },
    )
    return _render_question(state, question, response["title"])


async def handle_surveys(session, user_id, state, message, data, settings):
    if message is not None and not isinstance(message, str):
        message = message.text or ""
    state.payload = deepcopy(state.payload)
    service = SurveysService(session)
    if data.startswith("su:"):
        return _response_screen(state, await service.start_response(user_id, data.split(":", 1)[1]))
    if data == "supending":
        pending = await service.pending(user_id)
        return _screen(
            "Выбери встречу для оценки" if pending else "Пока нет доступных опросов",
            *[[(item["title"], f"su:{item['event']}")] for item in pending[:30]],
            [("Назад", "menu")],
        )
    if data.startswith(("suclub:", "suevent:")):
        scope, target = ("club" if data.startswith("suclub:") else "event"), data.split(":", 1)[1]
        survey = await (
            service.get_club(user_id, target)
            if scope == "club"
            else service.get_event(user_id, target)
        )
        state.payload = {
            "scope": scope,
            "target": target,
            "version": survey["version"],
            "questions": survey["questions"],
        }
        return _editor(state)
    if data.startswith(("suans:", "sudone:", "suskip:")) or (
        message and state.state in {"survey_answer", "survey_preview"}
    ):
        preview = state.state == "survey_preview"
        if state.state not in {"survey_answer", "survey_preview"}:
            raise ValidationError("Открой актуальный опрос.")
        if data:
            _check(state, state.state, data.split(":")[1])
        question = (
            state.payload["question"]
            if not preview
            else state.payload["questions"][state.payload["index"]]
        )
        value = None
        if data.startswith("suans:"):
            index = int(data.split(":")[2])
            if question["kind"] in {"rating", "scale"}:
                value = index
            else:
                if not 0 <= index < len(question["choices"]):
                    raise ValidationError("Вариант недоступен.")
                if question["kind"] == "multiple":
                    selected = list(state.payload.get("selected", []))
                    if index in selected:
                        selected.remove(index)
                    else:
                        selected.append(index)
                    state.payload = {**state.payload, "selected": selected}
                    _rotate(state)
                    return _render_question(state, question)
                choice = question["choices"][index]
                value = choice["id"] if isinstance(choice, dict) else str(index)
        elif data.startswith("sudone:"):
            value = [
                question["choices"][i]["id"] if isinstance(question["choices"][i], dict) else str(i)
                for i in state.payload.get("selected", [])
            ]
        elif message:
            if question["kind"] in {"rating", "scale"}:
                try:
                    value = int(message)
                except ValueError:
                    raise ValidationError(
                        f"Выбери значение от 1 до {question['scale_max']}"
                    ) from None
            else:
                value = message
        if preview:
            # Validate the same answer shapes without creating a response.
            candidate = {
                **question,
                "choices": [
                    {"id": str(i), "label": c} if isinstance(c, str) else c
                    for i, c in enumerate(question["choices"])
                ],
            }
            service._validate_answer(candidate, value)
            state.payload["index"] += 1
            state.payload["selected"] = []
            state.payload["nonce"] = _token()
            if state.payload["index"] == len(state.payload["questions"]):
                state.payload = state.payload["editor"]
                return _editor(state, "Предпросмотр завершён. Ответы не записаны.")
            return _render_question(
                state, state.payload["questions"][state.payload["index"]], "Предпросмотр"
            )
        response = await service.answer(user_id, state.payload["event"], question["id"], value)
        return _response_screen(state, response)
    if data.startswith("suexitpreview:"):
        _check(state, "survey_preview", data.split(":")[1])
        state.payload = state.payload["editor"]
        return _editor(state)
    if data.startswith(
        (
            "supage:",
            "suadd:",
            "suq:",
            "suchange:",
            "sumove:",
            "sudelete:",
            "suremove:",
            "subuilder:",
            "sutype:",
            "surequired:",
            "sustepback:",
            "supreview:",
            "susave:",
        )
    ):
        parts = data.split(":")
        _check(state, "survey_edit", parts[1])
        action, payload = parts[0], state.payload
        nonce = payload["nonce"]
        if action == "supage":
            payload["page"] = int(parts[2])
            return _editor(state)
        if action == "subuilder":
            for key in ("stage", "draft", "edit_index"):
                payload.pop(key, None)
            return _editor(state)
        if action == "suq":
            return _question_menu(state, int(parts[2]))
        if action in {"suadd", "suchange"}:
            payload["edit_index"] = int(parts[2]) if action == "suchange" else None
            if payload["edit_index"] == 0:
                raise ValidationError("Первый вопрос нельзя изменить.")
            payload["draft"] = (
                dict(payload["questions"][payload["edit_index"]]) if payload["edit_index"] else {}
            )
            payload["stage"] = "type"
            return _types(state)
        if action == "sutype":
            if parts[2] not in KINDS:
                raise ValidationError("Неизвестный тип вопроса.")
            draft = payload["draft"]
            payload["draft"] = {
                "kind": parts[2],
                "prompt": draft.get("prompt", ""),
                "required": draft.get("required", False),
                "choices": draft.get("choices", []) if draft.get("kind") == parts[2] else [],
                "scale_max": draft.get("scale_max") if draft.get("kind") == parts[2] else None,
            }
            payload["stage"] = "prompt"
            return _input(state)
        if action == "sustepback":
            stage = payload.get("stage")
            payload["stage"] = {
                "prompt": "type",
                "choices": "prompt",
                "scale": "prompt",
                "required": "choices"
                if payload["draft"]["kind"] in {"single", "multiple"}
                else "scale"
                if payload["draft"]["kind"] == "scale"
                else "prompt",
            }.get(stage, "type")
            return _types(state) if payload["stage"] == "type" else _input(state)
        if action == "surequired":
            draft = {**payload["draft"], "required": parts[2] == "1"}
            from cu_clubs.surveys import validate_questions

            draft = validate_questions([draft])[1]
            if payload.get("edit_index"):
                payload["questions"][payload["edit_index"]] = draft
            else:
                payload["questions"].append(draft)
            for key in ("stage", "draft", "edit_index"):
                payload.pop(key, None)
            return _editor(state)
        if action == "sumove":
            index, direction = int(parts[2]), int(parts[3])
            destination = index + direction
            if direction not in {-1, 1} or not (
                0 < index < len(payload["questions"])
                and 0 < destination < len(payload["questions"])
            ):
                return _editor(state, "Первый вопрос остаётся на месте.")
            payload["questions"][index], payload["questions"][destination] = (
                payload["questions"][destination],
                payload["questions"][index],
            )
            return _editor(state)
        if action in {"sudelete", "suremove"}:
            index = int(parts[2])
            if not 0 < index < len(payload["questions"]):
                raise ValidationError("Первый вопрос нельзя удалить.")
            if action == "sudelete":
                nonce = _rotate(state)
                return _screen(
                    "Удалить дополнительный вопрос?\n\n"
                    + escape(payload["questions"][index]["prompt"]),
                    [("Удалить", f"suremove:{nonce}:{index}")],
                    [("Назад", f"suq:{nonce}:{index}")],
                )
            payload["questions"].pop(index)
            return _editor(state)
        if action == "supreview":
            editor = dict(payload)
            state.state, state.payload = (
                "survey_preview",
                {
                    "editor": editor,
                    "questions": payload["questions"],
                    "index": 0,
                    "nonce": _token(),
                    "selected": [],
                },
            )
            return _render_question(state, state.payload["questions"][0], "Предпросмотр")
        if action == "susave":
            saved = await (
                service.save_club(
                    user_id, payload["target"], payload["questions"], payload["version"]
                )
                if payload["scope"] == "club"
                else service.save_event(
                    user_id, payload["target"], payload["questions"], payload["version"]
                )
            )
            payload["version"], payload["questions"] = saved["version"], saved["questions"]
            return _editor(state, "Опрос сохранён.")
    if (
        message
        and state.state == "survey_edit"
        and state.payload.get("stage") in {"prompt", "choices", "scale"}
    ):
        payload, stage = state.payload, state.payload["stage"]
        draft = dict(payload["draft"])
        if stage == "prompt":
            if not 1 <= len(message.strip()) <= 1000:
                raise ValidationError("Формулировка должна содержать от 1 до 1000 символов.")
            draft["prompt"] = message.strip()
            payload["stage"] = (
                "choices"
                if draft["kind"] in {"single", "multiple"}
                else "scale"
                if draft["kind"] == "scale"
                else "required"
            )
        elif stage == "choices":
            choices = [line.strip() for line in message.splitlines() if line.strip()]
            if (
                not 2 <= len(choices) <= 30
                or any(len(c) > 300 for c in choices)
                or len(set(choices)) != len(choices)
            ):
                raise ValidationError("Нужно от 2 до 30 разных вариантов до 300 символов.")
            draft["choices"], payload["stage"] = choices, "required"
        else:
            try:
                maximum = int(message)
            except ValueError:
                raise ValidationError("Введи целое число N ≥ 2.") from None
            if not 2 <= maximum < 2**31:
                raise ValidationError("Введи целое число N ≥ 2.")
            draft["scale_max"], payload["stage"] = maximum, "required"
        payload["draft"] = draft
        return _input(state)
    return None
