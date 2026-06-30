from pathlib import Path

from db import Database


def _make_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_channel(db: Database) -> int:
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (1, '@test', 'Test')"
        )
        return cur.lastrowid


def _save(db: Database, channel_id: int, msg_id: int):
    return db.save_media_message(
        channel_id=channel_id, message_id=msg_id, filename=f"f{msg_id}.pdf",
        size=1, mime_type="application/pdf", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )


def test_recorded_ids_empty_for_new_channel(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    assert db.get_recorded_message_ids(ch) == set()


def test_recorded_ids_includes_all_statuses(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid_dl = _save(db, ch, 10)
    db.mark_downloaded(mid_dl, "/x/f10.pdf")
    mid_disc = _save(db, ch, 11)
    db.mark_discarded(mid_disc)       # German/topic filter case
    _save(db, ch, 12)                 # left pending
    # All recorded ids count as "known" so reconcile won't re-fetch a file the
    # language filter already rejected.
    assert db.get_recorded_message_ids(ch) == {10, 11, 12}


def test_recorded_ids_isolated_per_channel(tmp_path):
    db = _make_db(tmp_path)
    ch1 = _insert_channel(db)
    with db._conn() as conn:
        ch2 = conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (2, '@two', 'Two')"
        ).lastrowid
    _save(db, ch1, 1)
    _save(db, ch2, 2)
    assert db.get_recorded_message_ids(ch1) == {1}
    assert db.get_recorded_message_ids(ch2) == {2}


def test_gap_detection_finds_missing_ids(tmp_path):
    """The core reconcile signal: Telegram ids present but absent from the DB."""
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    _save(db, ch, 100)
    _save(db, ch, 102)            # 101 was dropped mid-burst by real-time delivery
    _save(db, ch, 103)
    recorded = db.get_recorded_message_ids(ch)
    telegram_media_ids = {100, 101, 102, 103}
    missing = telegram_media_ids - recorded
    assert missing == {101}
