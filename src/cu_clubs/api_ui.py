"""Private key issuance and explicit student consent; credentials are file-only."""

from html import escape
from uuid import uuid4

from aiogram.types import BufferedInputFile

from cu_clubs.access import self_registration_enabled
from cu_clubs.api_models import ApiClient
from cu_clubs.api_security import ApiError
from cu_clubs.api_service import SCOPES, ApiService, as_uuid
from cu_clubs.services import PermissionDenied, ValidationError
from cu_clubs.ui_common import Screen, keyboard


def screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="club-owner")


def state_set(state, name, **payload):
    state.state, state.payload = name, {**payload, "nonce": uuid4().hex[:12]}


def confirm(state, name, nonce):
    if state.state != name or state.payload.get("nonce") != nonce:
        raise ValidationError("Кнопка устарела. Открой текущую интеграцию.")


async def handle_integrations(session, user_id, state, message, data, settings):
    api = ApiService(session, settings)
    try:
        if data.startswith(("apilinkopen:", "apilink:")):
            item = await (
                api.open_link(user_id, data.split(":", 1)[1])
                if data.startswith("apilinkopen:")
                else api.link_view(user_id, data.split(":", 1)[1])
            )
            state_set(state, "apilink_review", link=item["id"])
            if not item.get("eligible", item["verified"]):
                if self_registration_enabled(session):
                    return screen(
                        f"Подключение: {escape(item['name'])}\nКлуб: {escape(item['club'])}"
                        "\n\nЗапись через клубного бота доступна студентам и сотрудникам. "
                        "Абитуриенты и внешние гости участвуют через QR встречи.",
                        [("Личный кабинет", "profile")],
                    )
                return screen(
                    f"Подключение: {escape(item['name'])}\nКлуб: {escape(item['club'])}"
                    "\n\nСначала подтверди студенческую почту. "
                    "Привязка требует отдельного согласия.",
                    [("Подтвердить почту", f"auth:apilink:{item['id']}")],
                    [("Отмена", "profile")],
                )
            return screen(
                f"<b>{escape(item['name'])}</b> · {escape(item['club'])}"
                "\n\nРазрешить этому клубному боту записывать тебя на встречи "
                "и отменять твою запись от твоего имени? "
                "Само подключение не записывает на встречу.",
                [("Разрешить подключение", f"apiapprove:{state.payload['nonce']}")],
                [("Отмена", "profile")],
            )
        if data.startswith("apiapprove:"):
            confirm(state, "apilink_review", data.split(":")[1])
            await api.approve_link(user_id, state.payload["link"])
            state.state, state.payload = "idle", {}
            return screen(
                "Подключение разрешено. Вернись в клубного бота, чтобы продолжить.",
                [("Подключённые боты", "apigrants")],
                [("Назад", "profile")],
            )
        if data == "apigrants":
            items = await api.grants(user_id)
            state.state, state.payload = "idle", {}
            return screen(
                "Подключённые боты" if items else "Подключённых ботов пока нет.",
                *[
                    [(f"{item['name'][:35]} · отключить", f"apigrantd:{item['id']}")]
                    for item in items[:30]
                ],
                [("Назад", "profile")],
            )
        if data.startswith("apigrantd:"):
            grant_id = data.split(":")[1]
            item = next((g for g in await api.grants(user_id) if g["id"] == grant_id), None)
            if not item:
                raise PermissionDenied("Привязка недоступна.")
            state_set(state, "apigrant_revoke", grant=grant_id)
            return screen(
                f"Отключить {escape(item['name'])}? "
                "Бот потеряет возможность действовать от твоего имени.",
                [("Отключить", f"apigrantconfirm:{state.payload['nonce']}")],
                [("Назад", "apigrants")],
            )
        if data.startswith("apigrantconfirm:"):
            confirm(state, "apigrant_revoke", data.split(":")[1])
            await api.revoke_grant(user_id, state.payload["grant"])
            return await handle_integrations(session, user_id, state, None, "apigrants", settings)
        if data.startswith("apiclub:"):
            parts = data.split(":")
            page = max(0, int(parts[2])) if len(parts) == 3 else 0
            items = await api.clients(user_id, parts[1])
            page = min(page, max(0, (len(items) - 1) // 10))
            state.state, state.payload = "idle", {}
            rows = [[("Создать ключ", f"apinew:{parts[1]}")]]
            rows += [
                [
                    (
                        ("Активен · " if i["active"] else "Отозван · ") + i["name"][:40],
                        f"apiclient:{i['id']}",
                    )
                ]
                for i in items[page * 10 : (page + 1) * 10]
            ]
            nav = []
            if page:
                nav.append(("←", f"apiclub:{parts[1]}:{page - 1}"))
            if (page + 1) * 10 < len(items):
                nav.append(("→", f"apiclub:{parts[1]}:{page + 1}"))
            if nav:
                rows.append(nav)
            rows.append([("Назад", f"manageclub:{parts[1]}")])
            return screen(
                "<b>Интеграции клуба</b>\nКаждый ключ ограничен своим клубом. "
                "Персональные действия требуют отдельного согласия студента.",
                *rows,
            )
        if data.startswith("apinew:"):
            club_id = data.split(":")[1]
            await api.clients(user_id, club_id)
            state_set(state, "apiclient_name", club=club_id)
            return screen(
                "Как назвать подключение? Например: «Бот киноклуба».",
                [("Назад", f"apiclub:{club_id}")],
            )
        if message and state.state == "apiclient_name":
            name = (message.text or "").strip()
            if not 2 <= len(name) <= 120:
                return screen(
                    "Название: от 2 до 120 символов. Пришли его ещё раз.",
                    [("Назад", f"apiclub:{state.payload['club']}")],
                )
            state_set(state, "apiclient_scopes", club=state.payload["club"], name=name)
            return screen(
                "Какие права выдать подключению?\n\nЧтение: карточки и изменения."
                "\nПолный доступ: встречи, отметки, ведомости "
                "и запись студентов с их отдельного согласия.",
                [("Только чтение", f"apiscope:{state.payload['nonce']}:read")],
                [("Полный доступ клуба", f"apiscope:{state.payload['nonce']}:full")],
                [("Назад", f"apinew:{state.payload['club']}")],
            )
        if data.startswith("apiscope:"):
            _, nonce, mode = data.split(":")
            confirm(state, "apiclient_scopes", nonce)
            if mode not in {"read", "full"}:
                raise ValidationError("Неизвестный набор прав.")
            state_set(
                state,
                "apiclient_confirm",
                club=state.payload["club"],
                name=state.payload["name"],
                scopes=list(SCOPES) if mode == "full" else ["events:read", "changes:read"],
            )
            return screen(
                f"Создать ключ «{escape(state.payload['name'])}»?\nПрава: "
                + ("полный доступ клуба." if mode == "full" else "только чтение."),
                [("Создать ключ", f"apiissue:{state.payload['nonce']}")],
                [("Назад", f"apinew:{state.payload['club']}")],
            )
        if data.startswith("apiissue:"):
            confirm(state, "apiclient_confirm", data.split(":")[1])
            p = state.payload
            item = await api.issue_client(user_id, p["club"], p["name"], p["scopes"])
            state.state, state.payload = "idle", {}
            result = screen(
                "Ключ создан. Сохрани приложенный файл: ключ выдаётся один раз. "
                "Для замены отзови его и создай новый.",
                [("Интеграции", f"apiclub:{p['club']}")],
            )
            result.document = BufferedInputFile(
                (
                    "API_KEY="
                    + item["api_key"]
                    + "\nCLUB_ID="
                    + item["club_id"]
                    + "\nSCOPES="
                    + ",".join(item["scopes"])
                    + "\n"
                ).encode(),
                filename="cu-clubs-api-key.txt",
            )
            return result
        if data.startswith("apiclient:"):
            row = await session.get(ApiClient, as_uuid(data.split(":")[1]))
            if row is None:
                raise ValidationError("Интеграция не найдена.")
            from cu_clubs.models import Club

            club = await session.get(Club, row.club_id)
            await api._leader(user_id, club)
            state_set(state, "apiclient_revoke", client=row.id.hex, club=club.public_id)
            rows = (
                [[("Отозвать ключ", f"apirevoke:{state.payload['nonce']}")]]
                if row.revoked_at is None
                else []
            )
            return screen(
                f"<b>{escape(row.name)}</b>\nКлюч: {escape(row.key_prefix)}…"
                f"\n{'Активен' if row.revoked_at is None else 'Отозван'}"
                f"\nПрава: {escape(', '.join(row.scopes))}",
                *rows,
                [("Назад", f"apiclub:{club.public_id}")],
            )
        if data.startswith("apirevoke:"):
            confirm(state, "apiclient_revoke", data.split(":")[1])
            item = await api.revoke_client(user_id, state.payload["client"])
            return await handle_integrations(
                session, user_id, state, None, f"apiclub:{item['club_id']}", settings
            )
    except ApiError as error:
        return screen(error.message, [("Назад", "profile")])
    return None
