"""Post/card agreement, rich Telegram output and durable output selection."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from test_events import ADMIN, published, seed
from test_events import sessions as sessions

from cu_clubs.digest import ANNOUNCEMENTS_URL, render_post, week_bounds
from cu_clubs.digest_ui import handle_digest
from cu_clubs.event_models import Event
from cu_clubs.models import Conversation
from cu_clubs.screens import _pages


def item(identifier, start, names):
    return {
        "id": identifier,
        "starts_at": start,
        "clubs": [{"name": n} for n in names],
        "public_url": "https://t.me/cu_clubs_app?start=e_" + identifier,
    }


def test_post_owner_format_and_moscow_order():
    snapshot = {
        "events": [
            item("late", "2026-11-10T20:00:00+03:00", ["Кино", "Лит"]),
            item("mon", "2026-11-09T19:00:00+03:00", ["Клуб <А> & Б"]),
            item("early", "2026-11-10T18:00:00+03:00", ["Шахматы"]),
        ]
    }
    html, md, pages = render_post(snapshot)
    assert html.startswith(
        '<b>Понедельник:</b>\n\n➡️ <a href="https://t.me/cu_clubs_app?start=e_mon">'
        "Клуб &lt;А&gt; &amp; Б</a>"
    )
    assert html.index("e_early") < html.index("e_late")
    assert ">Кино х Лит</a>" in html
    assert "Среда:" not in html
    assert f'href="{ANNOUNCEMENTS_URL}"' in html
    assert html.endswith("#<b>Дайджестсобытий</b>")
    assert "**Вторник:**" in md and "[Кино х Лит]" in md
    assert len(pages) == 1 and "<a href=" in pages[0]


def test_large_post_pages_keep_all_events_and_rich_links():
    events = [
        item(str(n), "2026-11-09T19:00:00+03:00", [f"Клуб {n}: " + "я" * 150]) for n in range(80)
    ]
    html, md, pages = render_post({"events": events})
    assert len(pages) > 1
    for page in pages:
        assert _pages(page, photo=False) == [page]
    for event in events:
        marker = 'href="' + event["public_url"] + '"'
        assert sum(p.count(marker) for p in pages) == 1
        assert marker in html
    assert md.count("➡️ [") == 80
    assert sum("#<b>Дайджестсобытий</b>" in p for p in pages) == 1


def test_rich_links_are_counted_after_telegram_entity_parsing():
    text = "\n".join(
        f'<a href="https://example.org/{"x" * 90}/{n}">Событие {n}</a>' for n in range(60)
    )
    assert len(text) > 3900
    assert _pages(text, photo=False) == [text]


def test_post_and_cards_share_selection_and_revalidate(sessions):
    async def scenario():
        await seed(sessions)
        first = await published(sessions)
        async with sessions.begin() as session:
            e = await session.scalar(select(Event).where(Event.public_id == first["public_id"]))
            state = Conversation(telegram_id=ADMIN, state="idle", payload={})
            session.add(state)
            settings = SimpleNamespace(telegram_bot_username="cu_clubs_app")

            async def act(value):
                data = (
                    "digest" if value == "digest" else "dg:" + state.payload["nonce"] + ":" + value
                )
                return await handle_digest(session, ADMIN, state, None, data, settings)

            await act("digest")
            await act("week:" + week_bounds(e.starts_at.date().isoformat())[0].isoformat())
            preview = await act("all")
            assert "Текст поста" in str(preview.markup)
            post = await act("post:0")
            assert "?start=e_" + e.public_id in post.text
            full = await act("postfile")
            assert full.document.filename.endswith(".html")
            cards = await act("export")
            snap = json.loads(cards.document.data)
            assert [x["id"] for x in snap["events"]] == [e.public_id]
            assert post.text == render_post(snap)[0]
            await act("post:0")
            e.starts_at += timedelta(days=8)
            e.ends_at += timedelta(days=8)
            e.version += 1
            await session.flush()
            stale = await act("postfile")
            assert stale.document is None and "изменились" in stale.text

    asyncio.run(scenario())
