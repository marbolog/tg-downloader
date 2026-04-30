import logging

from telethon import TelegramClient, events

from db import Database

log = logging.getLogger(__name__)


async def run_listener(
    client: TelegramClient, db: Database, allowed_extensions: set
) -> None:
    """Start the real-time listener. Blocks until the client disconnects."""
    channels = db.list_channels()
    log.info(f"Listening — {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  · {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed_extensions)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()


async def _handle(event, db: Database, allowed_extensions: set) -> None:
    if not event.message.media:
        return

    # Check DB on every message so subscribe/unsubscribe takes effect without restart
    channel = db.get_channel_by_telegram_id(event.chat_id)
    if channel is None:
        return

    item = _extract_media(event.message)
    if item is None:
        return

    if allowed_extensions and item["ext"] not in allowed_extensions:
        log.debug(f"Skipping {item['filename']!r}: extension not in filter")
        return

    inserted = db.save_media_message(
        channel_id=channel["id"],
        message_id=event.message.id,
        filename=item["filename"],
        size=item["size"],
        mime_type=item["mime_type"],
        ext=item["ext"],
        date=event.message.date.isoformat(),
        caption=(event.message.message or "")[:120],
    )
    if inserted:
        log.info(f"[{channel['title']}] New media: {item['filename']} ({item['size']} B)")


def _extract_media(message) -> dict | None:
    if message.document:
        f = message.file
        ext = (f.ext or "").lstrip(".").lower()
        return {
            "filename": f.name or f"document_{message.id}{f.ext or ''}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "application/octet-stream",
            "ext": ext,
        }
    if message.photo:
        f = message.file
        ext = (f.ext or ".jpg").lstrip(".").lower()
        return {
            "filename": f"photo_{message.id}.{ext}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "image/jpeg",
            "ext": ext,
        }
    return None
