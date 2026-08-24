"""Tests for the web UI's bulk delete endpoint (POST /api/discard).

Regression coverage for a bug where discard_files() called db.mark_discarded()
once per id -- each its own connection/transaction -- against a DB the listener
container writes to concurrently. Any single "database is locked" mid-batch
raised an unhandled 500 and left already-unlinked files still marked
'downloaded' in the DB. Fixed by batching into one transaction
(db.mark_discarded_many) and catching the lock error explicitly.

webui/app.py reads its config from the environment at import time and mounts
StaticFiles("static") relative to the cwd, so we point both at temp/webui before
importing the module (same setup as test_webui_pdf.py).
"""
import importlib
import sqlite3
import sys
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(WEBUI_DIR)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("THUMBS_DIR", str(tmp_path / "thumbs"))
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)  # auth is tested in test_webui_auth.py
    if str(WEBUI_DIR) not in sys.path:
        sys.path.insert(0, str(WEBUI_DIR))
    sys.modules.pop("app", None)
    import app as webui_app
    importlib.reload(webui_app)
    from starlette.testclient import TestClient
    return webui_app, TestClient(webui_app.app)


_next_msg_id = iter(range(1, 10_000))


def _channel(db) -> int:
    with db._conn() as conn:
        row = conn.execute("SELECT id FROM channels WHERE telegram_id=1").fetchone()
        if row:
            return row["id"]
        return conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (1, '@c', 'C')"
        ).lastrowid


def _insert(db, *, filename: str, local_path: str) -> int:
    mid = db.save_media_message(
        channel_id=_channel(db), message_id=next(_next_msg_id), filename=filename, size=1,
        mime_type="application/octet-stream", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, local_path)
    return mid


def test_discard_deletes_files_and_marks_db(tmp_path, monkeypatch):
    webui_app, client = _client(tmp_path, monkeypatch)
    f1, f2 = tmp_path / "a.pdf", tmp_path / "b.pdf"
    f1.write_text("a")
    f2.write_text("b")
    id1 = _insert(webui_app.db, filename="a.pdf", local_path=str(f1))
    id2 = _insert(webui_app.db, filename="b.pdf", local_path=str(f2))

    r = client.post("/api/discard", json={"ids": [id1, id2]})

    assert r.status_code == 200
    assert r.json() == {"deleted": 2, "total": 2}
    assert not f1.exists()
    assert not f2.exists()
    with webui_app.db._conn() as conn:
        rows = conn.execute(
            "SELECT status, local_path FROM media_messages WHERE id IN (?, ?)", (id1, id2)
        ).fetchall()
    assert {r["status"] for r in rows} == {"discarded"}
    assert all(r["local_path"] is None for r in rows)


def test_discard_empty_ids_is_a_noop(tmp_path, monkeypatch):
    _webui_app, client = _client(tmp_path, monkeypatch)
    r = client.post("/api/discard", json={"ids": []})
    assert r.status_code == 200
    assert r.json() == {"deleted": 0, "total": 0}


def test_discard_uses_one_transaction_for_whole_batch(tmp_path, monkeypatch):
    """A batch of N ids must take exactly one write-lock, not N -- that's the
    actual fix: fewer lock-acquisition attempts means fewer chances to collide
    with the listener's concurrent writes."""
    webui_app, client = _client(tmp_path, monkeypatch)
    ids = [_insert(webui_app.db, filename=f"{i}.pdf", local_path=str(tmp_path / f"{i}.pdf")) for i in range(5)]
    for i in ids:
        (tmp_path / f"{ids.index(i)}.pdf").write_text("x")

    calls = []
    real_conn = webui_app.db._conn

    def counting_conn():
        calls.append(1)
        return real_conn()

    monkeypatch.setattr(webui_app.db, "_conn", counting_conn)
    r = client.post("/api/discard", json={"ids": ids})

    assert r.status_code == 200
    # One connection for the bulk local_path read, one for mark_discarded_many.
    assert len(calls) == 2


def test_discard_returns_503_on_database_locked(tmp_path, monkeypatch):
    """A lock-contention error must surface as a clean 503, not an unhandled
    500 -- and must not leave files unlinked from disk with a stale DB row."""
    webui_app, client = _client(tmp_path, monkeypatch)
    f1 = tmp_path / "a.pdf"
    f1.write_text("a")
    id1 = _insert(webui_app.db, filename="a.pdf", local_path=str(f1))

    def raise_locked(_ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webui_app.db, "mark_discarded_many", raise_locked)
    r = client.post("/api/discard", json={"ids": [id1]})

    assert r.status_code == 503
    assert f1.exists()  # never unlinked -- DB write happens before disk unlink
    with webui_app.db._conn() as conn:
        row = conn.execute("SELECT status FROM media_messages WHERE id=?", (id1,)).fetchone()
    assert row["status"] == "downloaded"
