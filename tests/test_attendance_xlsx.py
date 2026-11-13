from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import Workbook, load_workbook

from cu_clubs.attendance_xlsx import (
    EXPORT_HEADERS,
    IMPORT_HEADERS,
    export_attendance_xlsx,
    parse_attendance_xlsx,
    template_attendance_xlsx,
)


def make_xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_import_supports_arbitrary_header_order_and_normalizes_values() -> None:
    content = make_xlsx(
        ["Telegram-тег", "ФИО", "Логин в Тайме"],
        [["@guest_1", "  Иванов   Иван Иванович ", "  I.Ivanov@edu.centraluniversity.ru  "]],
    )
    result = parse_attendance_xlsx(content)
    assert result == {
        "rows": [
            {
                "row": 2,
                "full_name": "Иванов Иван Иванович",
                "time_login": "i.ivanov",
                "telegram_tag": "@guest_1",
            }
        ],
        "issues": [],
        "total": 1,
    }


def test_import_tag_column_is_optional_and_blank_rows_are_skipped() -> None:
    result = parse_attendance_xlsx(
        make_xlsx(["ФИО", "Логин в Тайме"], [["Петров Пётр", "p.petrov"], [None, None]])
    )
    assert result["rows"][0]["telegram_tag"] is None
    assert result["total"] == 1
    assert result["issues"] == []


def test_import_reports_duplicate_and_invalid_rows_without_repeating_login() -> None:
    result = parse_attendance_xlsx(
        make_xlsx(
            list(IMPORT_HEADERS),
            [
                ["Сидоров Сидор", "sidorov", ""],
                ["Другой Человек", "SIDOROV", ""],
                ["Одинокий", "bad login", ""],
            ],
        )
    )
    assert [row["row"] for row in result["rows"]] == [2]
    assert [issue["row"] for issue in result["issues"]] == [3, 4]
    assert result["total"] == 1


def test_import_requires_exact_headers_and_rejects_formula_cells() -> None:
    with pytest.raises(ValueError, match="обязательны точные заголовки"):
        parse_attendance_xlsx(make_xlsx(["ФИО", "Логин"], []))
    with pytest.raises(ValueError, match="Формулы"):
        parse_attendance_xlsx(make_xlsx(["ФИО", "Логин в Тайме"], [["=1+1", "user1"]]))


@pytest.mark.parametrize("payload", [b"not xlsx", b""])
def test_import_rejects_non_xlsx(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_attendance_xlsx(payload)


def test_import_rejects_oversized_input() -> None:
    with pytest.raises(ValueError, match="10 МБ"):
        parse_attendance_xlsx(b"x" * (10 * 1024 * 1024 + 1))


def test_import_rejects_excessive_uncompressed_archive() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content", b"0" * (40 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="40 МБ"):
        parse_attendance_xlsx(output.getvalue())


def test_template_has_headers_without_participant_examples() -> None:
    workbook = load_workbook(io.BytesIO(template_attendance_xlsx()), data_only=False)
    sheet = workbook.active
    assert tuple(cell.value for cell in sheet[1]) == IMPORT_HEADERS
    assert sheet.max_row == 1


def test_export_uses_exact_headers_and_writes_formula_looking_values_as_text() -> None:
    content = export_attendance_xlsx(
        [
            {
                "meeting": '=HYPERLINK("https://invalid")',
                "clubs": "+cmd",
                "date": "@text",
                "full_name": "-value",
                "visitor_type": "student",
                "time_login": "=1+1",
            }
        ]
    )
    workbook = load_workbook(io.BytesIO(content), data_only=False)
    sheet = workbook.active
    assert tuple(cell.value for cell in sheet[1]) == EXPORT_HEADERS
    assert sheet.max_row == 2
    for coordinate in ("A2", "B2", "C2", "D2", "F2"):
        assert sheet[coordinate].data_type == "s"
    assert sheet["A2"].value.startswith("=")


@pytest.mark.parametrize("coordinate", ["A6002", "XFD1"])
def test_sparse_far_cell_is_rejected_before_rectangular_iteration(monkeypatch, coordinate):
    from openpyxl.worksheet.worksheet import Worksheet

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ФИО", "Логин в Тайме"])
    sheet[coordinate] = "far"
    stream = io.BytesIO()
    workbook.save(stream)

    def refuse_iteration(*args, **kwargs):
        raise AssertionError("Oversized sheet must be rejected before iter_rows")

    monkeypatch.setattr(Worksheet, "iter_rows", refuse_iteration)
    with pytest.raises(ValueError, match="(5000 строк|64 столбцов)"):
        parse_attendance_xlsx(stream.getvalue())
