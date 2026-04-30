import asyncio
import logging
import sys

from rich.console import Console
from telethon import TelegramClient

from config import load_config
from scraper import scan_channel
from ui import show_scan_summary, select_files
from downloader import download_files

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def run() -> None:
    config = load_config("config.yaml")

    tg = config["telegram"]
    client = TelegramClient(tg["session_file"], tg["api_id"], tg["api_hash"])

    console.print("[bold]Connecting to Telegram...[/bold]")
    await client.start()
    console.print("[green]Connected.[/green]\n")

    max_msgs = config["download"]["max_messages_per_channel"]
    allowed_ext = set(config["filters"]["extensions"])  # empty set = no filter

    all_items = []
    items_by_channel: dict = {}

    for channel_id in config["channels"]:
        console.print(f"Scanning [cyan]{channel_id}[/cyan] ...")
        items = await scan_channel(client, channel_id, max_msgs, allowed_ext)
        items_by_channel[channel_id] = items
        all_items.extend(items)

    console.print()
    show_scan_summary(items_by_channel)

    selected = select_files(all_items)

    if not selected:
        console.print("[yellow]No files selected. Exiting.[/yellow]")
        await client.disconnect()
        return

    console.print(f"\nStarting download of [cyan]{len(selected)}[/cyan] file(s)...\n")

    await download_files(
        client,
        selected,
        config["download"]["destination"],
    )

    await client.disconnect()
    log.info("Session closed.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
