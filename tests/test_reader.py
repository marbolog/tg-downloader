from unittest.mock import patch

import pytest
from textual.widgets import ListView, Static

from db import Database
from reader import BrowseApp
from reader_document import Document, Section, TocEntry


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


async def test_enter_on_pdf_opens_reader_screen(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    fake_doc = Document(
        sections=[Section(title="Page 1", text="hello world")],
        toc=[TocEntry(title="Page 1", section_index=0)],
    )
    app = BrowseApp(db)
    with patch("reader.build_document", return_value=fake_doc):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "ReaderScreen"


async def test_enter_on_unsupported_extension_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "comic.cbz"
    f.write_bytes(b"fake")
    _downloaded(db, ch, 1, "comic.cbz", ext="cbz", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.screen.__class__.__name__ == "LibraryScreen"


async def test_enter_on_missing_file_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    missing_path = str(tmp_path / "gone.pdf")  # never created on disk
    _downloaded(db, ch, 1, "gone.pdf", ext="pdf", local_path=missing_path)

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.screen.__class__.__name__ == "LibraryScreen"


async def test_enter_on_textless_pdf_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "scanned.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=None):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "LibraryScreen"


def _fake_two_section_document() -> Document:
    return Document(
        sections=[
            Section(title="Page 1", text="First page text."),
            Section(title="Page 2", text="Second page text."),
        ],
        toc=[
            TocEntry(title="Page 1", section_index=0),
            TocEntry(title="Page 2", section_index=1),
        ],
    )


async def test_reader_screen_shows_toc_entries_and_section_text(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            toc = app.screen.query_one(ListView)
            assert len(toc.children) == 2
            first_section = app.screen.query_one("#section-0", Static)
            assert "First page text." in str(first_section.content)


async def test_selecting_toc_entry_scrolls_to_section(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            toc = app.screen.query_one(ListView)
            toc.index = 1
            await pilot.press("enter")  # select "Page 2" in the TOC
            second_section = app.screen.query_one("#section-1", Static)
            content = app.screen.query_one("#reader-content")
            assert second_section.region.y <= content.scroll_offset.y + content.size.height


async def test_escape_returns_to_library_screen(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "ReaderScreen"
            await pilot.press("escape")
            assert app.screen.__class__.__name__ == "LibraryScreen"


def _three_section_document_with_needle() -> Document:
    return Document(
        sections=[
            Section(title="Page 1", text="Nothing interesting here."),
            Section(title="Page 2", text="The needle is here."),
            Section(title="Page 3", text="Another needle appears."),
        ],
        toc=[
            TocEntry(title="Page 1", section_index=0),
            TocEntry(title="Page 2", section_index=1),
            TocEntry(title="Page 3", section_index=2),
        ],
    )


async def test_search_jumps_to_first_match(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"needle")
            await pilot.press("enter")
            reader_screen = app.screen
            assert reader_screen._matches == [1, 2]
            assert reader_screen._match_pos == 0


async def test_next_match_cycles_forward(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"needle")
            await pilot.press("enter")
            await pilot.press("n")
            reader_screen = app.screen
            assert reader_screen._match_pos == 1
            await pilot.press("n")
            assert reader_screen._match_pos == 0  # wraps around


async def test_search_no_matches_notifies_and_does_not_crash(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"xyzzy")
            await pilot.press("enter")
            reader_screen = app.screen
            assert reader_screen._matches == []
