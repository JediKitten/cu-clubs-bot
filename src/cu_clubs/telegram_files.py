"""Bounded Excel download, outside the business transaction and after access checks."""

from io import BytesIO

from aiogram.exceptions import TelegramAPIError

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_xlsx import MAX_INPUT_BYTES, parse_attendance_xlsx
from cu_clubs.models import Conversation
from cu_clubs.services import ValidationError


class LimitedBuffer(BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_INPUT_BYTES:
            raise ValidationError("Файл превышает 10 МБ")
        return super().write(value)


async def attendance_upload(sessions, bot, user_id, message, settings):
    if not message or not message.document:
        return None
    async with sessions() as session:
        state = await session.get(Conversation, user_id)
        if state is None or state.state != "attendance_upload":
            return None
        service = AttendanceService(session, settings.verification_secret)
        event = await service._event(state.payload["event"])
        await service._authorize(user_id, event)
    document = message.document
    if not (document.file_name or "").lower().endswith(".xlsx"):
        raise ValidationError("Пришли файл .xlsx")
    if (document.file_size or 0) > MAX_INPUT_BYTES:
        raise ValidationError("Файл превышает 10 МБ")
    stream = LimitedBuffer()
    try:
        file = await bot.get_file(document.file_id)
        if not file.file_path or (file.file_size or 0) > MAX_INPUT_BYTES:
            raise ValidationError("Файл недоступен или превышает 10 МБ")
        await bot.download_file(file.file_path, destination=stream)
        return parse_attendance_xlsx(stream.getvalue())
    except (TelegramAPIError, TimeoutError):
        raise ValidationError(
            "Не удалось загрузить файл. Попробуй отправить его ещё раз."
        ) from None
    except ValueError as error:
        raise ValidationError(str(error)) from None
    finally:
        stream.close()
