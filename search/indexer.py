"""High-level indexing entry point.

index_file(db, media_id, filepath, ext, filename, channel_identifier) — chunks and inserts.
Designed to run in a thread (asyncio.to_thread) — all I/O is synchronous.
"""
import logging
from pathlib import Path

from db import Database
from search.chunker import chunk_file, SUPPORTED_EXTS

log = logging.getLogger(__name__)


def index_file(
    db: Database,
    media_id: int,
    filepath: str | Path,
    ext: str,
    filename: str,
    channel_identifier: str = "",
) -> bool:
    if ext not in SUPPORTED_EXTS:
        return False
    path = Path(filepath)
    if not path.exists():
        log.warning(f"index_file: path not found: {path}")
        return False
    chunks = chunk_file(path, ext)
    if not chunks:
        log.debug(f"index_file: no chunks extracted from {filename}")
        return False
    db.search_fts_index_file(
        media_id=media_id,
        chunks=chunks,
        filename=filename,
        channel_identifier=channel_identifier,
    )
    log.info(f"Indexed {len(chunks)} chunk(s) for {filename!r} (media_id={media_id})")
    return True
