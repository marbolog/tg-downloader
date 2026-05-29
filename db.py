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
    id            INTEGER PRIMARY KEY,
    channel_id    INTEGER NOT NULL REFERENCES channels(id),
    message_id    INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    mime_type     TEXT,
    ext           TEXT,
    date          TEXT,
    caption       TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    local_path    TEXT,
    downloaded_at TEXT,
    language      TEXT,
    UNIQUE(channel_id, message_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    text,
    media_id           UNINDEXED,
    chunk_idx          UNINDEXED,
    page               UNINDEXED,
    chapter            UNINDEXED,
    filename           UNINDEXED,
    channel_identifier UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Each entry is tried once; OperationalError from a duplicate column is silently swallowed.
_MIGRATIONS = [
    "ALTER TABLE media_messages ADD COLUMN downloaded_at TEXT",
    "ALTER TABLE media_messages ADD COLUMN language TEXT",
    "ALTER TABLE media_messages ADD COLUMN file_hash TEXT",
    "ALTER TABLE media_messages ADD COLUMN indexed_at TEXT",
]


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            _apply_migrations(conn)
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
    ) -> int | None:
        """Insert a new media message. Returns the inserted row id, or None if already present."""
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO media_messages
                       (channel_id, message_id, filename, size, mime_type, ext, date, caption)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (channel_id, message_id, filename, size, mime_type, ext, date, caption),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

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

    def get_downloaded_media(self) -> list[dict]:
        """Returns files currently on disk (status='downloaded'), newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, c.title AS channel_title,
                          c.telegram_id AS channel_telegram_id,
                          c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'downloaded'
                   ORDER BY m.downloaded_at DESC, m.date DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_max_message_id(self, channel_id: int) -> int | None:
        """Returns the highest recorded message_id for a channel, or None if none exist."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(message_id) AS max_id FROM media_messages WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            return row["max_id"] if row else None

    def get_expired_files(self, retention_days: int) -> list[dict]:
        """Returns downloaded files whose downloaded_at is older than retention_days."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM media_messages
                   WHERE status = 'downloaded'
                     AND downloaded_at IS NOT NULL
                     AND downloaded_at < datetime('now', ?)""",
                (f"-{retention_days} days",),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_downloaded(self, media_id: int, local_path: str, language: str | None = None, file_hash: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='downloaded', local_path=?, downloaded_at=datetime('now'), language=?, file_hash=? WHERE id=?",
                (local_path, language, file_hash, media_id),
            )

    def mark_skipped(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='skipped' WHERE id=?", (media_id,)
            )

    def mark_discarded(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='discarded', local_path=NULL WHERE id=?",
                (media_id,),
            )
            conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))

    def mark_expired(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET status='expired', local_path=NULL WHERE id=?",
                (media_id,),
            )
            conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))

    def get_unindexed_downloaded(self) -> list[dict]:
        """Returns downloaded files with no indexed_at timestamp."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, c.title AS channel_title,
                          c.telegram_id AS channel_telegram_id,
                          c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'downloaded' AND m.indexed_at IS NULL
                   ORDER BY m.downloaded_at DESC, m.date DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_indexed(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE media_messages SET indexed_at=datetime('now') WHERE id=?",
                (media_id,),
            )

    # --- FTS5 full-text search ---

    def search_fts_index_file(
        self,
        media_id: int,
        chunks: list[dict],
        filename: str,
        channel_identifier: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
            conn.executemany(
                """INSERT INTO search_fts
                   (text, media_id, chunk_idx, page, chapter, filename, channel_identifier)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        c["text"],
                        str(media_id),
                        c["chunk_idx"],
                        c.get("page"),
                        c.get("chapter"),
                        filename,
                        channel_identifier,
                    )
                    for c in chunks
                ],
            )

    def search_fts_delete_file(self, media_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))

    def search_fts_query(
        self, q: str, top_k: int = 8, channel_identifier: str = ""
    ) -> list[dict]:
        """BM25-ranked FTS5 search. Single source of truth for the search query
        used by the CLI (`ask`) and the web UI (`/api/search`, `/api/ask`)."""
        select = """SELECT media_id, filename, page, chapter, channel_identifier,
                           snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                    FROM search_fts
                    WHERE search_fts MATCH ?"""
        with self._conn() as conn:
            if channel_identifier:
                rows = conn.execute(
                    select + " AND channel_identifier = ? ORDER BY rank LIMIT ?",
                    (q, channel_identifier, top_k),
                ).fetchall()
            else:
                rows = conn.execute(
                    select + " ORDER BY rank LIMIT ?",
                    (q, top_k),
                ).fetchall()
            return [
                {
                    "media_id": int(r["media_id"]),
                    "filename": r["filename"],
                    "page": r["page"],
                    "chapter": r["chapter"],
                    "channel_identifier": r["channel_identifier"],
                    "text": r["text"],
                }
                for r in rows
            ]

    def search_fts_missing_media_ids(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.id AS media_id, m.filename, m.ext,
                          m.local_path, c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'downloaded'
                     AND m.ext IN ('pdf', 'epub')
                     AND m.indexed_at IS NULL
                     AND CAST(m.id AS TEXT) NOT IN (
                         SELECT DISTINCT media_id FROM search_fts
                     )"""
            ).fetchall()
            return [dict(r) for r in rows]

    # --- web UI read queries (also used by host-side tools) ---

    def get_media(self, media_id: int) -> dict | None:
        """Single media row joined with its channel, by id."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT m.*, c.title AS channel_title, c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.id = ?""",
                (media_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_downloaded_files(
        self, channel: str = "", language: str = "", ids: list[int] | None = None
    ) -> list[dict]:
        """Downloaded files for the web UI grid, newest first. Optional filters by
        channel identifier, language (use '__unknown__' for NULL), and explicit id
        list. Deduplication is applied by the caller (presentation concern)."""
        where = ["m.status = 'downloaded'"]
        params: list = []
        if channel:
            where.append("c.identifier = ?")
            params.append(channel)
        if language == "__unknown__":
            where.append("m.language IS NULL")
        elif language:
            where.append("m.language = ?")
            params.append(language)
        if ids:
            placeholders = ",".join("?" * len(ids))
            where.append(f"m.id IN ({placeholders})")
            params.extend(ids)

        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT m.id, m.filename, m.size, m.ext, m.date, m.downloaded_at,
                           m.local_path, m.language, m.file_hash,
                           c.title AS channel_title, c.identifier AS channel_identifier
                    FROM media_messages m
                    JOIN channels c ON m.channel_id = c.id
                    WHERE {' AND '.join(where)}
                    ORDER BY m.downloaded_at DESC NULLS LAST, m.date DESC""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def language_counts(self) -> list[dict]:
        """Counts of downloaded files per language (NULL → '__unknown__')."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT COALESCE(m.language, '__unknown__') AS language, COUNT(*) AS count
                   FROM media_messages m
                   WHERE m.status = 'downloaded'
                   GROUP BY m.language
                   HAVING count > 0
                   ORDER BY count DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def channel_counts(self) -> list[dict]:
        """Per-channel downloaded-file counts, channels with at least one file."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.identifier, c.title,
                          SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS count
                   FROM channels c
                   LEFT JOIN media_messages m ON m.channel_id = c.id
                   GROUP BY c.id
                   HAVING count > 0
                   ORDER BY c.title"""
            ).fetchall()
            return [dict(r) for r in rows]

    def find_duplicate_groups(self) -> list[dict]:
        """Groups of downloaded files sharing a SHA-256 hash, most copies first."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT file_hash, COUNT(*) AS copies, MIN(filename) AS example
                   FROM media_messages
                   WHERE status = 'downloaded' AND file_hash IS NOT NULL
                   GROUP BY file_hash
                   HAVING copies > 1
                   ORDER BY copies DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def health_snapshot(self) -> dict:
        """One-row operational summary for the hourly heartbeat log. Cheap
        aggregate queries — the numbers an operator needs to tell at a glance
        whether the listener is keeping up or something is being missed."""
        with self._conn() as conn:
            def scalar(sql: str, params=()) -> int:
                return conn.execute(sql, params).fetchone()[0]

            return {
                "downloaded": scalar("SELECT COUNT(*) FROM media_messages WHERE status='downloaded'"),
                "pending": scalar("SELECT COUNT(*) FROM media_messages WHERE status='pending'"),
                "downloaded_last_hour": scalar(
                    "SELECT COUNT(*) FROM media_messages "
                    "WHERE status='downloaded' AND downloaded_at >= datetime('now', '-1 hour')"
                ),
                "discarded": scalar("SELECT COUNT(*) FROM media_messages WHERE status='discarded'"),
                "expired": scalar("SELECT COUNT(*) FROM media_messages WHERE status='expired'"),
                "indexed": scalar("SELECT COUNT(DISTINCT media_id) FROM search_fts"),
                "index_pending": scalar(
                    "SELECT COUNT(*) FROM media_messages m "
                    "WHERE m.status='downloaded' AND m.ext IN ('pdf','epub') "
                    "AND m.indexed_at IS NULL "
                    "AND CAST(m.id AS TEXT) NOT IN (SELECT DISTINCT media_id FROM search_fts)"
                ),
                "channels_no_messages": scalar(
                    "SELECT COUNT(*) FROM channels c "
                    "WHERE NOT EXISTS (SELECT 1 FROM media_messages m WHERE m.channel_id = c.id)"
                ),
            }

    def get_status_counts(self) -> list[dict]:
        """Returns per-channel counts of each status and total."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT c.title, c.identifier,
                       SUM(CASE WHEN m.status='pending'    THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                       SUM(CASE WHEN m.status='discarded'  THEN 1 ELSE 0 END) AS discarded,
                       SUM(CASE WHEN m.status='expired'    THEN 1 ELSE 0 END) AS expired,
                       SUM(CASE WHEN m.status='skipped'    THEN 1 ELSE 0 END) AS skipped,
                       COUNT(m.id) AS total
                FROM channels c
                LEFT JOIN media_messages m ON m.channel_id = c.id
                GROUP BY c.id
                ORDER BY c.added_at
            """).fetchall()
            return [dict(r) for r in rows]

    def get_untagged_downloaded(self) -> list[dict]:
        """Returns downloaded files with no language tag and a valid local_path."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'downloaded' AND m.language IS NULL
                     AND m.local_path IS NOT NULL
                   ORDER BY m.downloaded_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def set_language(self, media_id: int, language: str | None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE media_messages SET language=? WHERE id=?", (language, media_id))

    def get_untagged_for_hash(self) -> list[dict]:
        """Returns downloaded files with no file_hash and a valid local_path."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, c.identifier AS channel_identifier
                   FROM media_messages m
                   JOIN channels c ON m.channel_id = c.id
                   WHERE m.status = 'downloaded' AND m.file_hash IS NULL
                     AND m.local_path IS NOT NULL
                   ORDER BY m.downloaded_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def set_file_hash(self, media_id: int, file_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE media_messages SET file_hash=? WHERE id=?", (file_hash, media_id))

    def get_download_history(self, limit: int = 20) -> list[dict]:
        """Returns the most recently downloaded items, newest first."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT m.*, c.title AS channel_title
                FROM media_messages m
                JOIN channels c ON m.channel_id = c.id
                WHERE m.status = 'downloaded'
                ORDER BY m.downloaded_at DESC NULLS LAST, m.rowid DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as exc:
            # Only swallow the "already applied" case (duplicate column on ADD COLUMN).
            # Anything else (locked DB, disk full, syntax error) must surface.
            if "duplicate column name" not in str(exc).lower():
                raise
