from InquirerPy.base.control import Choice
from InquirerPy.prompts.checkbox import CheckboxPrompt
from rich.console import Console

from utils import human_size

console = Console()

KEYS_HINT = "[dim]Space[/dim]=toggle  [dim]a[/dim]=select all  [dim]↑↓[/dim]=navigate  [dim]Enter[/dim]=confirm\n"

_KEYBINDINGS = {
    "toggle-all-true": [{"key": "a"}],
}


async def select_discard(downloaded: list[dict]) -> list[dict]:
    """Show downloaded files and let the user select which ones to DELETE from disk.

    Returns the list of items the user chose to delete. Unselected items are kept.
    """
    if not downloaded:
        console.print("[yellow]No downloaded files.[/yellow]")
        return []

    total = len(downloaded)
    console.print(f"[bold]Select files to DELETE from disk[/bold]  ({total} on disk)")
    console.print("[red]Selected files will be permanently removed.[/red]")
    console.print(KEYS_HINT)

    to_delete = await CheckboxPrompt(
        message="Delete:",
        choices=[Choice(value=item, name=_format_choice(item, i + 1, total)) for i, item in enumerate(downloaded)],
        cycle=True,
        transformer=lambda r: f"{len(r)} file(s) to delete",
        keybindings=_KEYBINDINGS,
    ).execute_async()

    return to_delete


def _format_choice(item: dict, idx: int, total: int) -> str:
    date_str = (item.get("downloaded_at") or item.get("date") or "")[:10]
    size_str = human_size(item.get("size") or 0)
    ext = item.get("ext") or ""
    ext_str = f".{ext}" if ext else ""
    channel = (item.get("channel_title") or "")[:25]
    filename = (item.get("filename") or "")[:50]
    caption = item.get("caption") or ""
    caption_str = f"  [{caption[:40]}]" if caption else ""
    return f"[{idx}/{total}] [{channel}]  {filename}{caption_str}  ({size_str}{ext_str}, {date_str})"
