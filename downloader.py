import asyncio
import logging
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from telethon import TelegramClient

from db import Database
from utils import unique_path

log = logging.getLogger(__name__)

CONCURRENT_DOWNLOADS = 3


async def download_files(
    client: TelegramClient,
    db: Database,
    selected_items: list[dict],
    destination: str,
    console: Console,
) -> None:
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    failed = []

    # Resolve each unique channel once by its stored identifier (@username / URL).
    # Using the identifier avoids needing the entity cache (access hash), which is
    # not preserved in StringSession across restarts.
    channel_entities: dict[int, object] = {}
    for item in selected_items:
        cid = item["channel_telegram_id"]
        if cid in channel_entities:
            continue
        identifier = item.get("channel_identifier") or cid
        try:
            channel_entities[cid] = await client.get_entity(identifier)
        except Exception as exc:
            log.warning(f"Cannot resolve channel {identifier!r}: {exc}")
            channel_entities[cid] = None

    semaphore = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:

        async def download_one(item: dict) -> None:
            entity = channel_entities.get(item["channel_telegram_id"])
            if entity is None:
                console.print(f"[red]  Skipping {item['filename']}: channel not resolvable[/red]")
                failed.append(item)
                return

            filepath = unique_path(dest / item["filename"])
            task_id = progress.add_task(
                f"{(item.get('channel_title') or '')[:20]} / {item['filename'][:40]}",
                total=item.get("size") or None,
            )

            def make_cb(tid, size_hint):
                def cb(current, total):
                    progress.update(tid, completed=current, total=total or size_hint or None)
                return cb

            async with semaphore:
                try:
                    message = await client.get_messages(entity, ids=item["message_id"])
                    if message is None:
                        raise ValueError("Message not found — it may have been deleted")

                    await client.download_media(
                        message,
                        file=str(filepath),
                        progress_callback=make_cb(task_id, item.get("size")),
                    )
                    db.mark_downloaded(item["id"], str(filepath))
                    log.info(f"Downloaded: {filepath}")
                except Exception as exc:
                    log.error(f"Failed to download {item['filename']!r}: {exc}")
                    console.print(f"[red]  Error: {item['filename']}: {exc}[/red]")
                    failed.append(item)

        await asyncio.gather(*[download_one(item) for item in selected_items])

    console.print(
        f"\n[green]Downloaded {len(selected_items) - len(failed)}/{len(selected_items)} file(s)[/green]"
    )
    console.print(f"[green]Destination: {destination}[/green]")

    if failed:
        console.print(f"\n[yellow]Failed ({len(failed)}):[/yellow]")
        for item in failed:
            console.print(f"  - {item.get('channel_title', '')} / {item['filename']}")
