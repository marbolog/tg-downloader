"""Tests for search/indexer.py: index_file() code paths.

All PDF/EPUB chunking is mocked so tests run without real binary files.
"""
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from db import Database
from search.indexer import index_file


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _add_channel(db: Database) -> int:
    db.add_channel(1, "@ch", "Chan")
    with db._conn() as conn:
        return conn.execute("SELECT id FROM channels WHERE telegram_id=1").fetchone()["id"]


def _insert_downloaded(db: Database, channel_id: int) -> int:
    mid = db.save_media_message(
        channel_id=channel_id, message_id=1, filename="doc.pdf",
        size=1000, mime_type="application/pdf", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, "/dl/doc.pdf")
    return mid


class TestIndexFile:
    def test_unsupported_extension_returns_false(self, db, tmp_path):
        f = tmp_path / "book.mobi"
        f.write_bytes(b"fake")
        result = index_file(db, media_id=1, filepath=f, ext="mobi", filename="book.mobi")
        assert result is False

    def test_unsupported_extension_does_not_touch_db(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "book.mobi"
        f.write_bytes(b"fake")
        index_file(db, media_id=mid, filepath=f, ext="mobi", filename="book.mobi")
        with db._conn() as conn:
            row = conn.execute("SELECT indexed_at FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["indexed_at"] is None

    def test_missing_file_returns_false(self, db, tmp_path):
        result = index_file(db, media_id=1, filepath=tmp_path / "gone.pdf", ext="pdf", filename="gone.pdf")
        assert result is False

    def test_no_chunks_marks_indexed_returns_false(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "scanned.pdf"
        f.write_bytes(b"not a real pdf")

        with patch("search.indexer.chunk_file", return_value=[]):
            result = index_file(db, media_id=mid, filepath=f, ext="pdf", filename="scanned.pdf")

        assert result is False
        with db._conn() as conn:
            row = conn.execute("SELECT indexed_at FROM media_messages WHERE id=?", (mid,)).fetchone()
        # indexed_at should be set so startup heal skips it on next restart
        assert row["indexed_at"] is not None

    def test_no_chunks_does_not_add_fts_rows(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "scanned.pdf"
        f.write_bytes(b"not a real pdf")

        with patch("search.indexer.chunk_file", return_value=[]):
            index_file(db, media_id=mid, filepath=f, ext="pdf", filename="scanned.pdf")

        assert db.search_fts_query("anything") == []

    def test_chunks_indexed_returns_true(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"not a real pdf")

        fake_chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "Alentejo wine region"}]
        with patch("search.indexer.chunk_file", return_value=fake_chunks):
            result = index_file(db, media_id=mid, filepath=f, ext="pdf", filename="doc.pdf")

        assert result is True

    def test_chunks_are_searchable_after_index(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"not a real pdf")

        fake_chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "Alentejo wine region"}]
        with patch("search.indexer.chunk_file", return_value=fake_chunks):
            index_file(db, media_id=mid, filepath=f, ext="pdf", filename="doc.pdf")

        results = db.search_fts_query("Alentejo")
        assert len(results) == 1
        assert results[0]["media_id"] == mid

    def test_chunks_mark_indexed_after_success(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"not a real pdf")

        fake_chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "some indexable text here"}]
        with patch("search.indexer.chunk_file", return_value=fake_chunks):
            index_file(db, media_id=mid, filepath=f, ext="pdf", filename="doc.pdf")

        with db._conn() as conn:
            row = conn.execute("SELECT indexed_at FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["indexed_at"] is not None

    def test_channel_identifier_stored_in_fts(self, db, tmp_path):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"not a real pdf")

        fake_chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "Douro valley port wine"}]
        with patch("search.indexer.chunk_file", return_value=fake_chunks):
            index_file(db, media_id=mid, filepath=f, ext="pdf", filename="doc.pdf", channel_identifier="@ch")

        results = db.search_fts_query("Douro", channel_identifier="@ch")
        assert len(results) == 1
