"""Tests for Database methods not covered by the existing test suite.

Covers: channel CRUD, status lifecycle transitions, analytics queries,
tagging helpers, UI read queries, and side-effects of mark_discarded /
mark_expired on the search_fts table.
"""
from pathlib import Path
import pytest
from db import Database


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _add_channel(db: Database, telegram_id: int = 1, identifier: str = "@ch", title: str = "Chan") -> int:
    db.add_channel(telegram_id, identifier, title)
    with db._conn() as conn:
        row = conn.execute("SELECT id FROM channels WHERE telegram_id=?", (telegram_id,)).fetchone()
    return row["id"]


def _insert_downloaded(db: Database, channel_id: int, msg_id: int = 1, language: str | None = None, file_hash: str | None = None) -> int:
    mid = db.save_media_message(
        channel_id=channel_id, message_id=msg_id, filename=f"f{msg_id}.pdf",
        size=1000, mime_type="application/pdf", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, f"/dl/f{msg_id}.pdf", language=language, file_hash=file_hash)
    return mid


# ── Channel CRUD ──────────────────────────────────────────────────────────────

class TestChannelCrud:
    def test_add_and_list_channel(self, db):
        db.add_channel(1, "@news", "News")
        channels = db.list_channels()
        assert len(channels) == 1
        assert channels[0]["identifier"] == "@news"
        assert channels[0]["title"] == "News"

    def test_add_channel_upserts_on_conflict(self, db):
        db.add_channel(1, "@news", "Old Title")
        db.add_channel(1, "@news", "New Title")
        channels = db.list_channels()
        assert len(channels) == 1
        assert channels[0]["title"] == "New Title"

    def test_remove_channel_by_identifier(self, db):
        db.add_channel(1, "@news", "News")
        removed = db.remove_channel("@news")
        assert removed is True
        assert db.list_channels() == []

    def test_remove_channel_by_telegram_id(self, db):
        db.add_channel(42, "@news", "News")
        removed = db.remove_channel("42")
        assert removed is True
        assert db.list_channels() == []

    def test_remove_nonexistent_channel_returns_false(self, db):
        assert db.remove_channel("@nobody") is False

    def test_get_channel_by_telegram_id(self, db):
        db.add_channel(99, "@ch", "Ch")
        row = db.get_channel_by_telegram_id(99)
        assert row is not None
        assert row["identifier"] == "@ch"

    def test_get_channel_by_telegram_id_missing(self, db):
        assert db.get_channel_by_telegram_id(9999) is None


# ── Status lifecycle ──────────────────────────────────────────────────────────

class TestStatusLifecycle:
    def test_mark_skipped(self, db):
        ch = _add_channel(db)
        mid = db.save_media_message(
            channel_id=ch, message_id=1, filename="f.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2026-01-01T00:00:00", caption="",
        )
        db.mark_skipped(mid)
        with db._conn() as conn:
            row = conn.execute("SELECT status FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "skipped"

    def test_mark_expired_sets_status_and_clears_path(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        db.mark_expired(mid)
        with db._conn() as conn:
            row = conn.execute("SELECT status, local_path FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "expired"
        assert row["local_path"] is None

    def test_mark_expired_removes_fts_rows(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        db.search_fts_index_file(mid, [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "searchable content here"}], "f1.pdf")
        db.mark_expired(mid)
        assert db.search_fts_query("searchable") == []

    def test_mark_discarded_removes_fts_rows(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        db.search_fts_index_file(mid, [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "discardable content here"}], "f1.pdf")
        db.mark_discarded(mid)
        assert db.search_fts_query("discardable") == []

    def test_mark_discarded_many_sets_status_and_clears_path_for_all(self, db):
        ch = _add_channel(db)
        ids = [_insert_downloaded(db, ch, msg_id=i) for i in range(1, 4)]
        db.mark_discarded_many(ids)
        with db._conn() as conn:
            rows = conn.execute(
                f"SELECT status, local_path FROM media_messages WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        assert len(rows) == 3
        assert all(r["status"] == "discarded" and r["local_path"] is None for r in rows)

    def test_mark_discarded_many_removes_fts_rows_for_all(self, db):
        ch = _add_channel(db)
        ids = [_insert_downloaded(db, ch, msg_id=i) for i in (1, 2)]
        for mid in ids:
            db.search_fts_index_file(mid, [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "batchdiscardable content"}], "f.pdf")
        db.mark_discarded_many(ids)
        assert db.search_fts_query("batchdiscardable") == []

    def test_mark_discarded_many_empty_list_is_a_noop(self, db):
        db.mark_discarded_many([])  # must not raise (empty IN (...) is invalid SQL)

    def test_save_media_message_duplicate_returns_none(self, db):
        ch = _add_channel(db)
        db.save_media_message(
            channel_id=ch, message_id=5, filename="f.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2026-01-01T00:00:00", caption="",
        )
        result = db.save_media_message(
            channel_id=ch, message_id=5, filename="f.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2026-01-01T00:00:00", caption="",
        )
        assert result is None

    def test_mark_downloaded_stores_language_and_hash(self, db):
        ch = _add_channel(db)
        mid = db.save_media_message(
            channel_id=ch, message_id=1, filename="f.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2026-01-01T00:00:00", caption="",
        )
        db.mark_downloaded(mid, "/dl/f.pdf", language="en", file_hash="abc123")
        with db._conn() as conn:
            row = conn.execute("SELECT language, file_hash FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["language"] == "en"
        assert row["file_hash"] == "abc123"


# ── Retention / expired files ─────────────────────────────────────────────────

class TestGetExpiredFiles:
    def test_old_file_returned(self, db):
        ch = _add_channel(db)
        mid = db.save_media_message(
            channel_id=ch, message_id=1, filename="old.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2020-01-01T00:00:00", caption="",
        )
        # Force downloaded_at to a very old timestamp.
        with db._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='downloaded', local_path='/dl/old.pdf', downloaded_at='2020-01-01 00:00:00' WHERE id=?",
                (mid,),
            )
        expired = db.get_expired_files(retention_days=1)
        assert len(expired) == 1
        assert expired[0]["id"] == mid

    def test_recent_file_not_returned(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        assert db.get_expired_files(retention_days=365) == []

    def test_no_downloaded_at_not_returned(self, db):
        ch = _add_channel(db)
        mid = db.save_media_message(
            channel_id=ch, message_id=1, filename="old.pdf",
            size=1, mime_type="application/pdf", ext="pdf",
            date="2020-01-01T00:00:00", caption="",
        )
        with db._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='downloaded', local_path='/dl/old.pdf' WHERE id=?",
                (mid,),
            )
        assert db.get_expired_files(retention_days=1) == []


# ── Pending / downloaded queries ──────────────────────────────────────────────

class TestReadQueries:
    def test_get_max_message_id_empty(self, db):
        ch = _add_channel(db)
        assert db.get_max_message_id(ch) is None

    def test_get_max_message_id_returns_highest(self, db):
        ch = _add_channel(db)
        for msg_id in (10, 5, 20, 3):
            db.save_media_message(
                channel_id=ch, message_id=msg_id, filename=f"f{msg_id}.pdf",
                size=1, mime_type="application/pdf", ext="pdf",
                date="2026-01-01T00:00:00", caption="",
            )
        assert db.get_max_message_id(ch) == 20

    def test_pending_counts_by_channel(self, db):
        ch = _add_channel(db)
        db.save_media_message(channel_id=ch, message_id=1, filename="a.pdf", size=1, mime_type="application/pdf", ext="pdf", date="2026-01-01T00:00:00", caption="")
        db.save_media_message(channel_id=ch, message_id=2, filename="b.pdf", size=1, mime_type="application/pdf", ext="pdf", date="2026-01-01T00:00:00", caption="")
        counts = db.pending_counts()
        assert counts[ch] == 2

    def test_pending_counts_excludes_downloaded(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        counts = db.pending_counts()
        assert ch not in counts or counts[ch] == 0

    def test_get_pending_media_returns_pending(self, db):
        ch = _add_channel(db)
        db.save_media_message(channel_id=ch, message_id=1, filename="p.pdf", size=1, mime_type="application/pdf", ext="pdf", date="2026-01-01T00:00:00", caption="")
        rows = db.get_pending_media()
        assert len(rows) == 1
        assert rows[0]["filename"] == "p.pdf"

    def test_get_downloaded_media_excludes_pending(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, msg_id=1)
        db.save_media_message(channel_id=ch, message_id=2, filename="pending.pdf", size=1, mime_type="application/pdf", ext="pdf", date="2026-01-01T00:00:00", caption="")
        rows = db.get_downloaded_media()
        assert len(rows) == 1
        assert rows[0]["filename"] == "f1.pdf"

    def test_get_media_returns_joined_row(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        row = db.get_media(mid)
        assert row is not None
        assert row["id"] == mid
        assert "channel_title" in row

    def test_get_media_missing_returns_none(self, db):
        assert db.get_media(9999) is None


# ── Analytics ─────────────────────────────────────────────────────────────────

class TestAnalytics:
    def test_health_snapshot_shape(self, db):
        snap = db.health_snapshot()
        for key in ("downloaded", "pending", "discarded", "expired", "indexed", "index_pending", "channels_no_messages"):
            assert key in snap

    def test_health_snapshot_counts_downloaded(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch)
        assert db.health_snapshot()["downloaded"] == 1

    def test_health_snapshot_channels_no_messages(self, db):
        _add_channel(db)
        assert db.health_snapshot()["channels_no_messages"] == 1

    def test_language_counts(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, msg_id=1, language="en")
        _insert_downloaded(db, ch, msg_id=2, language="en")
        _insert_downloaded(db, ch, msg_id=3, language="fr")
        rows = db.language_counts()
        by_lang = {r["language"]: r["count"] for r in rows}
        assert by_lang["en"] == 2
        assert by_lang["fr"] == 1

    def test_language_counts_null_as_unknown(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, msg_id=1, language=None)
        rows = db.language_counts()
        by_lang = {r["language"]: r["count"] for r in rows}
        assert by_lang.get("__unknown__") == 1

    def test_channel_counts_excludes_empty_channels(self, db):
        _add_channel(db, telegram_id=1, identifier="@empty")
        ch2 = _add_channel(db, telegram_id=2, identifier="@active")
        _insert_downloaded(db, ch2)
        rows = db.channel_counts()
        identifiers = [r["identifier"] for r in rows]
        assert "@active" in identifiers
        assert "@empty" not in identifiers

    def test_find_duplicate_groups_returns_shared_hashes(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, msg_id=1, file_hash="deadbeef")
        _insert_downloaded(db, ch, msg_id=2, file_hash="deadbeef")
        groups = db.find_duplicate_groups()
        assert len(groups) == 1
        assert groups[0]["copies"] == 2
        assert groups[0]["file_hash"] == "deadbeef"

    def test_find_duplicate_groups_unique_files_excluded(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, msg_id=1, file_hash="aaaa")
        _insert_downloaded(db, ch, msg_id=2, file_hash="bbbb")
        assert db.find_duplicate_groups() == []

    def test_get_status_counts_shape(self, db):
        _add_channel(db)
        rows = db.get_status_counts()
        assert len(rows) == 1
        row = rows[0]
        for key in ("pending", "downloaded", "discarded", "expired", "skipped", "total"):
            assert key in row


# ── Tagging helpers ───────────────────────────────────────────────────────────

class TestTagging:
    def test_set_language_and_retrieve(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        db.set_language(mid, "en")
        with db._conn() as conn:
            row = conn.execute("SELECT language FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["language"] == "en"

    def test_set_language_to_none(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch, language="de")
        db.set_language(mid, None)
        with db._conn() as conn:
            row = conn.execute("SELECT language FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["language"] is None

    def test_get_untagged_downloaded_returns_null_language(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        rows = db.get_untagged_downloaded()
        assert len(rows) == 1
        assert rows[0]["id"] == mid

    def test_get_untagged_downloaded_excludes_tagged(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, language="en")
        assert db.get_untagged_downloaded() == []

    def test_set_file_hash_and_retrieve(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        db.set_file_hash(mid, "cafebabe")
        with db._conn() as conn:
            row = conn.execute("SELECT file_hash FROM media_messages WHERE id=?", (mid,)).fetchone()
        assert row["file_hash"] == "cafebabe"

    def test_get_untagged_for_hash_returns_unhashed(self, db):
        ch = _add_channel(db)
        mid = _insert_downloaded(db, ch)
        rows = db.get_untagged_for_hash()
        assert any(r["id"] == mid for r in rows)

    def test_get_untagged_for_hash_excludes_hashed(self, db):
        ch = _add_channel(db)
        _insert_downloaded(db, ch, file_hash="abc")
        assert db.get_untagged_for_hash() == []


# ── list_downloaded_files filters ─────────────────────────────────────────────

class TestListDownloadedFiles:
    def _setup(self, db):
        ch1 = _add_channel(db, telegram_id=1, identifier="@a")
        ch2 = _add_channel(db, telegram_id=2, identifier="@b")
        _insert_downloaded(db, ch1, msg_id=1, language="en")
        _insert_downloaded(db, ch2, msg_id=2, language="fr")
        _insert_downloaded(db, ch1, msg_id=3, language=None)
        return ch1, ch2

    def test_no_filter_returns_all(self, db):
        self._setup(db)
        assert len(db.list_downloaded_files()) == 3

    def test_filter_by_channel(self, db):
        self._setup(db)
        rows = db.list_downloaded_files(channel="@a")
        assert all(r["channel_identifier"] == "@a" for r in rows)
        assert len(rows) == 2

    def test_filter_by_language(self, db):
        self._setup(db)
        rows = db.list_downloaded_files(language="en")
        assert all(r["language"] == "en" for r in rows)
        assert len(rows) == 1

    def test_filter_by_unknown_language(self, db):
        self._setup(db)
        rows = db.list_downloaded_files(language="__unknown__")
        assert all(r["language"] is None for r in rows)
        assert len(rows) == 1

    def test_filter_by_ids(self, db):
        ch = _add_channel(db)
        mid1 = _insert_downloaded(db, ch, msg_id=10)
        _insert_downloaded(db, ch, msg_id=11)
        rows = db.list_downloaded_files(ids=[mid1])
        assert len(rows) == 1
        assert rows[0]["id"] == mid1


# ── get_download_history ──────────────────────────────────────────────────────

class TestGetDownloadHistory:
    def test_returns_up_to_limit(self, db):
        ch = _add_channel(db)
        for i in range(5):
            _insert_downloaded(db, ch, msg_id=i)
        rows = db.get_download_history(limit=3)
        assert len(rows) == 3

    def test_excludes_pending(self, db):
        ch = _add_channel(db)
        db.save_media_message(channel_id=ch, message_id=1, filename="p.pdf", size=1, mime_type="application/pdf", ext="pdf", date="2026-01-01T00:00:00", caption="")
        assert db.get_download_history() == []
