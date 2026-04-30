import logging
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
)
from telethon import TelegramClient

from scraper import MediaItem
from utils import unique_path

log = logging.getLogger(__name__)
console = Console()


async def download_files(
    client: TelegramClient,
    selected_items: list[MediaItem],
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
            filepath = unique_path(dest / item.filename)

            task_id = progress.add_task(
                f"{item.channel[:20]} / {item.filename[:40]}",
                total=item.size or None,
            )

            def make_progress_cb(tid, total_hint):
                def cb(current, total):
                    progress.update(tid, completed=current, total=total or total_hint or None)
                return cb

            try:
                await client.download_media(
                    item.message,
                    file=str(filepath),
                    progress_callback=make_progress_cb(task_id, item.size),
                )
                log.info(f"Downloaded: {filepath}")
            except Exception as exc:
                log.error(f"Failed to download {item.filename!r}: {exc}")
                console.print(f"[red]  Error: {item.filename}: {exc}[/red]")
                failed.append(item)

    console.print(f"\n[green]Downloaded {len(selected_items) - len(failed)}/{len(selected_items)} file(s)[/green]")
    console.print(f"[green]Destination: {destination}[/green]")

    if failed:
        console.print(f"\n[yellow]Failed ({len(failed)}):[/yellow]")
        for item in failed:
            console.print(f"  - {item.channel} / {item.filename}")


