"""Actual Telegram dispatcher with isolated PostgreSQL and an offline file transport."""

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from aiogram.methods import SendDocument
from aiogram.types import Update
from openpyxl import Workbook, load_workbook
from pydantic import SecretStr
from sqlalchemy import func, select
from test_events import LEADER, published, seed
from test_events import sessions as sessions
from test_runtime import FakeBot

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.config import Settings
from cu_clubs.event_models import Registration
from cu_clubs.telegram import build_dispatcher


@pytest.mark.integration
def test_qr_guest_and_excel_upload_export_through_dispatcher(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        settings = Settings(
            _env_file=None,
            environment="test",
            telegram_bot_username="offline_test_bot",
            verification_secret=SecretStr("offline-attendance-dispatch-secret-32"),
        )
        async with sessions.begin() as session:
            qr = await AttendanceService(session, settings.verification_secret).event_qr(
                LEADER, event["public_id"]
            )

        book = Workbook()
        book.active.append(["ФИО", "Логин в Тайме", "Telegram-тег"])
        book.active.append(["Импорт Тестовый Студент", "dispatch.test", "@dispatch_test"])
        content = BytesIO()
        book.save(content)
        book.close()

        class FileBot(FakeBot):
            async def get_file(self, file_id, **kwargs):
                return SimpleNamespace(file_path="offline.xlsx", file_size=len(content.getvalue()))

            async def download_file(self, file_path, destination, **kwargs):
                destination.write(content.getvalue())
                return destination

        bot = FileBot()
        dispatcher = build_dispatcher(sessions, settings)
        sequence = 0

        async def feed(actor, *, text=None, callback=None, document=None):
            nonlocal sequence
            sequence += 1
            user = {"id": actor, "is_bot": False, "first_name": "Offline User"}
            message = {
                "message_id": sequence,
                "date": 1700000000,
                "chat": {"id": actor, "type": "private"},
                "from": user,
            }
            if callback:
                message["from"] = {"id": bot.id, "is_bot": True, "first_name": "Bot"}
                payload = {
                    "callback_query": {
                        "id": str(sequence),
                        "from": user,
                        "chat_instance": "offline",
                        "message": message,
                        "data": callback,
                    }
                }
            else:
                if text is not None:
                    message["text"] = text
                if document is not None:
                    message["document"] = document
                payload = {"message": message}
            await dispatcher.feed_update(
                bot, Update.model_validate({"update_id": sequence, **payload})
            )

        def button(prefix):
            markup = bot.sent[-1].reply_markup
            return next(
                b.callback_data
                for row in markup.inline_keyboard
                for b in row
                if b.callback_data and b.callback_data.startswith(prefix)
            )

        guest = 91992
        try:
            await feed(guest, text=f"/start a_{qr['token']}")
            await feed(guest, callback=button("atguest:"))
            await feed(guest, text="Гость Тестовый Посетитель")
            await feed(guest, callback=button("atguestconfirm:"))
            async with sessions.begin() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 1
                assert await session.scalar(select(func.count()).select_from(Registration)) == 0
                personal_qr = await AttendanceService(
                    session, settings.verification_secret
                ).get_personal_qr(guest)

            await feed(LEADER, text=f"/start a_{personal_qr['token']}")
            await feed(LEADER, callback=button("atpick:"))
            await feed(LEADER, callback=button("atperson:"))
            async with sessions.begin() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 1

            await feed(LEADER, callback=f"atupload:{event['public_id']}")
            await feed(
                LEADER,
                document={
                    "file_id": "OFFLINE_XLSX",
                    "file_unique_id": "OFFLINE_UNIQUE",
                    "file_name": "attendance.xlsx",
                    "file_size": len(content.getvalue()),
                },
            )
            await feed(LEADER, callback=button("atimportconfirm:"))
            async with sessions.begin() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 2

            await feed(LEADER, callback=f"atexportall:{event['public_id']}")
            assert isinstance(bot.sent[-1], SendDocument)
            exported = load_workbook(BytesIO(bot.sent[-1].document.data))
            try:
                assert exported.active.max_row == 3
            finally:
                exported.close()
        finally:
            await bot.session.close()

    asyncio.run(scenario())
