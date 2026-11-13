"""Calendar adapter checks against synthetic Sheets data and isolated PostgreSQL."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from test_events import ADMIN, LEADER, published, seed
from test_events import sessions as sessions

from cu_clubs.calendar import CalendarSync, marker
from cu_clubs.calendar_models import CalendarBinding
from cu_clubs.event_models import Event
from cu_clubs.events import EventService
from cu_clubs.integrations.sheets import (
    CellPatch,
    SheetsClient,
    SheetsError,
    SheetSnapshot,
    cell_text,
    digest_color,
)
from cu_clubs.models import Outbox


def serial(day):
    return (day - date(1899, 12, 30)).days


def grid(year=2026, *, weeks=None):
    """Small synthetic sheet: irregular week heights, no private source data."""
    snap = SheetSnapshot(year, 41, f"Calendar {year}", 60)
    weeks = weeks or [(8, date(year, 1, 5)), (13, date(year, 1, 12)), (22, date(year, 1, 19))]
    for row, monday in weeks:
        for col in range(1, 8):
            snap.cells[row, col] = {
                "effectiveValue": {"numberValue": serial(monday + timedelta(days=col - 1))}
            }
    return snap


class FakeSheets:
    def __init__(self, snapshots=None):
        self.snapshots = snapshots or {2026: grid()}
        self.writes = []

    async def read_year(self, year):
        if year not in self.snapshots:
            raise SheetsError("missing_year")
        return self.snapshots[year]

    async def guarded_write(self, patches):
        self.writes.append(patches)
        for patch in patches:
            cell = patch.snapshot.cells.setdefault((patch.row, patch.col), {})
            if patch.value is None:
                cell.pop("userEnteredValue", None)
            else:
                cell["userEnteredValue"] = {"stringValue": patch.value}
            if patch.note:
                cell["note"] = patch.note
            else:
                cell.pop("note", None)


def test_snapshot_date_headers_variable_blocks_and_slot_guards():
    snap = grid(weeks=[(8, date(2026, 1, 5)), (12, date(2026, 1, 12)), (21, date(2026, 1, 19))])
    assert snap.headers() == [
        (8, date(2026, 1, 5)),
        (12, date(2026, 1, 12)),
        (21, date(2026, 1, 19)),
    ]
    assert snap.date_at(15, 2) == date(2026, 1, 13)
    target_day = date(2026, 1, 13)
    # Ignore weekday labels, occupied text, formulas, merged and protected cells.
    snap.cells[13, 2] = {"userEnteredValue": {"stringValue": "Tuesday"}}
    snap.cells[14, 2] = {"userEnteredValue": {"stringValue": "Existing"}}
    snap.cells[15, 2] = {"userEnteredValue": {"formulaValue": "=1"}}
    snap.merges = [
        {"startRowIndex": 16, "endRowIndex": 17, "startColumnIndex": 2, "endColumnIndex": 3}
    ]
    snap.protected = [
        {
            "range": {
                "startRowIndex": 17,
                "endRowIndex": 18,
                "startColumnIndex": 2,
                "endColumnIndex": 3,
            }
        }
    ]
    assert snap.free_slot(target_day) == (18, 2)
    with pytest.raises(SheetsError, match="missing_week"):
        snap.day_rows(date(2026, 3, 1))
    tail = grid(weeks=[(8, date(2026, 1, 5))])
    with pytest.raises(SheetsError, match="unbounded_week"):
        tail.free_slot(date(2026, 1, 6))


def test_sheets_year_payload_uses_exact_tab_and_maps_effective_headers():
    class PayloadClient(SheetsClient):
        def __init__(self):
            self.settings = type("S", (), {"google_spreadsheet_id": "fake"})()
            self.book = "fake"
            self.calls = []

        async def _request(self, method, suffix="", **kwargs):
            self.calls.append((method, suffix, kwargs))
            if len(self.calls) == 1:
                return {
                    "sheets": [
                        {
                            "properties": {
                                "sheetId": 41,
                                "title": "Calendar 2026",
                                "gridProperties": {"rowCount": 60, "columnCount": 12},
                            }
                        }
                    ]
                }
            return {
                "sheets": [
                    {
                        "properties": {"sheetId": 41, "title": "Calendar 2026"},
                        "data": [
                            {
                                "startRow": 7,
                                "startColumn": 1,
                                "rowData": [
                                    {
                                        "values": [
                                            {
                                                "effectiveValue": {
                                                    "numberValue": serial(
                                                        date(2026, 1, 5) + timedelta(days=i)
                                                    )
                                                }
                                            }
                                            for i in range(7)
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

    async def run():
        client = PayloadClient()
        snap = await client.read_year(2026)
        assert snap.headers() == [(7, date(2026, 1, 5))]
        assert client.calls[1][2]["params"]["ranges"] == "'Calendar 2026'!B1:H60"
        assert "effectiveFormat" in client.calls[1][2]["params"]["fields"]

    asyncio.run(run())


def test_note_markers_and_digest_color_preserve_human_note():
    note = "Note by organizer"
    marked = marker("a" * 32)
    from cu_clubs.calendar import marked_note, unmark_note

    assert marked_note(note, "a" * 32) == note + "\n" + marked
    assert unmark_note(marked_note(note, "a" * 32), "a" * 32) == note
    assert digest_color(
        {"effectiveFormat": {"backgroundColor": {"red": 1.0, "green": 0, "blue": 1.0}}}
    )
    assert not digest_color(
        {"effectiveFormat": {"backgroundColor": {"red": 1, "green": 0, "blue": 0}}}
    )


def test_guarded_write_payload_uses_conditional_find_replace():
    class Capturing(SheetsClient):
        def __init__(self, snapshot):
            self.settings = type(
                "S",
                (),
                {
                    "environment": "test",
                    "calendar_allow_writes": True,
                    "calendar_test_spreadsheet_id": "fake",
                    "google_spreadsheet_id": "fake",
                },
            )()
            self.book, self.snapshot, self.posts = "fake", snapshot, []

        async def read_year(self, year):
            return self.snapshot

        async def _request(self, method, suffix="", **kwargs):
            if method == "POST":
                self.posts.append(kwargs["json"])
                request = kwargs["json"]["requests"][0].get("findReplace")
                if request:
                    bounds = request["range"]
                    self.snapshot.cells[bounds["startRowIndex"], bounds["startColumnIndex"]][
                        "userEnteredValue"
                    ] = {"stringValue": request["replacement"]}
                return {"replies": [{"findReplace": {"occurrencesChanged": 1}}]}

    async def run():
        snap = grid(weeks=[(8, date(2026, 1, 5)), (13, date(2026, 1, 12))])
        snap.cells[9, 1] = {"userEnteredValue": {"stringValue": "old"}, "note": "keep"}
        client = Capturing(snap)
        await client.guarded_write([CellPatch(snap, 9, 1, "new", "keep")])
        request = client.posts[0]["requests"][0]["findReplace"]
        assert request["find"] == "old" and request["replacement"] == "new"
        assert request["matchEntireCell"] and request["matchCase"]

    asyncio.run(run())


def test_calendar_sync_create_repeat_room_reverse_conflict_move_and_digest(sessions):
    async def run():
        await seed(sessions)
        event = await published(
            sessions,
            starts_at=datetime(2026, 1, 6, 12, tzinfo=__import__("datetime").UTC),
            ends_at=datetime(2026, 1, 6, 14, tzinfo=__import__("datetime").UTC),
        )
        fake = FakeSheets()
        settings = type(
            "S",
            (),
            {
                "google_spreadsheet_id": "synthetic",
                "calendar_sync_enabled": True,
                "telegram_bot_username": "testbot",
            },
        )()
        sync = CalendarSync(sessions, settings, client=fake)
        assert (await sync.tick())["synced"] == 1
        assert len(fake.writes) == 1
        async with sessions() as session:
            binding = await session.scalar(select(CalendarBinding))
            assert binding.status == "synced" and binding.baseline_date == date(2026, 1, 6)
            coord = (binding.row_index, binding.column_index)
            assert marker(event["public_id"]) in fake.snapshots[2026].cell(*coord)["note"]
        assert (await sync.tick())["checked"] == 0
        # Human room edit is imported when the local room is unchanged.
        cell = fake.snapshots[2026].cell(*coord)
        cell["userEnteredValue"]["stringValue"] = cell_text(cell).replace(
            "Аудитория: Аудиторию уточняем", "Аудитория: B101"
        )
        async with sessions.begin() as session:
            binding = await session.scalar(select(CalendarBinding))
            binding.next_sync_at = datetime.now(__import__("datetime").UTC) - timedelta(seconds=1)
        assert (await sync.tick())["synced"] == 1
        async with sessions() as session:
            current = await session.scalar(select(Event))
            assert current.room == "B101"
            binding = await session.scalar(select(CalendarBinding))
            assert binding.baseline_room == "B101"
        # A simultaneous local and sheet room edit becomes a conflict.
        async with sessions.begin() as session:
            current = await session.scalar(select(Event))
            await EventService(session).update_event(
                LEADER, event["public_id"], {"room": "B202"}, current.version
            )
        cell["userEnteredValue"]["stringValue"] = cell_text(cell).replace(
            "Аудитория: B101", "Аудитория: C303"
        )
        async with sessions.begin() as session:
            binding = await session.scalar(select(CalendarBinding))
            binding.next_sync_at = datetime.now(__import__("datetime").UTC) - timedelta(seconds=1)
        assert (await sync.tick())["conflicts"] == 1
        async with sessions() as session:
            assert (await session.scalar(select(CalendarBinding))).conflict["kind"] == "room"
        # Move the event to a prepared date; the old cell is cleared and marker removed.
        # Resolve room conflict to local before moving.
        async with sessions.begin() as session:
            binding = await session.scalar(select(CalendarBinding))
            binding.resolution = {**binding.conflict, "choice": "local", "actor": ADMIN}
            binding.next_sync_at = datetime.now(__import__("datetime").UTC) - timedelta(seconds=1)
        assert (await sync.tick())["synced"] == 1
        # A later highlight produces one notice; initialization never sends history.
        cell["effectiveFormat"] = {"backgroundColor": {"red": 1, "green": 0, "blue": 1}}
        async with sessions.begin() as session:
            (await session.scalar(select(CalendarBinding))).next_sync_at = datetime.now(
                __import__("datetime").UTC
            ) - timedelta(seconds=1)
        assert (await sync.tick())["synced"] == 1
        async with sessions.begin() as session:
            current = await session.scalar(select(Event))
            await EventService(session).update_event(
                LEADER,
                event["public_id"],
                {
                    "starts_at": datetime(2026, 1, 13, 12, tzinfo=__import__("datetime").UTC),
                    "ends_at": datetime(2026, 1, 13, 14, tzinfo=__import__("datetime").UTC),
                },
                current.version,
            )
        assert (await sync.tick())["synced"] == 1
        async with sessions() as session:
            binding = await session.scalar(select(CalendarBinding))
            assert binding.baseline_date == date(2026, 1, 13)
            assert binding.digest_weeks == ["2026-01-05"]
            notices = list(
                await session.scalars(select(Outbox).where(Outbox.kind == "calendar.digest.notice"))
            )
            assert notices
        assert marker(event["public_id"]) not in fake.snapshots[2026].cell(*coord).get("note", "")

    asyncio.run(run())


def test_calendar_sync_missing_week_and_provider_error_are_recorded(sessions):
    async def run():
        await seed(sessions)
        await published(
            sessions,
            starts_at=datetime(2026, 2, 3, 12, tzinfo=__import__("datetime").UTC),
            ends_at=datetime(2026, 2, 3, 14, tzinfo=__import__("datetime").UTC),
        )

        class Broken(FakeSheets):
            async def read_year(self, year):
                raise SheetsError("provider_unavailable")

        settings = type(
            "S",
            (),
            {
                "google_spreadsheet_id": "synthetic",
                "calendar_sync_enabled": True,
                "telegram_bot_username": "testbot",
            },
        )()
        result = await CalendarSync(sessions, settings, client=Broken()).tick()
        assert result["errors"] == 1
        async with sessions() as session:
            binding = await session.scalar(
                select(CalendarBinding).where(
                    CalendarBinding.event_id == (await session.scalar(select(Event))).id
                )
            )
            assert binding.status == "error" and binding.error_code == "provider_unavailable"
            assert binding.attempts == 1

    asyncio.run(run())
