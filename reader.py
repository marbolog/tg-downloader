"""Terminal browser/reader for downloaded files -- `tgdctl browse`.

Textual application with two screens: LibraryScreen (a filterable table of
downloaded files) and ReaderScreen (opened per file).
"""
import asyncio
import sqlite3
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, Static

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
                *[
                    ListItem(Label(entry.title, markup=False))
                    for entry in self.document.toc
                ],
                id="toc-sidebar",
            ),
            VerticalScroll(
                *[
                    Static(section.text, id=f"section-{i}", markup=False)
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
    """Ranger/mc-inspired file table: j/k/g/G to move, space to mark a row,
    v to mark a contiguous range, d to delete whatever is marked (or just the
    cursor row if nothing is), y/n/Esc to resolve the delete prompt shown in
    the status bar, q to quit. No modal dialogs anywhere in this screen --
    the status bar is the only "popup" and it never takes focus away from
    the table.
    """

    MARK = "●"  # ●

    BINDINGS = [
        ("c", "cycle_channel", "Channel"),
        ("l", "cycle_language", "Language"),
        ("/", "filter_text", "Filter"),
        ("j", "move_down", "Down"),
        ("k", "move_up", "Up"),
        ("g", "jump_top", "Top"),
        ("G", "jump_bottom", "Bottom"),
        ("space", "toggle_mark", "Mark"),
        ("v", "toggle_visual", "Visual"),
        ("d", "delete_selected", "Delete"),
        Binding("y", "confirm_delete", "Confirm", show=False),
        Binding("n", "cancel_delete", "Cancel", show=False),
        Binding("escape", "cancel_active", "Cancel", show=False),
        ("q", "quit_smart", "Quit"),
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
        self._visible_ids: list[int] = []
        self._marked: set[int] = set()
        self._visual_anchor_row: int | None = None
        self._pending_delete_ids: list[int] | None = None

    def compose(self) -> ComposeResult:
        yield DataTable(id="library-table", cursor_type="row")
        yield Static("", id="status-bar", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one(DataTable)
        column_keys = self.table.add_columns(
            "", "Channel", "Filename", "Size", "Ext", "Language", "Date"
        )
        self._mark_column = column_keys[0]
        self.status_bar = self.query_one("#status-bar", Static)
        self.status_bar.display = False
        self._load_rows()

    def _load_rows(self) -> None:
        rows = dedupe_by_hash(self.db.list_downloaded_files())
        self._all_rows = rows
        self._rows_by_id = {r["id"]: r for r in rows}
        self._channels = ["All"] + sorted({r["channel_identifier"] for r in rows})
        self._languages = ["All"] + sorted({r.get("language") or "__unknown__" for r in rows})
        self._channel_idx = 0
        self._language_idx = 0
        self._marked.clear()
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
        self._visible_ids = [r["id"] for r in visible]
        self.table.clear()
        for r in visible:
            self.table.add_row(
                self.MARK if r["id"] in self._marked else "",
                r["channel_title"],
                r["filename"],
                human_size(r["size"]),
                r["ext"],
                r.get("language") or "-",
                (r.get("downloaded_at") or "")[:10],
                key=str(r["id"]),
            )

    def action_cycle_channel(self) -> None:
        if self._interaction_locked():
            return
        self._channel_idx = (self._channel_idx + 1) % len(self._channels)
        self._apply_filters()

    def action_cycle_language(self) -> None:
        if self._interaction_locked():
            return
        self._language_idx = (self._language_idx + 1) % len(self._languages)
        self._apply_filters()

    def action_filter_text(self) -> None:
        if self._interaction_locked():
            return
        self.app.push_screen(TextPromptScreen("Filter by filename/channel..."), self._handle_text_filter)

    def _handle_text_filter(self, value: str | None) -> None:
        if value is None:
            return
        self._text_filter = value
        self._apply_filters()

    def action_move_down(self) -> None:
        self.table.action_cursor_down()
        self._refresh_visual_status()

    def action_move_up(self) -> None:
        self.table.action_cursor_up()
        self._refresh_visual_status()

    def action_jump_top(self) -> None:
        if self.table.row_count:
            self.table.move_cursor(row=0)
        self._refresh_visual_status()

    def action_jump_bottom(self) -> None:
        if self.table.row_count:
            self.table.move_cursor(row=self.table.row_count - 1)
        self._refresh_visual_status()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._interaction_locked():
            return
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

    def _interaction_locked(self) -> bool:
        """True while a delete prompt or an unfinished visual selection owns
        the keyboard -- everything except its own resolution keys is a no-op
        so a stray keypress can't half-apply either one."""
        return self._pending_delete_ids is not None or self._visual_anchor_row is not None

    def _set_mark(self, media_id: int, marked: bool) -> None:
        if marked:
            self._marked.add(media_id)
        else:
            self._marked.discard(media_id)
        if media_id in self._visible_ids:
            self.table.update_cell(
                str(media_id), self._mark_column, self.MARK if marked else ""
            )

    def action_toggle_mark(self) -> None:
        if self._visual_anchor_row is not None:
            self._commit_visual_range()
            return
        if self._pending_delete_ids is not None:
            return
        item = self._current_item()
        if item is None:
            return
        media_id = item["id"]
        self._set_mark(media_id, media_id not in self._marked)

    def action_toggle_visual(self) -> None:
        if self._pending_delete_ids is not None:
            return
        if self._visual_anchor_row is not None:
            self._visual_anchor_row = None
            self._refresh_visual_status()
            return
        if self.table.row_count == 0:
            return
        self._visual_anchor_row = self.table.cursor_coordinate.row
        self._refresh_visual_status()

    def _commit_visual_range(self) -> None:
        lo, hi = sorted((self._visual_anchor_row, self.table.cursor_coordinate.row))
        for row_index in range(lo, hi + 1):
            if row_index < len(self._visible_ids):
                self._set_mark(self._visible_ids[row_index], True)
        self._visual_anchor_row = None
        self._refresh_visual_status()

    def _refresh_visual_status(self) -> None:
        if self._pending_delete_ids is not None:
            return  # delete prompt owns the status bar
        if self._visual_anchor_row is None:
            self.status_bar.display = False
            return
        lo, hi = sorted((self._visual_anchor_row, self.table.cursor_coordinate.row))
        count = hi - lo + 1
        self.status_bar.update(f"-- VISUAL -- {count} row(s) (space: mark, esc: cancel)")
        self.status_bar.display = True

    def action_delete_selected(self) -> None:
        if self._visual_anchor_row is not None or self._pending_delete_ids is not None:
            return
        if self._marked:
            ids = sorted(self._marked)
        else:
            item = self._current_item()
            if item is None:
                return
            ids = [item["id"]]

        self._pending_delete_ids = ids
        if len(ids) == 1:
            item = self._rows_by_id.get(ids[0])
            filename = item["filename"] if item else "file"
            self.status_bar.update(f"Delete {filename!r}? [y/N]")
        else:
            self.status_bar.update(f"Delete {len(ids)} files? [y/N]")
        self.status_bar.display = True

    def action_confirm_delete(self) -> None:
        if self._pending_delete_ids is None:
            return
        ids = self._pending_delete_ids
        self._pending_delete_ids = None
        self.status_bar.display = False
        self._perform_delete(ids)

    def action_cancel_delete(self) -> None:
        if self._pending_delete_ids is None:
            return
        self._pending_delete_ids = None
        self.status_bar.display = False

    def action_cancel_active(self) -> None:
        if self._pending_delete_ids is not None:
            self.action_cancel_delete()
        elif self._visual_anchor_row is not None:
            self._visual_anchor_row = None
            self._refresh_visual_status()

    def action_quit_smart(self) -> None:
        if self._pending_delete_ids is not None:
            self.action_cancel_delete()
            return
        if self._visual_anchor_row is not None:
            self._visual_anchor_row = None
            self._refresh_visual_status()
            return
        self.app.exit()

    def _perform_delete(self, ids: list[int]) -> None:
        rows = {media_id: self.db.get_media(media_id) for media_id in ids}
        valid_ids = [i for i in ids if rows[i] is not None and rows[i]["status"] == "downloaded"]
        stale_ids = [i for i in ids if i not in valid_ids]

        if stale_ids:
            self.notify(f"{len(stale_ids)} file(s) already removed.", severity="warning")

        removed_ids = set(stale_ids)

        if valid_ids:
            try:
                self.db.mark_discarded_many(valid_ids)
            except sqlite3.OperationalError:
                self.notify("Database busy -- try again.", severity="error")
                self._drop_rows(removed_ids)
                return
            for media_id in valid_ids:
                local_path = rows[media_id]["local_path"]
                if local_path:
                    p = Path(local_path)
                    if p.exists():
                        p.unlink()
            removed_ids |= set(valid_ids)
            self.notify(f"Deleted {len(valid_ids)} file(s).")

        self._drop_rows(removed_ids)

    def _drop_rows(self, media_ids: set[int]) -> None:
        self._marked -= media_ids
        self._all_rows = [r for r in self._all_rows if r["id"] not in media_ids]
        for media_id in media_ids:
            self._rows_by_id.pop(media_id, None)
        self._apply_filters()


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
    #status-bar {
        background: $accent;
        color: $text;
        padding: 0 1;
        height: 1;
        dock: bottom;
    }
    """

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.db))
