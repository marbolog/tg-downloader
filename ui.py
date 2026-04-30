from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console

from utils import human_size

console = Console()


def select_pending_media(pending: list[dict], action: str = "download") -> list[dict]:
    if not pending:
        console.print("[yellow]No pending media.[/yellow]")
        return []

    choices = [Choice(value=item, name=_format_choice(item)) for item in pending]

    console.print(
        "[dim]Space[/dim]=toggle  [dim]A[/dim]=select all  "
        "[dim]↑↓[/dim]=navigate  [dim]Enter[/dim]=confirm\n"
    )

    return inquirer.checkbox(
        message=f"Select media to {action} ({len(pending)} pending):",
        choices=choices,
        cycle=True,
        transformer=lambda result: f"{len(result)} file(s) selected",
    ).execute()


def _format_choice(item: dict) -> str:
    date_str = (item.get("date") or "")[:10]
    size_str = human_size(item.get("size") or 0)
    ext = item.get("ext") or ""
    ext_str = f".{ext}" if ext else ""
    channel = (item.get("channel_title") or "")[:25]
    filename = (item.get("filename") or "")[:50]
    caption = item.get("caption") or ""
    caption_str = f"  [{caption[:40]}]" if caption else ""
    return f"[{channel}]  {filename}{caption_str}  ({size_str}{ext_str}, {date_str})"
