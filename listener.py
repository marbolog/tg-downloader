import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel, PeerChat

from db import Database
from downloader import download_item

log = logging.getLogger(__name__)


async def run_listener(client: TelegramClient, db: Database, config: dict) -> None:
    """Start the real-time listener. Blocks until the client disconnects."""
    destination = Path(config["download"]["destination"])
    allowed = set(config["filters"]["extensions"])
    retention_days = config["download"]["retention_days"]
    concurrent_downloads = config["download"]["concurrent_downloads"]
    topic_keywords = config["filters"].get("discard_topics") or {}
    topic_min_matches = config["filters"].get("topic_min_matches", 2)
    topic_min_occurrences = config["filters"].get("topic_min_keyword_occurrences", 1)
    destination.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrent_downloads)

    rag_config = config.get("rag", {})
    indexer = None
    if rag_config.get("enabled"):
        try:
            from rag.indexer import Indexer
            indexer = Indexer(rag_config)
        except Exception as exc:
            log.error(f"RAG: failed to initialise indexer -- {exc}. Continuing without RAG.")

    await _flush_pending(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
    await _heal_missing(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
    await _backfill_missed(client, db, allowed, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)

    asyncio.create_task(_cleanup_loop(db, retention_days))

    channels = db.list_channels()
    log.info(f"Listening -- {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  . {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed, client, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()


async def _flush_pending(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Download all items that are pending in the DB (e.g. from a previous scrape)."""
    pending = db.get_pending_media()
    if not pending:
        return
    log.info(f"Flushing {len(pending)} pending item(s) from previous session(s)...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        indexer=indexer)
          for item in pending],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Flush complete: {ok}/{len(pending)} succeeded")


async def _heal_missing(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Re-download files marked 'downloaded' in the DB but absent from disk."""
    downloaded = db.get_downloaded_media()
    missing = [
        item for item in downloaded
        if not item.get("local_path") or not Path(item["local_path"]).exists()
    ]
    if not missing:
        return
    log.info(f"Healing {len(missing)} file(s) present in DB but missing from disk...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        indexer=indexer)
          for item in missing],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Heal complete: {ok}/{len(missing)} restored")


async def _backfill_missed(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Fetch messages that arrived while the service was down and download them."""
    for ch in db.list_channels():
        max_id = db.get_max_message_id(ch["id"])
        if max_id is None:
            log.warning(
                f"Backfill: no prior messages recorded for {ch['title']!r} -- "
                f"run 'scrape --channel {ch['identifier']}' to pull existing history"
            )
            continue

        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            log.warning(f"Backfill: cannot resolve {ch['identifier']!r}: {exc}")
            continue

        tasks = []
        async for message in client.iter_messages(entity, min_id=max_id):
            if not message.media:
                continue
            item_meta = _extract_media(message)
            if item_meta is None:
                continue
            if allowed and item_meta["ext"] not in allowed:
                continue
            db_id = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item_meta["filename"],
                size=item_meta["size"],
                mime_type=item_meta["mime_type"],
                ext=item_meta["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if db_id:
                tasks.append(download_item(
                    client, db,
                    {
                        "id": db_id,
                        "channel_identifier": ch["identifier"],
                        "channel_telegram_id": ch["telegram_id"],
                        "channel_title": ch["title"],
                        "message_id": message.id,
                        "filename": item_meta["filename"],
                        "size": item_meta["size"],
                        "ext": item_meta["ext"],
                    },
                    dest, semaphore, message=message,
                    topic_keywords=topic_keywords,
                    topic_min_matches=topic_min_matches,
                    topic_min_occurrences=topic_min_occurrences,
                    indexer=indexer,
                ))

        if tasks:
            log.info(f"Backfilling {len(tasks)} missed item(s) from {ch['title']}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Backfill {ch['title']}: {ok}/{len(tasks)} succeeded")


async def _cleanup_loop(db: Database, retention_days: int) -> None:
    """Run retention cleanup once on startup, then every hour. No-op if retention_days <= 0."""
    if retention_days <= 0:
        return
    while True:
        try:
            _run_cleanup(db, retention_days)
        except Exception as exc:
            log.error(f"Cleanup error: {exc}", exc_info=True)
        await asyncio.sleep(3600)


def _run_cleanup(db: Database, retention_days: int) -> None:
    expired = db.get_expired_files(retention_days)
    if not expired:
        return
    deleted = 0
    for item in expired:
        if item.get("local_path"):
            p = Path(item["local_path"])
            if p.exists():
                p.unlink()
                deleted += 1
        db.mark_expired(item["id"])
    log.info(
        f"Retention cleanup: {deleted} file(s) deleted, "
        f"{len(expired)} record(s) marked expired (>{retention_days}d)"
    )


async def _handle(
    event, db, allowed, client, dest, semaphore,
    topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    if not event.message.media:
        return

    peer = event.message.peer_id
    if isinstance(peer, PeerChannel):
        raw_id = peer.channel_id
    elif isinstance(peer, PeerChat):
        raw_id = peer.chat_id
    else:
        return

    channel = db.get_channel_by_telegram_id(raw_id)
    if channel is None:
        return

    item_meta = _extract_media(event.message)
    if item_meta is None:
        return

    if allowed and item_meta["ext"] not in allowed:
        log.debug(f"Skipping {item_meta['filename']!r}: extension not in filter")
        return

    db_id = db.save_media_message(
        channel_id=channel["id"],
        message_id=event.message.id,
        filename=item_meta["filename"],
        size=item_meta["size"],
        mime_type=item_meta["mime_type"],
        ext=item_meta["ext"],
        date=event.message.date.isoformat(),
        caption=(event.message.message or "")[:120],
    )
    if db_id:
        log.info(f"[{channel['title']}] New media: {item_meta['filename']} ({item_meta['size']} B) -- queuing download")
        asyncio.create_task(download_item(
            client, db,
            {
                "id": db_id,
                "channel_identifier": channel["identifier"],
                "channel_telegram_id": channel["telegram_id"],
                "channel_title": channel["title"],
                "message_id": event.message.id,
                "filename": item_meta["filename"],
                "size": item_meta["size"],
                "ext": item_meta["ext"],
            },
            dest, semaphore, message=event.message,
            topic_keywords=topic_keywords,
            topic_min_matches=topic_min_matches,
            topic_min_occurrences=topic_min_occurrences,
            indexer=indexer,
        ))


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
