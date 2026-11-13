"""Safe XLSX import, export, and template helpers for attendance sheets."""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Font

from cu_clubs.identity import normalize_login, normalize_name
from cu_clubs.services import ValidationError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_ROWS = 5000
IMPORT_HEADERS = ("ФИО", "Логин в Тайме", "Telegram-тег")
EXPORT_HEADERS = (
    "встреча",
    "клубы",
    "дата",
    "ФИО",
    "тип посетителя",
    "логин в Тайме",
    "Telegram-тег",
    "предварительная запись",
    "её статус",
    "посещение",
    "источник",
    "время",
    "зарегистрирован в боте на момент отметки",
)
EXPORT_KEYS = (
    "meeting",
    "clubs",
    "date",
    "full_name",
    "visitor_type",
    "time_login",
    "telegram_tag",
    "pre_registration",
    "registration_status",
    "attendance",
    "source",
    "marked_at",
    "bot_registered_at_mark",
)
TAG_PATTERN = re.compile(r"@[A-Za-z0-9_]{1,64}\Z")


def _check_archive(content: bytes) -> None:
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("Файл превышает допустимый размер 10 МБ")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Распакованный файл превышает допустимый размер 40 МБ")
            names = {member.filename.lower() for member in members}
            if any(
                name.endswith("vbaproject.bin") or name.endswith(".bin") and "vba" in name
                for name in names
            ):
                raise ValueError("Файлы с макросами не поддерживаются")
            if any("externallinks/" in name for name in names):
                raise ValueError("Внешние связи в Excel не поддерживаются")
            for member in members:
                if member.filename.lower().endswith(".rels"):
                    try:
                        root = ElementTree.fromstring(archive.read(member))
                    except ElementTree.ParseError as exc:
                        raise ValueError("Повреждён файл связей Excel") from exc
                    if any(rel.attrib.get("TargetMode") == "External" for rel in root):
                        raise ValueError("Внешние связи в Excel не поддерживаются")
    except zipfile.BadZipFile as exc:
        raise ValueError("Нужен файл Excel формата .xlsx") from exc


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_attendance_xlsx(content: bytes) -> dict:
    """Parse the first worksheet, retaining spreadsheet row numbers in issues."""
    _check_archive(content)
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=False, data_only=False)
    except Exception as exc:
        raise ValueError("Не удалось прочитать файл Excel") from exc
    try:
        if not workbook.worksheets:
            raise ValueError("В файле нет листа с ведомостью")
        if workbook.vba_archive is not None:
            raise ValueError("Файлы с макросами не поддерживаются")
        sheet = workbook.worksheets[0]
        # A sparse worksheet can advertise a huge rectangle using a single far
        # cell. Bound dimensions before header reads or rectangular iteration.
        if sheet.max_row - 1 > MAX_ROWS:
            raise ValueError("В файле больше 5000 строк данных")
        if sheet.max_column > 64:
            raise ValueError("В файле больше 64 столбцов")
        headers = [_cell_text(sheet.cell(1, col).value) for col in range(1, sheet.max_column + 1)]
        if "ФИО" not in headers or "Логин в Тайме" not in headers:
            raise ValueError("В первой строке обязательны точные заголовки «ФИО» и «Логин в Тайме»")
        known_headers = [value for value in headers if value in IMPORT_HEADERS]
        if len(known_headers) != len(set(known_headers)):
            raise ValueError("Заголовки столбцов не должны повторяться")
        name_col, login_col = headers.index("ФИО") + 1, headers.index("Логин в Тайме") + 1
        tag_col = headers.index("Telegram-тег") + 1 if "Telegram-тег" in headers else None
        selected = [name_col, login_col] + ([tag_col] if tag_col else [])
        if any(
            sheet.cell(row, col).data_type == "f"
            for row in range(2, sheet.max_row + 1)
            for col in selected
        ):
            raise ValueError("Формулы в данных участников не поддерживаются: замените их текстом")
        rows, issues, seen = [], [], set()
        for row_num in range(2, sheet.max_row + 1):
            name_value = sheet.cell(row_num, name_col).value
            login_value = sheet.cell(row_num, login_col).value
            tag_value = sheet.cell(row_num, tag_col).value if tag_col else None
            if all(
                value is None or not str(value).strip()
                for value in (name_value, login_value, tag_value)
            ):
                continue
            try:
                if not isinstance(name_value, str) or not isinstance(login_value, str):
                    raise ValidationError("ФИО и логин должны быть текстовыми значениями")
                full_name = normalize_name(name_value)
                time_login = normalize_login(login_value)
                tag = _cell_text(tag_value) if tag_value is not None else ""
                if tag and not TAG_PATTERN.fullmatch(tag):
                    raise ValidationError("Укажите Telegram-тег в формате @username")
            except ValidationError as exc:
                issues.append({"row": row_num, "message": str(exc)})
                continue
            if time_login in seen:
                issues.append({"row": row_num, "message": "Повторяется логин в Тайме в этом файле"})
                continue
            seen.add(time_login)
            rows.append(
                {
                    "row": row_num,
                    "full_name": full_name,
                    "time_login": time_login,
                    "telegram_tag": tag or None,
                }
            )
        return {"rows": rows, "issues": issues, "total": len(rows)}
    finally:
        workbook.close()


def _write_text(cell: Cell, value: object) -> None:
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"


def _new_workbook(headers: tuple[str, ...]) -> tuple[Workbook, object]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ведомость"
    for col, header in enumerate(headers, 1):
        _write_text(sheet.cell(1, col), header)
        sheet.cell(1, col).font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{sheet.cell(1, len(headers)).column_letter}1"
    return workbook, sheet


def template_attendance_xlsx() -> bytes:
    workbook, _ = _new_workbook(IMPORT_HEADERS)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def export_attendance_xlsx(rows: list[dict]) -> bytes:
    workbook, sheet = _new_workbook(EXPORT_HEADERS)
    for row_num, row in enumerate(rows, 2):
        for col, key in enumerate(EXPORT_KEYS, 1):
            _write_text(sheet.cell(row_num, col), row.get(key))
    if rows:
        sheet.auto_filter.ref = (
            f"A1:{sheet.cell(1, len(EXPORT_HEADERS)).column_letter}{len(rows) + 1}"
        )
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
