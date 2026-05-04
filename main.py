import argparse
import asyncio
import logging
import sys

from rich.console import Console
from rich.table import Table
from telethon import TelegramClient

from config import load_config
from db import Database

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = "data/tg_downloader.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("listen", help="Start the real-time listener (main service process)")

    p = sub.add_parser("subscribe", help="Subscribe to a channel")
    p.add_argument("channel", help="@username, t.me/... URL, or numeric Telegram ID")

    p = sub.add_parser("unsubscribe", help="Unsubscribe from a channel")
    p.add_argument("channel", help="Same identifier used when subscribing")

    sub.add_parser("channels", help="List subscribed channels and pending counts")

    sub.add_parser("download", help="Choose pending media files to download")

    sub.add_parser("status", help="Show download stats per channel")

    sub.add_parser("skip", help="Interactively mark pending media as skipped")

    p = sub.add_parser("history", help="Show recently downloaded files")
    p.add_argument("--limit", type=int, default=20, metavar="N", help="Number of entries (default: 20)")

    p = sub.add_parser("scrape", help="Backfill media from channel history")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None, help="Specific channel (default: all)")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="Max messages to scan per channel (default: unlimited)")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None, help="Stop at messages older than this date")

    return parser


async def run(args) -> None:
    config = load_config("config.yaml")
    db = Database(DB_PATH)

    # These commands only touch the local DB — no Telegram connection needed.
    if args.command == "status":
        cmd_status(db)
        return
    if args.command == "history":
        cmd_history(db, args.limit)
        return
    if args.command == "skip":
        await cmd_skip(db)
        return
    if args.command == "channels":
        cmd_channels(db)
        return
    if args.command == "unsubscribe":
        cmd_unsubscribe(db, args.channel)
        return

    tg = config["telegram"]
    client = TelegramClient(tg["session_file"], tg["api_id"], tg["api_hash"])
    await client.start()

    try:
        if args.command == "listen":
            from listener import run_listener
            allowed = set(config["filters"]["extensions"])
            await run_listener(client, db, allowed)

        elif args.command == "subscribe":
            await cmd_subscribe(client, db, args.channel)

        elif args.command == "unsubscribe":
            cmd_unsubscribe(db, args.channel)

        elif args.command == "channels":
            cmd_channels(db)

        elif args.command == "download":
            from ui import select_pending_media
            from downloader import download_files
            pending = db.get_pending_media()
            selected = await select_pending_media(pending)
            if selected:
                await download_files(client, db, selected, config["download"]["destination"])
            else:
                console.print("[yellow]Nothing selected.[/yellow]")

        elif args.command == "scrape":
            await cmd_scrape(client, db, config, args.channel, args.limit, args.since)
    finally:
        await client.disconnect()


async def cmd_subscribe(client: TelegramClient, db: Database, identifier: str) -> None:
    try:
        entity = await client.get_entity(identifier)
    except Exception as exc:
        console.print(f"[red]Cannot resolve {identifier!r}: {exc}[/red]")
        return
    title = getattr(entity, "title", identifier)
    db.add_channel(entity.id, identifier, title)
    console.print(f"[green]Subscribed:[/green] {title}  (id={entity.id})")


def cmd_unsubscribe(db: Database, identifier: str) -> None:
    if db.remove_channel(identifier):
        console.print(f"[green]Unsubscribed:[/green] {identifier}")
    else:
        console.print(f"[yellow]Not found:[/yellow] {identifier}")


def cmd_channels(db: Database) -> None:
    channels = db.list_channels()
    if not channels:
        console.print("[yellow]No subscribed channels. Use 'subscribe' to add one.[/yellow]")
        return

    pending = db.pending_counts()
    table = Table(title="Subscribed Channels")
    table.add_column("Title")
    table.add_column("Identifier")
    table.add_column("Pending", justify="right")
    table.add_column("Since")

    for c in channels:
        table.add_row(
            c["title"],
            c["identifier"],
            str(pending.get(c["id"], 0)),
            c["added_at"][:10],
        )
    console.print(table)


def cmd_status(db: Database) -> None:
    rows = db.get_status_counts()
    if not rows:
        console.print("[yellow]No channels subscribed.[/yellow]")
        return

    table = Table(title="Download Status")
    table.add_column("Channel")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Downloaded", justify="right", style="green")
    table.add_column("Skipped", justify="right", style="dim")
    table.add_column("Total", justify="right")

    totals = {"pending": 0, "downloaded": 0, "skipped": 0, "total": 0}
    for r in rows:
        for k in totals:
            totals[k] += r[k] or 0
        table.add_row(
            r["title"],
            str(r["pending"] or 0),
            str(r["downloaded"] or 0),
            str(r["skipped"] or 0),
            str(r["total"] or 0),
        )
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{totals['pending']}[/bold]",
        f"[bold]{totals['downloaded']}[/bold]",
        f"[bold]{totals['skipped']}[/bold]",
        f"[bold]{totals['total']}[/bold]",
    )
    console.print(table)


def cmd_history(db: Database, limit: int) -> None:
    rows = db.get_download_history(limit)
    if not rows:
        console.print("[yellow]No downloads yet.[/yellow]")
        return

    table = Table(title=f"Recent Downloads (last {limit})")
    table.add_column("Date")
    table.add_column("Channel")
    table.add_column("File")
    table.add_column("Saved as")

    for r in rows:
        table.add_row(
            (r.get("date") or "")[:10],
            (r.get("channel_title") or "")[:25],
            (r.get("filename") or "")[:40],
            (r.get("local_path") or "")[:60],
        )
    console.print(table)


async def cmd_scrape(client: TelegramClient, db: Database, config: dict, identifier: str | None, limit: int | None, since: str | None) -> None:
    from datetime import datetime, timezone
    from listener import _extract_media

    channels = db.list_channels()
    if identifier:
        channels = [c for c in channels if c["identifier"] == identifier or str(c["telegram_id"]) == identifier]
        if not channels:
            console.print(f"[red]Channel not found:[/red] {identifier}")
            return

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)

    allowed = set(config["filters"]["extensions"])
    total_new = 0

    for ch in channels:
        label = f"since {since}" if since_dt else f"up to {limit} messages"
        console.print(f"[dim]Scanning {ch['title']} ({label})…[/dim]")
        count = 0
        scanned = 0
        async for message in client.iter_messages(ch["telegram_id"], limit=limit):
            if since_dt and message.date < since_dt:
                break
            scanned += 1
            if not message.media:
                continue
            item = _extract_media(message)
            if item is None:
                continue
            if allowed and item["ext"] not in allowed:
                continue
            inserted = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item["filename"],
                size=item["size"],
                mime_type=item["mime_type"],
                ext=item["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if inserted:
                count += 1
        console.print(f"  [green]+{count} new item(s)[/green] ({scanned} messages scanned)")
        total_new += count

    console.print(f"\n[green]Done — {total_new} new item(s) added to pending queue.[/green]")


async def cmd_skip(db: Database) -> None:
    from ui import select_pending_media
    pending = db.get_pending_media()
    selected = await select_pending_media(pending, action="skip")
    for item in selected:
        db.mark_skipped(item["id"])
    if selected:
        console.print(f"[green]Skipped {len(selected)} item(s).[/green]")


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
