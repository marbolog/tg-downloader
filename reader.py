"""Terminal browser/reader for downloaded files -- `tgdctl browse`.

Textual application with two screens: LibraryScreen (a filterable table of
downloaded files) and ReaderScreen (opened per file).
"""
import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input

from db import Database
from reader_document import build_document, Document
from utils import dedupe_by_hash, human_size


class TextPromptScreen(ModalScreen[str]):
    """A single-line text input modal. Dismisses with the typed value on
    Enter, or None on Escape. Shared by LibraryScreen's text filter and
    ReaderScreen's in-document search (Task 7) -- one prompt implementation,
    two callers."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, placeholder: str, initial: str = "") -> None:
        super().__init__()
        self._placeholder = placeholder
        self._initial = initial

    def compose(self) -> ComposeResult:
        yield Container(
            Input(placeholder=self._placeholder, value=self._initial, id="prompt-input"),
            id="prompt-dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReaderScreen(Screen):
    """Shows one opened document. TOC sidebar and scrolling content pane are
    added in Task 6; in-document search in Task 7."""

    def __init__(self, filename: str, document: Document) -> None:
        super().__init__()
        self.filename = filename
        self.document = document

    def compose(self) -> ComposeResult:
        yield Footer()


class LibraryScreen(Screen):
    BINDINGS = [
        ("c", "cycle_channel", "Channel"),
        ("l", "cycle_language", "Language"),
        ("/", "filter_text", "Filter"),
    ]

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self._all_rows: list[dict] = []
        self._rows_by_id: dict[int, dict] = {}
        self._channels: list[str] = ["All"]
        self._languages: list[str] = ["All"]
        self._channel_idx = 0
        self._language_idx = 0
        self._text_filter = ""

    def compose(self) -> ComposeResult:
        yield DataTable(id="library-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one(DataTable)
        self.table.add_columns("Channel", "Filename", "Size", "Ext", "Language", "Date")
        self._load_rows()

    def _load_rows(self) -> None:
        rows = dedupe_by_hash(self.db.list_downloaded_files())
        self._all_rows = rows
        self._rows_by_id = {r["id"]: r for r in rows}
        self._channels = ["All"] + sorted({r["channel_identifier"] for r in rows})
        self._languages = ["All"] + sorted({r.get("language") or "__unknown__" for r in rows})
        self._channel_idx = 0
        self._language_idx = 0
        self._apply_filters()

    def _apply_filters(self) -> None:
        channel = self._channels[self._channel_idx]
        language = self._languages[self._language_idx]
        text = self._text_filter.lower()

        def matches(r: dict) -> bool:
            if channel != "All" and r["channel_identifier"] != channel:
                return False
            if language != "All" and (r.get("language") or "__unknown__") != language:
                return False
            if text and text not in r["filename"].lower() and text not in (r["channel_title"] or "").lower():
                return False
            return True

        visible = [r for r in self._all_rows if matches(r)]
        self.table.clear()
        for r in visible:
            self.table.add_row(
                r["channel_title"],
                r["filename"],
                human_size(r["size"]),
                r["ext"],
                r.get("language") or "-",
                (r.get("downloaded_at") or "")[:10],
                key=str(r["id"]),
            )

    def action_cycle_channel(self) -> None:
        self._channel_idx = (self._channel_idx + 1) % len(self._channels)
        self._apply_filters()

    def action_cycle_language(self) -> None:
        self._language_idx = (self._language_idx + 1) % len(self._languages)
        self._apply_filters()

    def action_filter_text(self) -> None:
        self.app.push_screen(TextPromptScreen("Filter by filename/channel..."), self._handle_text_filter)

    def _handle_text_filter(self, value: str | None) -> None:
        if value is None:
            return
        self._text_filter = value
        self._apply_filters()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        media_id = int(event.row_key.value)
        item = self._rows_by_id.get(media_id)
        if item is None:
            return

        ext = (item.get("ext") or "").lower()
        if ext not in ("pdf", "epub"):
            self.notify(
                f"{ext or 'this format'} isn't readable in the terminal -- use the web UI to download it.",
                severity="warning",
            )
            return

        local_path = item.get("local_path")
        if not local_path or not Path(local_path).exists():
            self.notify("File is missing from disk.", severity="error")
            return

        document = await asyncio.to_thread(build_document, Path(local_path), ext)
        if document is None:
            self.notify("No extractable text -- this looks like a scanned/image file.", severity="warning")
            return

        self.app.push_screen(ReaderScreen(item["filename"], document))


class BrowseApp(App):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.db))
