"""Terminal browser/reader for downloaded files -- `tgdctl browse`.

Textual application with two screens: LibraryScreen (a filterable table of
downloaded files) and ReaderScreen (opened per file).
"""
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer

from db import Database
from utils import dedupe_by_hash, human_size


class LibraryScreen(Screen):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self._rows_by_id: dict[int, dict] = {}

    def compose(self) -> ComposeResult:
        yield DataTable(id="library-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one(DataTable)
        self.table.add_columns("Channel", "Filename", "Size", "Ext", "Language", "Date")
        self._load_rows()

    def _load_rows(self) -> None:
        rows = dedupe_by_hash(self.db.list_downloaded_files())
        self._rows_by_id = {r["id"]: r for r in rows}
        self.table.clear()
        for r in rows:
            self.table.add_row(
                r["channel_title"],
                r["filename"],
                human_size(r["size"]),
                r["ext"],
                r.get("language") or "-",
                (r.get("downloaded_at") or "")[:10],
                key=str(r["id"]),
            )


class BrowseApp(App):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.db))
