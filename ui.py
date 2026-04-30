from rich.console import Console
from rich.table import Table
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from scraper import MediaItem
from utils import human_size

console = Console()


def show_scan_summary(items_by_channel: dict[str, list[MediaItem]]) -> None:
    total_items = sum(len(v) for v in items_by_channel.values())
    total_bytes = sum(item.size for items in items_by_channel.values() for item in items)

    table = Table(title="Scan Summary", show_footer=True)
    table.add_column("Channel", footer="Total")
    table.add_column("Files", justify="right", footer=str(total_items))
    table.add_column("Total size", justify="right", footer=human_size(total_bytes))

    for channel, items in items_by_channel.items():
        channel_size = sum(i.size for i in items)
        table.add_row(channel, str(len(items)), human_size(channel_size))

    console.print(table)
    console.print()


def select_files(all_items: list[MediaItem]) -> list[MediaItem]:
    if not all_items:
        console.print("[yellow]No media files found in the scanned channels.[/yellow]")
        return []

    choices = [
        Choice(
            value=item,
            name=_format_choice(item),
        )
        for item in all_items
    ]

    console.print(
        "[dim]Space[/dim]=toggle  [dim]A[/dim]=select all  "
        "[dim]↑↓[/dim]=navigate  [dim]Enter[/dim]=confirm\n"
    )

    selected = inquirer.checkbox(
        message=f"Select files to download ({len(all_items)} available):",
        choices=choices,
        cycle=True,
        transformer=lambda result: f"{len(result)} file(s) selected",
    ).execute()

    return selected


def _format_choice(item: MediaItem) -> str:
    date_str = item.date.strftime("%Y-%m-%d")
    size_str = human_size(item.size)
    ext_str = f".{item.ext}" if item.ext else ""
    channel_str = item.channel[:25]
    name_str = item.filename[:50]
    caption_str = f"  [{item.caption[:40]}]" if item.caption else ""
    return f"[{channel_str}]  {name_str}{caption_str}  ({size_str}, {ext_str}, {date_str})"
