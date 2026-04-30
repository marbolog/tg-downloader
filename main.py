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

    return parser


async def run(args) -> None:
    config = load_config("config.yaml")
    db = Database(DB_PATH)

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


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
