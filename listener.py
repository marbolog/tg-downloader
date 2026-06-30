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

    await _flush_pending(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences)
    await _heal_missing(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences)
    await _backfill_missed(client, db, allowed, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences)

    asyncio.create_task(_heal_search_index(db))
    asyncio.create_task(_cleanup_loop(db, retention_days))
    asyncio.create_task(_heartbeat_loop(db))
    asyncio.create_task(_backfill_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences,
    ))
    asyncio.create_task(_deep_reconcile_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences,
    ))

    channels = db.list_channels()
    log.info(f"Listening -- {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  . {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed, client, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    # Catch up on anything that arrived while we were offline / mid-reconnect.
    # Must run after the handler above is registered so loaded updates are processed.
    await client.catch_up()

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()


async def _flush_pending(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences
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
                        topic_min_occurrences=topic_min_occurrences)
          for item in pending],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Flush complete: {ok}/{len(pending)} succeeded")


async def _heal_missing(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences
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
                        topic_min_occurrences=topic_min_occurrences)
          for item in missing],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Heal complete: {ok}/{len(missing)} restored")


async def _backfill_missed(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches,
    topic_min_occurrences, warn_empty: bool = True
) -> None:
    """Fetch messages that arrived while the service was down and download them.

    `warn_empty` controls the per-channel "no prior messages" warning: useful once
    at startup, but suppressed by the hourly safety-net loop so known-empty
    channels don't emit the same warning every hour (the heartbeat already
    surfaces `channels_no_messages`)."""
    for ch in db.list_channels():
        max_id = db.get_max_message_id(ch["id"])
        if max_id is None:
            if warn_empty:
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
                ))

        if tasks:
            log.info(f"Backfilling {len(tasks)} missed item(s) from {ch['title']}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Backfill {ch['title']}: {ok}/{len(tasks)} succeeded")


async def _heal_search_index(db: Database) -> None:
    missing = db.search_fts_missing_media_ids()
    if not missing:
        return
    log.info(f"Search index heal: {len(missing)} file(s) not yet indexed, indexing in background...")
    from search.indexer import index_file
    indexed = 0
    textless = 0
    errors = 0
    for item in missing:
        try:
            result = await asyncio.to_thread(
                index_file, db,
                item["media_id"], item["local_path"], item["ext"],
                item["filename"], item.get("channel_identifier", ""),
            )
            # True = chunks stored; False = no extractable text (image-only PDF),
            # already marked processed by index_file so it won't be retried.
            if result:
                indexed += 1
            else:
                textless += 1
        except Exception as exc:
            errors += 1
            log.warning(f"Search heal failed for {item['filename']!r}: {exc}")
    # Distinguish the three outcomes: a high `textless` count is normal (scanned
    # magazines); a non-zero `errors` count is the only line worth acting on.
    log.info(
        f"Search index heal complete: {indexed} indexed, {textless} no text, "
        f"{errors} error(s) (of {len(missing)})"
    )


async def _heartbeat_loop(db: Database) -> None:
    """Log one structured operational summary line every hour. This is the surface
    an operator scans to answer 'is the listener keeping up / is anything being
    missed?' without writing SQL: download rate, queue depth, index backlog, and
    channels that have never produced a message (likely not joined / wrong id)."""
    while True:
        await asyncio.sleep(3600)
        try:
            s = db.health_snapshot()
            log.info(
                "Heartbeat: "
                f"downloaded={s['downloaded']} (+{s['downloaded_last_hour']}/h) "
                f"pending={s['pending']} indexed={s['indexed']} "
                f"index_pending={s['index_pending']} "
                f"discarded={s['discarded']} expired={s['expired']} "
                f"channels_no_messages={s['channels_no_messages']}"
            )
        except Exception as exc:
            log.error(f"Heartbeat error: {exc}", exc_info=True)


async def _backfill_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences
) -> None:
    """Re-run backfill every hour as a safety net against silent update-stream
    stalls. Telethon's real-time update channel can go stale after a network blip
    while the TCP connection (and this asyncio loop) stays alive -- the process
    keeps logging heartbeats but no `events.NewMessage` ever fires, so downloads
    silently stop until the next restart. Polling each channel for messages newer
    than the last recorded id closes that gap within the hour, independent of why
    real-time delivery stopped. Harmless when real-time is healthy: `min_id` is
    already current, so nothing new is fetched and no message is double-downloaded
    (save_media_message dedups on message_id)."""
    while True:
        await asyncio.sleep(3600)
        try:
            await _backfill_missed(
                client, db, allowed, dest, semaphore,
                topic_keywords, topic_min_matches, topic_min_occurrences,
                warn_empty=False,
            )
        except Exception as exc:
            log.error(f"Periodic backfill error: {exc}", exc_info=True)


# How many recent messages per channel the deep-reconcile pass re-examines, and
# how often. The hourly backfill only fetches ids *newer* than the highest one
# recorded, so a file dropped mid-burst (while a higher id from the same burst
# landed) is permanently below that watermark. This pass re-walks a fixed recent
# window with no watermark and lets save_media_message's dedup insert only the
# genuinely-missing ids -- closing that hole for recent drops without the cost of
# scanning full history. Older holes are recovered manually with `scrape`.
RECONCILE_WINDOW = 400
RECONCILE_INTERVAL_SECONDS = 86400  # daily


async def _deep_reconcile_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences
) -> None:
    """Once a day, re-scan each channel's recent window ignoring the backfill
    watermark, recovering media that real-time delivery dropped mid-burst."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            await _deep_reconcile(
                client, db, allowed, dest, semaphore,
                topic_keywords, topic_min_matches, topic_min_occurrences,
            )
        except Exception as exc:
            log.error(f"Deep reconcile error: {exc}", exc_info=True)


async def _deep_reconcile(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences
) -> None:
    for ch in db.list_channels():
        recorded = db.get_recorded_message_ids(ch["id"])
        if not recorded:
            continue  # never seen this channel — that's `scrape`'s job, not reconcile's
        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            log.warning(f"Deep reconcile: cannot resolve {ch['identifier']!r}: {exc}")
            continue

        tasks = []
        async for message in client.iter_messages(entity, limit=RECONCILE_WINDOW):
            if not message.media or message.id in recorded:
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
                ))

        if tasks:
            log.info(f"Deep reconcile: recovered {len(tasks)} mid-burst miss(es) from {ch['title']}")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Deep reconcile {ch['title']}: {ok}/{len(tasks)} downloaded")


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
    topic_keywords, topic_min_matches, topic_min_occurrences
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
