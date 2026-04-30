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
console = Console()


async def download_files(
    client: TelegramClient,
    db: Database,
    selected_items: list[dict],
    destination: str,
) -> None:
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    failed = []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        for item in selected_items:
            filepath = unique_path(dest / item["filename"])

            task_id = progress.add_task(
                f"{(item.get('channel_title') or '')[:20]} / {item['filename'][:40]}",
                total=item.get("size") or None,
            )

            def make_cb(tid, size_hint):
                def cb(current, total):
                    progress.update(tid, completed=current, total=total or size_hint or None)
                return cb

            try:
                # Re-fetch the message from Telegram by channel ID + message ID
                message = await client.get_messages(
                    item["channel_telegram_id"], ids=item["message_id"]
                )
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

    console.print(
        f"\n[green]Downloaded {len(selected_items) - len(failed)}/{len(selected_items)} file(s)[/green]"
    )
    console.print(f"[green]Destination: {destination}[/green]")

    if failed:
        console.print(f"\n[yellow]Failed ({len(failed)}):[/yellow]")
        for item in failed:
            console.print(f"  - {item.get('channel_title', '')} / {item['filename']}")
