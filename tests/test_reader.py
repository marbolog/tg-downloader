import sqlite3
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


async def test_reader_screen_preserves_bracketed_text_as_literal_not_markup(db, tmp_path):
    # Extracted PDF/EPUB text routinely contains "[...]" (citations, footnotes,
    # code). Static/Label default to Rich markup parsing, which silently drops
    # anything that looks like a markup tag -- e.g. "[1]" or "[Note]" -- rather
    # than raising, so the loss is easy to miss without this regression test.
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    doc = Document(
        sections=[Section(title="Page 1", text="See reference [1] and note [x] here.")],
        toc=[TocEntry(title="Weird [Title]", section_index=0)],
    )
    app = BrowseApp(db)
    with patch("reader.build_document", return_value=doc):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            section = app.screen.query_one("#section-0", Static)
            assert "See reference [1] and note [x] here." in str(section.content)
            toc = app.screen.query_one(ListView)
            label = toc.children[0].query_one("Label")
            assert "Weird [Title]" in str(label.content)


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


async def test_delete_prompt_shows_status_bar_not_a_dialog(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is False
        await pilot.press("d")
        assert status_bar.display is True
        assert "book.pdf" in str(status_bar.content)
        assert app.screen.__class__.__name__ == "LibraryScreen"  # no modal pushed


async def test_delete_confirmed_with_y_removes_file_marks_discarded_and_removes_row(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.row_count == 1
        await pilot.press("d")
        await pilot.press("y")
        assert table.row_count == 0

    assert not f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "discarded"
    assert row["local_path"] is None


async def test_delete_prompt_ignores_other_keys_until_resolved(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("j")  # not y/n/escape -- must not resolve the prompt
        assert table.row_count == 1
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is True

    assert f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "downloaded"


async def test_delete_cancelled_with_n_keeps_file(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("n")
        assert table.row_count == 1
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is False

    assert f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "downloaded"


async def test_delete_cancelled_with_escape_keeps_file(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("escape")
        assert table.row_count == 1

    assert f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "downloaded"


async def test_delete_when_already_removed_notifies_and_refreshes(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        # Simulate the web UI discarding it concurrently, between listing and confirming.
        db.mark_discarded_many([media_id])
        f.unlink()

        table = app.screen.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("y")
        assert table.row_count == 0  # still cleared from the visible table


async def test_delete_surfaces_locked_database_as_notification_not_crash(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)

    def raise_locked(_ids):
        raise sqlite3.OperationalError("database is locked")

    async with app.run_test() as pilot:
        with patch.object(db, "mark_discarded_many", side_effect=raise_locked):
            table = app.screen.query_one("#library-table")
            await pilot.press("d")
            await pilot.press("y")
            assert table.row_count == 1  # unchanged -- delete did not go through

    assert f.exists()  # never unlinked -- DB write is attempted before disk unlink


async def test_space_marks_row_shows_indicator_and_persists_across_movement(db, tmp_path):
    ch = _channel(db)
    _downloaded(db, ch, 1, "a.pdf", local_path=str(tmp_path / "a.pdf"))
    _downloaded(db, ch, 2, "b.pdf", local_path=str(tmp_path / "b.pdf"))
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"x")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("space")  # mark row under cursor
        assert table.get_cell_at((0, 0)) == "●"
        await pilot.press("j")  # move to next row -- mark must persist, cursor must move
        assert table.cursor_coordinate.row == 1
        assert table.get_cell_at((0, 0)) == "●"
        assert table.get_cell_at((1, 0)) == ""


async def test_space_again_unmarks_row(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    _downloaded(db, ch, 1, "a.pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("space")
        assert table.get_cell_at((0, 0)) == "●"
        await pilot.press("space")
        assert table.get_cell_at((0, 0)) == ""


async def test_delete_with_marks_deletes_all_marked_and_leaves_others(db, tmp_path):
    ch = _channel(db)
    ids = []
    for i, name in enumerate(["a.pdf", "b.pdf", "c.pdf"], start=1):
        f = tmp_path / name
        f.write_bytes(b"x")
        ids.append(_downloaded(db, ch, i, name, local_path=str(f)))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("space")  # mark row 0 (a.pdf)
        await pilot.press("j")
        await pilot.press("j")
        await pilot.pause()  # let the cursor-move settle before reading it back
        await pilot.press("space")  # mark row 2 (c.pdf)
        await pilot.press("d")
        status_bar = app.screen.query_one("#status-bar", Static)
        assert "2" in str(status_bar.content)
        await pilot.press("y")
        assert table.row_count == 1

    assert not (tmp_path / "a.pdf").exists()
    assert (tmp_path / "b.pdf").exists()
    assert not (tmp_path / "c.pdf").exists()
    assert db.get_media(ids[0])["status"] == "discarded"
    assert db.get_media(ids[1])["status"] == "downloaded"
    assert db.get_media(ids[2])["status"] == "discarded"


async def test_visual_mode_marks_contiguous_range(db, tmp_path):
    ch = _channel(db)
    ids = []
    for i, name in enumerate(["a.pdf", "b.pdf", "c.pdf"], start=1):
        f = tmp_path / name
        f.write_bytes(b"x")
        ids.append(_downloaded(db, ch, i, name, local_path=str(f)))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("v")
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is True
        await pilot.press("j")
        await pilot.press("j")
        await pilot.pause()  # let the cursor-move settle before reading it back
        await pilot.press("space")  # commit range [0, 2]
        assert status_bar.display is False
        assert table.get_cell_at((0, 0)) == "●"
        assert table.get_cell_at((1, 0)) == "●"
        assert table.get_cell_at((2, 0)) == "●"


async def test_visual_mode_escape_cancels_without_marking(db, tmp_path):
    ch = _channel(db)
    for i, name in enumerate(["a.pdf", "b.pdf"], start=1):
        f = tmp_path / name
        f.write_bytes(b"x")
        _downloaded(db, ch, i, name, local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("v")
        await pilot.press("j")
        await pilot.press("escape")
        assert table.get_cell_at((0, 0)) == ""
        assert table.get_cell_at((1, 0)) == ""
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is False


async def test_j_and_k_move_cursor_up_and_down(db, tmp_path):
    ch = _channel(db)
    for i, name in enumerate(["a.pdf", "b.pdf"], start=1):
        f = tmp_path / name
        f.write_bytes(b"x")
        _downloaded(db, ch, i, name, local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        assert table.cursor_coordinate.row == 0
        await pilot.press("j")
        assert table.cursor_coordinate.row == 1
        await pilot.press("k")
        assert table.cursor_coordinate.row == 0


async def test_g_and_shift_g_jump_to_top_and_bottom(db, tmp_path):
    ch = _channel(db)
    for i, name in enumerate(["a.pdf", "b.pdf", "c.pdf"], start=1):
        f = tmp_path / name
        f.write_bytes(b"x")
        _downloaded(db, ch, i, name, local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.screen.query_one("#library-table")
        await pilot.press("G")
        assert table.cursor_coordinate.row == 2
        await pilot.press("g")
        assert table.cursor_coordinate.row == 0


async def test_q_quits_app_from_library_screen(db):
    app = BrowseApp(db)
    async with app.run_test() as pilot:
        assert app._exit is False
        await pilot.press("q")
        assert app._exit is True


async def test_q_during_delete_prompt_cancels_instead_of_quitting(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    _downloaded(db, ch, 1, "a.pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.press("q")
        assert app._exit is False
        status_bar = app.screen.query_one("#status-bar", Static)
        assert status_bar.display is False
        table = app.screen.query_one("#library-table")
        assert table.row_count == 1  # cancelled, not deleted
