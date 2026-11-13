"""Bounded Sheets reads and guarded cell writes; no workbook contents enter logs.

Google Sheets provides no cell-level compare-and-swap. Each write is preceded by
a fresh read and followed by verification; a concurrent human edit can still win
between those operations and is reported instead of being called atomic CAS.
"""

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from cu_clubs.integrations.google_oauth import refresh_access_token

API = "https://sheets.googleapis.com/v4/spreadsheets/"
MAX_ROWS = 2000
CELL_FIELDS = (
    "userEnteredValue,effectiveValue,formattedValue,note,userEnteredFormat,"
    "effectiveFormat,hyperlink,textFormatRuns"
)
DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


class SheetsError(RuntimeError):
    """Safe, fixed error code, never a Google response or a private cell value."""


class SheetConflict(SheetsError):
    pass


def cell_text(cell: dict) -> str:
    return cell.get("userEnteredValue", {}).get("stringValue", "")


def signature(cell: dict) -> tuple:
    return cell.get("userEnteredValue", {}), cell.get("note", ""), cell.get("textFormatRuns", [])


def digest_color(cell: dict) -> bool:
    fmt = cell.get("effectiveFormat", {})
    rgb = fmt.get("backgroundColorStyle", {}).get("rgbColor") or fmt.get("backgroundColor", {})
    components = tuple(round(float(rgb.get(part, 0)) * 255) for part in ("red", "green", "blue"))
    return components == (255, 0, 255)


def _contains(bounds: dict, row: int, col: int) -> bool:
    return bounds.get("startRowIndex", 0) <= row < bounds.get(
        "endRowIndex", MAX_ROWS
    ) and bounds.get("startColumnIndex", 0) <= col < bounds.get("endColumnIndex", 12)


@dataclass
class SheetSnapshot:
    year: int
    sheet_id: int
    title: str
    row_count: int
    cells: dict[tuple[int, int], dict] = field(default_factory=dict)
    merges: list[dict] = field(default_factory=list)
    protected: list[dict] = field(default_factory=list)

    def cell(self, row: int, col: int) -> dict:
        return self.cells.get((row, col), {})

    def blocked(self, row: int, col: int) -> bool:
        if not (0 <= row < self.row_count and 1 <= col <= 7):
            return True
        if any(_contains(bounds, row, col) for bounds in self.merges):
            return True
        return any(
            _contains(item.get("range", {}), row, col)
            and not any(_contains(bounds, row, col) for bounds in item.get("unprotectedRanges", []))
            for item in self.protected
        )

    def headers(self) -> list[tuple[int, date]]:
        result = []
        epoch = date(1899, 12, 30)
        for row in range(7, self.row_count):
            values = [
                self.cell(row, col).get("effectiveValue", {}).get("numberValue")
                for col in range(1, 8)
            ]
            if not all(type(value) in (float, int) and value == int(value) for value in values):
                continue
            if not all(values[index] == values[0] + index for index in range(7)):
                continue
            try:
                monday = epoch + timedelta(days=int(values[0]))
            except (OverflowError, ValueError):
                continue
            if monday.weekday() == 0 and any(
                (monday + timedelta(days=i)).year == self.year for i in range(7)
            ):
                result.append((row, monday))
        dates = [monday for _, monday in result]
        if len(dates) != len(set(dates)):
            raise SheetsError("ambiguous_week_headers")
        return result

    def day_rows(self, day: date) -> list[int]:
        headers = self.headers()
        matches = [
            index
            for index, (_, monday) in enumerate(headers)
            if monday <= day <= monday + timedelta(days=6)
        ]
        if len(matches) != 1:
            raise SheetsError("missing_week")
        index = matches[0]
        if index + 1 == len(headers):
            # The long empty tail of the real workbook is not a prepared week.
            raise SheetsError("unbounded_week")
        return list(range(headers[index][0] + 1, headers[index + 1][0]))

    def date_at(self, row: int, col: int) -> date | None:
        headers = self.headers()
        for (start, monday), (end, _) in zip(headers, headers[1:], strict=False):
            if start < row < end and 1 <= col <= 7:
                return monday + timedelta(days=col - 1)
        return None

    def free_slot(self, day: date) -> tuple[int, int]:
        col = day.weekday() + 1
        for row in self.day_rows(day):
            if self.blocked(row, col):
                continue
            if any(cell_text(self.cell(row, c)).strip().lower() in DAYS for c in range(1, 8)):
                continue
            cell = self.cell(row, col)
            if not any(
                cell.get(key)
                for key in ("userEnteredValue", "effectiveValue", "formattedValue", "note")
            ):
                return row, col
        raise SheetsError("no_safe_slot")


@dataclass
class CellPatch:
    snapshot: SheetSnapshot
    row: int
    col: int
    value: str | None
    note: str


class SheetsClient:
    def __init__(self, settings, *, http_client=None, token_provider=None):
        self.settings = settings
        self.book = settings.google_spreadsheet_id
        self.http = http_client
        self.token_provider = token_provider
        self._access_token, self._token_until = None, 0

    async def _token(self):
        if self._access_token and time.monotonic() < self._token_until:
            return self._access_token
        if self.token_provider:
            token = await self.token_provider()
        elif self.settings.google_credentials_path:
            token = await refresh_access_token(self.settings.google_credentials_path)
        else:
            raise SheetsError("google_credentials_missing")
        self._access_token, self._token_until = token, time.monotonic() + 300
        return token

    async def _request(self, method, suffix="", **kwargs):
        if not self.book or not all(char.isalnum() or char in "_-" for char in self.book):
            raise SheetsError("calendar_not_configured")
        token = await self._token()
        try:
            if self.http:
                response = await self.http.request(
                    method,
                    API + self.book + suffix,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                    response = await client.request(
                        method,
                        API + self.book + suffix,
                        headers={"Authorization": f"Bearer {token}"},
                        **kwargs,
                    )
            if response.status_code != 200:
                if response.status_code == 401:
                    self._access_token = None
                raise SheetsError("google_request_failed")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError):
            raise SheetsError("google_request_failed") from None

    async def read_year(self, year: int) -> SheetSnapshot:
        title = f"Calendar {year}"
        metadata = await self._request(
            "GET",
            params={
                "fields": "spreadsheetId,properties(timeZone),"
                "sheets(properties(sheetId,title,gridProperties))"
            },
        )
        sheets = [
            sheet
            for sheet in metadata.get("sheets", [])
            if sheet.get("properties", {}).get("title") == title
        ]
        if len(sheets) != 1:
            raise SheetsError("missing_year")
        props = sheets[0]["properties"]
        count = min(props.get("gridProperties", {}).get("rowCount", 0), MAX_ROWS)
        if count < 9 or props.get("gridProperties", {}).get("columnCount", 0) < 8:
            raise SheetsError("invalid_calendar_layout")
        payload = await self._request(
            "GET",
            params={
                "ranges": f"'{title}'!B1:H{count}",
                "fields": "sheets(properties(sheetId,title),merges,"
                "protectedRanges(range,unprotectedRanges,warningOnly),"
                f"data(startRow,startColumn,rowData(values({CELL_FIELDS}))))",
            },
        )
        matches = [
            sheet
            for sheet in payload.get("sheets", [])
            if sheet.get("properties", {}).get("sheetId") == props["sheetId"]
        ]
        if len(matches) != 1:
            raise SheetsError("calendar_changed")
        sheet = matches[0]
        snapshot = SheetSnapshot(
            year,
            props["sheetId"],
            title,
            count,
            merges=sheet.get("merges", []),
            protected=sheet.get("protectedRanges", []),
        )
        for block in sheet.get("data", []):
            start_row, start_col = block.get("startRow", 0), block.get("startColumn", 0)
            for row, row_data in enumerate(block.get("rowData", []), start_row):
                for col, cell in enumerate(row_data.get("values", []), start_col):
                    if 0 <= row < count and 1 <= col <= 7 and cell:
                        snapshot.cells[row, col] = cell
        snapshot.headers()
        return snapshot

    async def guarded_write(self, patches: list[CellPatch]) -> None:
        if not (
            self.settings.environment == "test"
            and getattr(self.settings, "calendar_allow_writes", False)
            and getattr(self.settings, "calendar_test_spreadsheet_id", None) == self.book
        ):
            raise SheetsError("calendar_writes_not_authorized")
        if not patches or len(patches) > 2:
            raise SheetsError("invalid_calendar_mutation")
        coordinates = {(patch.snapshot.sheet_id, patch.row, patch.col) for patch in patches}
        if len(coordinates) != len(patches):
            raise SheetsError("invalid_calendar_mutation")
        fresh = {
            year: await self.read_year(year) for year in {patch.snapshot.year for patch in patches}
        }
        requests = []
        for patch in patches:
            current = fresh[patch.snapshot.year]
            if current.sheet_id != patch.snapshot.sheet_id or current.blocked(patch.row, patch.col):
                raise SheetConflict("calendar_cell_unavailable")
            if current.date_at(patch.row, patch.col) is None or "formulaValue" in current.cell(
                patch.row, patch.col
            ).get("userEnteredValue", {}):
                raise SheetConflict("calendar_cell_unavailable")
            if signature(current.cell(patch.row, patch.col)) != signature(
                patch.snapshot.cell(patch.row, patch.col)
            ):
                raise SheetConflict("calendar_cell_changed_before_write")
            original = current.cell(patch.row, patch.col)
            if original.get("textFormatRuns") or original.get("userEnteredFormat", {}).get(
                "textFormat", {}
            ).get("link"):
                raise SheetConflict("calendar_rich_text_requires_review")
            values: dict[str, Any] = {"note": patch.note}
            if patch.value is not None:
                values["userEnteredValue"] = {"stringValue": patch.value}
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": current.sheet_id,
                            "startRowIndex": patch.row,
                            "endRowIndex": patch.row + 1,
                            "startColumnIndex": patch.col,
                            "endColumnIndex": patch.col + 1,
                        },
                        "rows": [{"values": [values]}],
                        "fields": "userEnteredValue,note",
                    }
                }
            )
        single = patches[0]
        old_text = cell_text(single.snapshot.cell(single.row, single.col))
        conditional = (
            len(patches) == 1
            and old_text
            and single.value is not None
            and single.note == single.snapshot.cell(single.row, single.col).get("note", "")
        )
        if conditional:
            response = await self._request(
                "POST",
                ":batchUpdate",
                json={
                    "requests": [
                        {
                            "findReplace": {
                                "range": requests[0]["updateCells"]["range"],
                                "find": old_text,
                                "replacement": single.value,
                                "matchCase": True,
                                "matchEntireCell": True,
                                "searchByRegex": False,
                                "includeFormulas": False,
                            }
                        }
                    ],
                    "includeSpreadsheetInResponse": False,
                },
            )
            if (
                response.get("replies", [{}])[0].get("findReplace", {}).get("occurrencesChanged", 0)
                != 1
            ):
                raise SheetConflict("calendar_cell_changed_before_write")
        else:
            await self._request(
                "POST",
                ":batchUpdate",
                json={"requests": requests, "includeSpreadsheetInResponse": False},
            )
        verified = {year: await self.read_year(year) for year in fresh}
        for patch in patches:
            expected = (
                {"stringValue": patch.value} if patch.value is not None else {},
                patch.note,
                [],
            )
            if signature(verified[patch.snapshot.year].cell(patch.row, patch.col)) != expected:
                raise SheetConflict("calendar_write_verification_failed")
