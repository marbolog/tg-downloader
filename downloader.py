import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from db import Database
from lang_filter import DISCARD_LANG, analyze_file
from utils import compute_sha256, human_size, unique_path

log = logging.getLogger(__name__)


async def download_item(
    client: TelegramClient,
    db: Database,
    item: dict,
    dest: Path,
    semaphore: asyncio.Semaphore,
    *,
    message=None,
    topic_keywords: dict | None = None,
    topic_min_matches: int = 2,
    topic_min_occurrences: int = 1,
    discard_newspapers: bool = False,
) -> bool:
    """Download one media item to dest. Returns True on success.

    item must contain: id, message_id, filename, and either channel_identifier
    or channel_telegram_id for entity resolution.

    If message is provided (a live Telethon message object), it is used directly
    and no Telegram fetch is needed.

    The file is indexed via FTS5 after a successful download in a background asyncio task.
    """
    async with semaphore:
        filepath = unique_path(dest / item["filename"])
        label = item.get("channel_title") or item.get("channel_identifier") or str(item.get("channel_telegram_id", "?"))
        try:
            if message is None:
                identifier = item.get("channel_identifier") or item["channel_telegram_id"]
                entity = await client.get_entity(identifier)
                message = await client.get_messages(entity, ids=item["message_id"])
                if message is None:
                    db.mark_discarded(item["id"])
                    log.warning(
                        f"[{label}] Message {item['message_id']} not found on Telegram "
                        f"(deleted?) -- {item['filename']!r} marked discarded"
                    )
                    return True

            await client.download_media(message, file=str(filepath))

            ext = item.get("ext") or ""

            lang, topic, is_newspaper = analyze_file(
                filepath, ext, topic_keywords, topic_min_matches, topic_min_occurrences,
                discard_newspapers=discard_newspapers,
            )

            if lang == DISCARD_LANG:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (German): {item['filename']}")
                return True

            if topic:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (topic: {topic}): {item['filename']}")
                return True

            if is_newspaper:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (newspaper): {item['filename']}")
                return True

            file_hash = None
            try:
                file_hash = await asyncio.to_thread(compute_sha256, filepath)
            except Exception as exc:
                log.warning(f"[{label}] Hash failed for {item['filename']!r}: {exc}")

            db.mark_downloaded(item["id"], str(filepath), language=lang, file_hash=file_hash)
            size_str = human_size(filepath.stat().st_size) if filepath.exists() else "?"
            lang_tag = f" [{lang}]" if lang else ""
            log.info(f"[{label}] Downloaded: {item['filename']}  ({size_str}){lang_tag}")

            asyncio.create_task(_index_async(
                db,
                item["id"],
                str(filepath),
                item.get("ext", ""),
                item.get("filename", filepath.name),
                item.get("channel_identifier", ""),
            ))

            return True
        except Exception as exc:
            log.error(f"[{label}] Failed to download {item['filename']!r}: {exc}")
            return False


async def _index_async(
    db: Database, media_id: int, filepath: str, ext: str, filename: str, channel_identifier: str = ""
) -> None:
    """Index a downloaded file via FTS5 in the background. Errors are logged, not raised."""
    from search.indexer import index_file

    try:
        await asyncio.to_thread(index_file, db, media_id, filepath, ext, filename, channel_identifier)
    except Exception as exc:
        log.warning(f"FTS5 indexing failed for {filename!r}: {exc}")
