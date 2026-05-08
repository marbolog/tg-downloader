import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from db import Database
from lang_filter import should_discard
from utils import human_size, unique_path

log = logging.getLogger(__name__)

CONCURRENT_DOWNLOADS = 1


async def download_item(
    client: TelegramClient,
    db: Database,
    item: dict,
    dest: Path,
    semaphore: asyncio.Semaphore,
    message=None,
) -> bool:
    """Download one media item to dest. Returns True on success.

    item must contain: id, message_id, filename, and either channel_identifier
    or channel_telegram_id for entity resolution.

    If message is provided (a live Telethon message object), it is used directly
    and no Telegram fetch is needed.
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
                    raise ValueError("Message not found — may have been deleted from Telegram")

            await client.download_media(message, file=str(filepath))

            if should_discard(filepath, item.get("ext") or ""):
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (German): {item['filename']}")
                return True

            db.mark_downloaded(item["id"], str(filepath))
            size_str = human_size(filepath.stat().st_size) if filepath.exists() else "?"
            log.info(f"[{label}] Downloaded: {item['filename']}  ({size_str})")
            return True
        except Exception as exc:
            log.error(f"[{label}] Failed to download {item['filename']!r}: {exc}")
            return False
