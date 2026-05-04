import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
    identifier  TEXT NOT NULL,
    title       TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS media_messages (
    id           INTEGER PRIMARY KEY,
    channel_id   INTEGER NOT NULL REFERENCES channels(id),
    message_id   INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    mime_type    TEXT,
    ext          TEXT,
    date         TEXT,
    caption      TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    local_path   TEXT,
    UNIQUE(channel_id, message_id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        log.info(f"Database ready: {path}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- channels ---

    def add_channel(self, telegram_id: int, identifier: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO channels (telegram_id, identifier, title) VALUES (?, ?, ?)",
                (telegram_id, identifier, title),
            )

    def remove_channel(self, identifier: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM channels WHERE identifier = ? OR CAST(telegram_id AS TEXT) = ?",
                (identifier, identifier),
            )
            return cur.rowcount > 0

    def list_channels(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY added_at").fetchall()
            return [dict(r) for r in rows]

    def get_channel_by_telegram_id(self, telegram_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None

    def pending_counts(self) -> dict[int, int]:
        """Returns {channel_id: pending_count}."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT channel_id, COUNT(*) AS n FROM media_messages WHERE status='pending' GROUP BY channel_id"
            ).fetchall()
            return {r["channel_id"]: r["n"] for r in rows}

    # --- media messages ---

    def save_media_message(
        self,
        channel_id: int,
        message_id: int,
        filename: str,
        size: int,
        mime_type: str,
        ext: str,
        date: str,
        caption: str,
    ) -> bool:
        """Insert a new media message. Returns True if inserted, False if already present."""
        with self._conn() as conn:
            try:
                conn.execute(
                    """INSERT INTO media_messages
                       (channel_id, message_id, filename, size, mime_type, ext, date, caption)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (channel_id, message_id, filename, size, mime_type, ext, date, caption),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_pending_media(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, c.title AS channel_title,
                          c.telegram_id AS channel_telegram_id,
                          c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'pending'
                   ORDER BY m.date DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_downloaded(self, media_id: int, local_path: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='downloaded', local_path=? WHERE id=?",
                (local_path, media_id),
            )

    def mark_skipped(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='skipped' WHERE id=?", (media_id,)
            )

    def get_status_counts(self) -> list[dict]:
        """Returns per-channel counts of pending/downloaded/skipped/total media."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT c.title, c.identifier,
                       SUM(CASE WHEN m.status='pending'    THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                       SUM(CASE WHEN m.status='skipped'    THEN 1 ELSE 0 END) AS skipped,
                       COUNT(m.id) AS total
                FROM channels c
                LEFT JOIN media_messages m ON m.channel_id = c.id
                GROUP BY c.id
                ORDER BY c.added_at
            """).fetchall()
            return [dict(r) for r in rows]

    def get_download_history(self, limit: int = 20) -> list[dict]:
        """Returns the most recently downloaded items, newest first."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT m.*, c.title AS channel_title
                FROM media_messages m
                JOIN channels c ON m.channel_id = c.id
                WHERE m.status = 'downloaded'
                ORDER BY m.rowid DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
