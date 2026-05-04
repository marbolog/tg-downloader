from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from InquirerPy.prompts.checkbox import CheckboxPrompt
from rich.console import Console

from utils import human_size

console = Console()

KEYS_HINT = "[dim]Space[/dim]=toggle  [dim]a[/dim]=select all  [dim]↑↓[/dim]=navigate  [dim]Enter[/dim]=confirm\n"

_KEYBINDINGS = {
    "toggle-all-true": [{"key": "a"}],
}


def _checkbox(message: str, choices: list, **kwargs) -> CheckboxPrompt:
    """CheckboxPrompt that appends a live n/total position indicator."""
    total = len(choices)

    class _PC(CheckboxPrompt):
        def _get_prompt_message(self) -> list:
            msg = super()._get_prompt_message()
            if not self.status["answered"]:
                try:
                    idx = self.content_control.selected_choice_index
                    msg.append(("class:instruction", f" [{idx + 1}/{total}]"))
                except Exception:
                    pass
            return msg

    return _PC(message=message, choices=choices, **kwargs)


async def select_download_and_skip(pending: list[dict]) -> tuple[list[dict], list[dict]]:
    """Two-pass selection: first pick items to download, then pick items to skip.

    Returns (to_download, to_skip). Items in neither list stay pending.
    """
    if not pending:
        console.print("[yellow]No pending media.[/yellow]")
        return [], []

    # Pass 1: download
    console.print(f"[bold]Step 1/2 — select items to download[/bold]  ({len(pending)} pending)")
    console.print(KEYS_HINT)
    to_download = await _checkbox(
        message="Download:",
        choices=[Choice(value=item, name=_format_choice(item)) for item in pending],
        cycle=True,
        transformer=lambda r: f"{len(r)} file(s)",
        keybindings=_KEYBINDINGS,
    ).execute_async()

    # Pass 2: skip (only items not selected for download)
    download_ids = {item["id"] for item in to_download}
    remaining = [item for item in pending if item["id"] not in download_ids]

    to_skip = []
    if remaining:
        console.print(f"\n[bold]Step 2/2 — select items to skip[/bold]  ({len(remaining)} remaining)")
        console.print(KEYS_HINT)
        to_skip = await _checkbox(
            message="Skip (permanently dismiss):",
            choices=[Choice(value=item, name=_format_choice(item)) for item in remaining],
            cycle=True,
            transformer=lambda r: f"{len(r)} file(s)",
            keybindings=_KEYBINDINGS,
        ).execute_async()

    return to_download, to_skip


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
