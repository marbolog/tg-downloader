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
        cmd_skip(db)
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
            selected = select_pending_media(pending)
            if selected:
                await download_files(client, db, selected, config["download"]["destination"])
            else:
                console.print("[yellow]Nothing selected.[/yellow]")
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


def cmd_skip(db: Database) -> None:
    from ui import select_pending_media
    pending = db.get_pending_media()
    selected = select_pending_media(pending, action="skip")
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
