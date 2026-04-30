import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from telethon import TelegramClient

log = logging.getLogger(__name__)


@dataclass
class MediaItem:
    channel: str
    message_id: int
    filename: str
    size: int         # bytes; 0 if unknown
    mime_type: str
    ext: str          # lowercase, no dot
    date: datetime
    caption: str      # truncated to 120 chars
    message: object   # original Telethon message, used for download


async def scan_channel(
    client: TelegramClient,
    channel_id: str,
    max_messages: int,
    allowed_extensions: set,
) -> list[MediaItem]:
    """Iterate the most recent max_messages messages of channel_id and return
    MediaItem records for every message that carries a downloadable file."""

    log.info(f"Connecting to channel: {channel_id!r}, limit={max_messages}")
    items: list[MediaItem] = []

    try:
        entity = await client.get_entity(channel_id)
        channel_name = getattr(entity, "title", None) or str(channel_id)
    except Exception as exc:
        log.error(f"Cannot access channel {channel_id!r}: {exc}")
        return items

    scanned = 0
    async for message in client.iter_messages(entity, limit=max_messages):
        scanned += 1

        if not message.media:
            continue

        item = _extract_item(message, channel_name)
        if item is None:
            continue

        if allowed_extensions and item.ext not in allowed_extensions:
            log.debug(f"Skipping {item.filename!r}: extension {item.ext!r} not in filter")
            continue

        items.append(item)

    log.info(f"Channel {channel_name!r}: scanned {scanned} messages, found {len(items)} media items")
    return items


def _extract_item(message, channel_name: str) -> Optional[MediaItem]:
    caption = (message.message or "")[:120]

    if message.document:
        f = message.file
        ext = (f.ext or "").lstrip(".").lower()
        filename = f.name or f"document_{message.id}{f.ext or ''}"
        return MediaItem(
            channel=channel_name,
            message_id=message.id,
            filename=filename,
            size=f.size or 0,
            mime_type=f.mime_type or "application/octet-stream",
            ext=ext,
            date=message.date,
            caption=caption,
            message=message,
        )

    if message.photo:
        f = message.file
        ext = (f.ext or ".jpg").lstrip(".").lower()
        filename = f"photo_{message.id}.{ext}"
        return MediaItem(
            channel=channel_name,
            message_id=message.id,
            filename=filename,
            size=f.size or 0,
            mime_type=f.mime_type or "image/jpeg",
            ext=ext,
            date=message.date,
            caption=caption,
            message=message,
        )

    return None
