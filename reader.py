"""Terminal browser/reader for downloaded files -- `tgdctl browse`.

Textual application with two screens: LibraryScreen (a filterable table of
downloaded files) and ReaderScreen (opened per file).
"""
import asyncio
import sqlite3
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, ListItem, ListView, Static

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


class ConfirmDeleteScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, filename: str) -> None:
        super().__init__()
        self._filename = filename

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"Delete {self._filename!r}?"),
            Horizontal(
                Button("Delete", id="confirm-yes", variant="error"),
                Button("Cancel", id="confirm-no", variant="primary"),
            ),
            id="confirm-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "enter":
            self.dismiss(True)


class ReaderScreen(Screen):
    BINDINGS = [
        ("escape", "close", "Back"),
        ("q", "close", "Back"),
        ("/", "search", "Search"),
        ("n", "next_match", "Next match"),
        ("N", "prev_match", "Prev match"),
    ]

    def __init__(self, filename: str, document: Document) -> None:
        super().__init__()
        self.filename = filename
        self.document = document
        self._matches: list[int] = []
        self._match_pos: int = -1

    def compose(self) -> ComposeResult:
        yield Horizontal(
            ListView(
                *[ListItem(Label(entry.title)) for entry in self.document.toc],
                id="toc-sidebar",
            ),
            VerticalScroll(
                *[
                    Static(section.text, id=f"section-{i}")
                    for i, section in enumerate(self.document.sections)
                ],
                id="reader-content",
            ),
        )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = self.query_one(ListView).index
        if index is None or index >= len(self.document.toc):
            return
        section_index = self.document.toc[index].section_index
        self.query_one(f"#section-{section_index}", Static).scroll_visible(top=True)

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_search(self) -> None:
        self.app.push_screen(TextPromptScreen("Search in document..."), self._handle_search_query)

    def _handle_search_query(self, query: str | None) -> None:
        if not query:
            return
        needle = query.lower()
        self._matches = [
            i for i, s in enumerate(self.document.sections) if needle in s.text.lower()
        ]
        self._match_pos = -1
        if not self._matches:
            self.notify(f"No matches for {query!r}.", severity="warning")
            return
        self.action_next_match()

    def action_next_match(self) -> None:
        if not self._matches:
            return
        self._match_pos = (self._match_pos + 1) % len(self._matches)
        self._scroll_to_match()

    def action_prev_match(self) -> None:
        if not self._matches:
            return
        self._match_pos = (self._match_pos - 1) % len(self._matches)
        self._scroll_to_match()

    def _scroll_to_match(self) -> None:
        section_index = self._matches[self._match_pos]
        self.query_one(f"#section-{section_index}", Static).scroll_visible(top=True)


class LibraryScreen(Screen):
    BINDINGS = [
        ("c", "cycle_channel", "Channel"),
        ("l", "cycle_language", "Language"),
        ("/", "filter_text", "Filter"),
        ("d", "delete_selected", "Delete"),
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

    def _current_item(self) -> dict | None:
        if self.table.row_count == 0:
            return None
        row_key, _ = self.table.coordinate_to_cell_key(self.table.cursor_coordinate)
        media_id = int(row_key.value)
        return self._rows_by_id.get(media_id)

    def action_delete_selected(self) -> None:
        item = self._current_item()
        if item is None:
            return

        def handle_result(confirmed: bool | None) -> None:
            if confirmed:
                self._delete_item(item)

        self.app.push_screen(ConfirmDeleteScreen(item["filename"]), handle_result)

    def _delete_item(self, item: dict) -> None:
        media_id = item["id"]
        fresh = self.db.get_media(media_id)
        if fresh is None or fresh["status"] != "downloaded":
            self.notify("File was already removed.", severity="warning")
            self._all_rows = [r for r in self._all_rows if r["id"] != media_id]
            self._rows_by_id.pop(media_id, None)
            self._apply_filters()
            return

        local_path = fresh["local_path"]
        try:
            self.db.mark_discarded_many([media_id])
        except sqlite3.OperationalError:
            self.notify("Database busy -- try again.", severity="error")
            return

        if local_path:
            p = Path(local_path)
            if p.exists():
                p.unlink()

        self._all_rows = [r for r in self._all_rows if r["id"] != media_id]
        self._rows_by_id.pop(media_id, None)
        self._apply_filters()
        self.notify(f"Deleted {item['filename']!r}.")


class BrowseApp(App):
    CSS = """
    #toc-sidebar {
        width: 30;
        border-right: solid $accent;
    }
    #reader-content {
        padding: 1 2;
    }
    #prompt-dialog {
        align: center middle;
        padding: 1 2;
        border: thick $accent;
        width: 60;
        height: 5;
    }
    """

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.db))
