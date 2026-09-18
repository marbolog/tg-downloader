import pytest
from db import Database
from reader import BrowseApp


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _channel(db, telegram_id=1, identifier="@ch", title="Chan") -> int:
    db.add_channel(telegram_id, identifier, title)
    with db._conn() as conn:
        return conn.execute(
            "SELECT id FROM channels WHERE telegram_id=?", (telegram_id,)
        ).fetchone()["id"]


def _downloaded(db, channel_id, msg_id, filename, ext="pdf", local_path=None, language=None) -> int:
    mid = db.save_media_message(
        channel_id=channel_id, message_id=msg_id, filename=filename, size=100,
        mime_type="application/octet-stream", ext=ext,
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, local_path or f"/tmp/{filename}", language=language)
    return mid


async def test_library_screen_lists_downloaded_files(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "book.pdf")
    _downloaded(db, ch, 2, "novel.epub")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 2


async def test_library_screen_is_empty_when_no_downloads(db):
    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 0


async def test_library_screen_dedupes_by_hash(db):
    ch = _channel(db)
    id1 = _downloaded(db, ch, 1, "book.pdf")
    id2 = _downloaded(db, ch, 2, "book_copy.pdf")
    with db._conn() as conn:
        conn.execute("UPDATE media_messages SET file_hash='samehash' WHERE id IN (?, ?)", (id1, id2))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 1


async def test_cycle_channel_filters_table(db):
    ch1 = _channel(db, telegram_id=1, identifier="@ch1", title="Channel One")
    ch2 = _channel(db, telegram_id=2, identifier="@ch2", title="Channel Two")
    _downloaded(db, ch1, 1, "a.pdf")
    _downloaded(db, ch2, 2, "b.pdf")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 2
        await pilot.press("c")  # All -> @ch1 (channels sorted alphabetically)
        assert table.row_count == 1
        await pilot.press("c")  # @ch1 -> @ch2
        assert table.row_count == 1
        await pilot.press("c")  # @ch2 -> back to All
        assert table.row_count == 2


async def test_cycle_language_filters_table(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "en.pdf", language="en")
    _downloaded(db, ch, 2, "unknown.pdf", language=None)

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 2
        await pilot.press("l")  # All -> __unknown__ (sorts before "en")
        assert table.row_count == 1
        await pilot.press("l")  # __unknown__ -> en
        assert table.row_count == 1
        await pilot.press("l")  # en -> back to All
        assert table.row_count == 2


async def test_text_filter_matches_filename(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "economist.pdf")
    _downloaded(db, ch, 2, "novel.epub")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("slash")
        await pilot.press(*"econ")
        await pilot.press("enter")
        assert table.row_count == 1


async def test_text_filter_cancel_leaves_table_unchanged(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "a.pdf")
    _downloaded(db, ch, 2, "b.pdf")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("slash")
        await pilot.press("escape")
        assert table.row_count == 2
