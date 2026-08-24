from pathlib import Path
import pytest
from db import Database


def _make_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_channel(db: Database) -> int:
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (1, '@test', 'Test')"
        )
        return cur.lastrowid


def _insert_downloaded(db: Database, channel_id: int, msg_id: int = 1) -> int:
    mid = db.save_media_message(
        channel_id=channel_id,
        message_id=msg_id,
        filename=f"file{msg_id}.pdf",
        size=1000,
        mime_type="application/pdf",
        ext="pdf",
        date="2026-01-01T00:00:00",
        caption="",
    )
    db.mark_downloaded(mid, f"/downloads/file{msg_id}.pdf")
    return mid


def test_get_unindexed_downloaded_returns_downloaded_records(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    rows = db.get_unindexed_downloaded()
    assert len(rows) == 1
    assert rows[0]["id"] == mid


def test_mark_indexed_removes_from_unindexed(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    db.mark_indexed(mid)
    assert db.get_unindexed_downloaded() == []


def test_mark_indexed_sets_timestamp(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    db.mark_indexed(mid)
    with db._conn() as conn:
        row = conn.execute("SELECT indexed_at FROM media_messages WHERE id=?", (mid,)).fetchone()
    assert row["indexed_at"] is not None


def test_get_unindexed_excludes_pending(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    db.save_media_message(
        channel_id=ch, message_id=99, filename="pending.pdf",
        size=500, mime_type="application/pdf", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )
    assert db.get_unindexed_downloaded() == []


def test_get_unindexed_returns_multiple(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    _insert_downloaded(db, ch, msg_id=1)
    _insert_downloaded(db, ch, msg_id=2)
    assert len(db.get_unindexed_downloaded()) == 2
