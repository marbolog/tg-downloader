# CLI Browse/Delete/Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `tgdctl browse` command that lets the user list downloaded files, delete them, and read PDF/EPUB content — all from the terminal.

**Architecture:** One new Textual TUI application (`reader.py`, two screens: `LibraryScreen` and `ReaderScreen`) proxied into the running container exactly like the existing `discard` command (`docker compose exec -it`). PDF/EPUB text extraction reuses `search/chunker.py`'s existing `chunk_file()` unchanged; a new thin adapter (`reader_document.py`) turns its output into reader-ready sections plus a table of contents (PDF bookmarks via PyMuPDF's `get_toc()`, or EPUB chapter titles — falling back to a flat page-number list when neither exists). Deletion reuses `Database.mark_discarded_many` with the same DB-write-before-disk-unlink ordering already fixed in `webui/app.py`.

**Tech Stack:** Python 3.11, Textual (new dependency), PyMuPDF (`fitz`, already a dependency), `search/chunker.py` (existing), SQLite via the existing `Database` class, `pytest` + `pytest-asyncio` (new dev dependency) for tests.

**Spec:** `docs/superpowers/specs/2026-09-18-cli-browse-reader-design.md`

## Global Constraints

- `discard`, `ui.py`, `webui/`, and `search/chunker.py` are not modified in behavior — only `webui/app.py` gets a small import change in Task 1 (logic moves, does not change).
- Reading is PDF/EPUB only for this pass. Any other extension shows a clear "not readable in terminal" message — never a crash or a silent no-op.
- No terminal image/graphics rendering (sixel, kitty protocol, chafa) — text only, per the spec.
- New capability is proxied through `tgdctl` into the container via `docker compose exec -it`, matching `discard`'s existing invocation model — no standalone host-native tool.
- `textual` and `pytest-asyncio` are added with a `>=` floor and no upper bound, matching every other dependency in `pyproject.toml`.
- Every new DB-touching code path re-reads the row immediately before acting on it when the action is destructive (delete) — never trust a snapshot taken earlier in the session, since the listener/webui write to the same DB concurrently.

---

## Task 1: Extract shared dedupe-by-hash helper into `utils.py`

`webui/app.py`'s file listing already deduplicates downloaded files by SHA-256 hash (falling back to filename+size) via a private `_deduplicate_with_counts` function. `browse`'s `LibraryScreen` needs the identical behavior so the CLI and the web UI agree on "how many distinct files are there." Moving it to `utils.py` (the project's existing shared-helpers module) lets both consume one implementation instead of duplicating the logic.

**Files:**
- Modify: `utils.py`
- Modify: `webui/app.py:84-107` (the `_deduplicate_with_counts` function and its one call site at `webui/app.py:74`)
- Modify: `webui/Dockerfile` (add `utils.py` to the image — it isn't copied today)
- Test: `tests/test_utils.py`

**Interfaces:**
- Produces: `utils.dedupe_by_hash(items: list[dict]) -> list[dict]` — takes a list of file dicts (each with at least `id`, `filename`, `size`, and optionally `file_hash`), returns the subset representing one row per unique file (lowest `id` wins), with a `copy_count` key added to each returned dict.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_utils.py` (check the file's existing imports first — add `from utils import dedupe_by_hash` alongside what's already imported):

```python
def test_dedupe_by_hash_keeps_lowest_id_per_hash():
    items = [
        {"id": 3, "filename": "a.pdf", "size": 100, "file_hash": "abc"},
        {"id": 1, "filename": "a_copy.pdf", "size": 100, "file_hash": "abc"},
        {"id": 2, "filename": "b.pdf", "size": 200, "file_hash": "def"},
    ]
    result = dedupe_by_hash(items)
    result_ids = {r["id"] for r in result}
    assert result_ids == {1, 2}
    kept = next(r for r in result if r["id"] == 1)
    assert kept["copy_count"] == 2


def test_dedupe_by_hash_falls_back_to_filename_and_size_when_no_hash():
    items = [
        {"id": 5, "filename": "same.pdf", "size": 100, "file_hash": None},
        {"id": 4, "filename": "same.pdf", "size": 100, "file_hash": None},
        {"id": 6, "filename": "different.pdf", "size": 100, "file_hash": None},
    ]
    result = dedupe_by_hash(items)
    result_ids = {r["id"] for r in result}
    assert result_ids == {4, 6}


def test_dedupe_by_hash_single_copy_has_count_one():
    items = [{"id": 1, "filename": "solo.pdf", "size": 50, "file_hash": "xyz"}]
    result = dedupe_by_hash(items)
    assert result[0]["copy_count"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_utils.py -v -k dedupe`
Expected: FAIL with `ImportError: cannot import name 'dedupe_by_hash' from 'utils'`

- [ ] **Step 3: Add `dedupe_by_hash` to `utils.py`**

Add this function to `utils.py` (after `unique_path`, at the end of the file):

```python
def dedupe_by_hash(items: list[dict]) -> list[dict]:
    """Keep one row per unique file (by SHA-256 hash, or filename+size when no
    hash is recorded), annotated with copy_count.

    Items are expected sorted newest-first (the DB's default ordering); the
    kept representative is the lowest id (the oldest download of that file).
    """
    groups: dict[str, list[int]] = {}
    id_to_key: dict[int, str] = {}
    for item in items:
        key = item.get("file_hash") or f"\x00{item['filename']}\x00{item['size']}"
        groups.setdefault(key, []).append(item["id"])
        id_to_key[item["id"]] = key

    rep_ids = {min(ids) for ids in groups.values()}
    key_counts = {k: len(v) for k, v in groups.items()}

    result = []
    for item in items:
        if item["id"] in rep_ids:
            item["copy_count"] = key_counts[id_to_key[item["id"]]]
            result.append(item)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_utils.py -v -k dedupe`
Expected: PASS (3 tests)

- [ ] **Step 5: Point `webui/app.py` at the shared helper**

In `webui/app.py`, add to the imports at the top:

```python
from utils import dedupe_by_hash
```

Replace the call site at `webui/app.py:74`:

```python
    if hide_dupes:
        all_items = _deduplicate_with_counts(all_items)
```

with:

```python
    if hide_dupes:
        all_items = dedupe_by_hash(all_items)
```

Delete the now-unused `_deduplicate_with_counts` function (`webui/app.py:84-107`).

- [ ] **Step 6: Add `utils.py` to the webui Docker image**

In `webui/Dockerfile`, add a line after `COPY db.py ./`:

```dockerfile
COPY db.py ./
COPY utils.py ./
COPY webui/app.py ./
```

- [ ] **Step 7: Run the full webui test suite to confirm nothing broke**

Run: `uv run pytest tests/test_webui_discard.py tests/test_webui_pdf.py tests/test_webui_auth.py -v`
Expected: PASS (all existing tests still pass — this task only moved code, behavior is unchanged)

- [ ] **Step 8: Commit**

```bash
git add utils.py webui/app.py webui/Dockerfile tests/test_utils.py
git commit -m "refactor: extract dedupe-by-hash helper to utils.py for reuse by CLI browse"
```

---

## Task 2: `reader_document.py` — text extraction adapter for the reader

Turns `search/chunker.py`'s existing per-page (PDF) / per-chapter (EPUB) chunks into a reader-ready `Document`: a flat list of `Section`s to display, plus a `TocEntry` list for the sidebar. For PDF, real bookmarks (`fitz`'s `get_toc()`) map to the nearest available page; when a PDF has no bookmarks (or the EPUB path, which always has chapter titles), the TOC is synthesized 1:1 from the sections themselves — so the TOC is never empty for a document that parsed successfully, and `ReaderScreen` (Task 6) never needs two code paths for "has real bookmarks" vs. "doesn't."

**Files:**
- Create: `reader_document.py`
- Test: `tests/test_reader_document.py`

**Interfaces:**
- Consumes: `search.chunker.chunk_file(path: Path, ext: str) -> list[dict]` (existing — each dict has `chunk_idx: int`, `page: int | None`, `chapter: str | None`, `text: str`).
- Produces:
  - `Section` (dataclass): `title: str`, `text: str`
  - `TocEntry` (dataclass): `title: str`, `section_index: int`
  - `Document` (dataclass): `sections: list[Section]`, `toc: list[TocEntry]`
  - `build_document(path: Path, ext: str) -> Document | None` — `None` means "no extractable text" (image-only PDF, unparseable file, or an extension other than pdf/epub). Callers are expected to check `path.exists()` themselves before calling this — it assumes the path is valid.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reader_document.py`:

```python
import io
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from reader_document import build_document, Section, TocEntry, Document


def _mock_pdf_chunks():
    return [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "Page one text."},
        {"chunk_idx": 1, "page": 2, "chapter": None, "text": "Page two text."},
        {"chunk_idx": 2, "page": 3, "chapter": None, "text": "Page three text."},
    ]


def test_pdf_without_outline_falls_back_to_page_titles():
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = []
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [s.title for s in doc.sections] == ["Page 1", "Page 2", "Page 3"]
    assert doc.sections[0].text == "Page one text."
    # No real bookmarks -> TOC is synthesized 1:1 from the sections.
    assert [e.title for e in doc.toc] == ["Page 1", "Page 2", "Page 3"]
    assert [e.section_index for e in doc.toc] == [0, 1, 2]


def test_pdf_with_outline_maps_bookmarks_to_sections():
    mock_doc = MagicMock()
    # (level, title, page) -- PyMuPDF's get_toc() format, 1-based pages.
    mock_doc.get_toc.return_value = [
        [1, "Introduction", 1],
        [1, "Chapter One", 2],
    ]
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.title for e in doc.toc] == ["Introduction", "Chapter One"]
    assert [e.section_index for e in doc.toc] == [0, 1]
    # Sections themselves always stay page-granular, titled by page number,
    # regardless of the bookmark titles -- the TOC is a separate index into them.
    assert [s.title for s in doc.sections] == ["Page 1", "Page 2", "Page 3"]


def test_pdf_bookmark_targeting_skipped_page_uses_nearest_later_page():
    # Bookmark points at page 2, but page 2's text was skipped during chunking
    # (e.g. a blank divider) -- only pages 1 and 3 exist as chunks.
    chunks = [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "Page one."},
        {"chunk_idx": 1, "page": 3, "chapter": None, "text": "Page three."},
    ]
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = [[1, "Chapter One", 2]]
    with patch("reader_document.chunk_file", return_value=chunks), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.section_index for e in doc.toc] == [1]  # section for page 3


def test_pdf_bookmark_targeting_page_past_the_end_falls_back_to_default_toc():
    # The one bookmark maps to nothing valid -- an empty TOC sidebar would be
    # useless, so this falls back to the same per-page TOC used when a PDF has
    # no outline at all, rather than leaving the reader with no navigation.
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = [[1, "Appendix", 99]]
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.title for e in doc.toc] == ["Page 1", "Page 2", "Page 3"]


def test_pdf_with_no_extractable_text_returns_none():
    with patch("reader_document.chunk_file", return_value=[]):
        doc = build_document(Path("scanned.pdf"), "pdf")
    assert doc is None


def test_epub_uses_chapter_titles_for_both_sections_and_toc():
    chunks = [
        {"chunk_idx": 0, "page": None, "chapter": "Chapter 1", "text": "First chapter."},
        {"chunk_idx": 1, "page": None, "chapter": None, "text": "Untitled section."},
    ]
    with patch("reader_document.chunk_file", return_value=chunks):
        doc = build_document(Path("book.epub"), "epub")

    assert doc is not None
    assert doc.sections[0].title == "Chapter 1"
    assert doc.sections[0].text == "First chapter."
    assert doc.sections[1].title == "Section 2"  # no chapter heading -> fallback title
    assert [e.title for e in doc.toc] == ["Chapter 1", "Section 2"]
    assert [e.section_index for e in doc.toc] == [0, 1]


def test_epub_with_no_extractable_text_returns_none():
    with patch("reader_document.chunk_file", return_value=[]):
        doc = build_document(Path("empty.epub"), "epub")
    assert doc is None


def test_unsupported_extension_returns_none():
    with patch("reader_document.chunk_file", return_value=[{"chunk_idx": 0, "page": None, "chapter": None, "text": "x"}]):
        doc = build_document(Path("book.mobi"), "mobi")
    assert doc is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader_document.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reader_document'`

- [ ] **Step 3: Write `reader_document.py`**

```python
"""Adapter turning search/chunker.py's flat text chunks into a reader-ready
Document: a list of displayable sections plus a table of contents.

chunk_file() gives per-page text for PDF and per-chapter text for EPUB, in
document order -- already exactly what a reader needs to display. The one
thing it doesn't carry is a real table of contents for PDF (chunks only have
page numbers, not titles): PyMuPDF can read a PDF's own embedded bookmarks
via get_toc(); when present, that's mapped onto section indices. When absent
(common for scans), or for EPUB (whose chapters already carry titles from
chunk_file itself), the TOC is synthesized 1:1 from the sections -- so
Document.toc is never empty for a document that parsed successfully, and
callers never need two code paths for "has bookmarks" vs. "doesn't."
"""
from dataclasses import dataclass
from pathlib import Path

import fitz

from search.chunker import chunk_file


@dataclass
class Section:
    title: str
    text: str


@dataclass
class TocEntry:
    title: str
    section_index: int


@dataclass
class Document:
    sections: list[Section]
    toc: list[TocEntry]


def build_document(path: Path, ext: str) -> Document | None:
    """Build a reader Document from an existing PDF/EPUB file's extracted text.

    Returns None when there is no extractable text (image-only/scanned PDF,
    unparseable file) or ext isn't pdf/epub. Callers are expected to check
    path.exists() themselves first -- this only reports whether text came out.
    """
    if ext not in ("pdf", "epub"):
        return None
    chunks = chunk_file(path, ext)
    if not chunks:
        return None

    if ext == "pdf":
        sections = [Section(title=f"Page {c['page']}", text=c["text"]) for c in chunks]
        toc = _pdf_toc(path, chunks) or _default_toc(sections)
    else:
        sections = [
            Section(title=c["chapter"] or f"Section {i + 1}", text=c["text"])
            for i, c in enumerate(chunks)
        ]
        toc = _default_toc(sections)

    return Document(sections=sections, toc=toc)


def _default_toc(sections: list[Section]) -> list[TocEntry]:
    return [TocEntry(title=s.title, section_index=i) for i, s in enumerate(sections)]


def _pdf_toc(path: Path, chunks: list[dict]) -> list[TocEntry]:
    """Map PyMuPDF's outline (real embedded bookmarks) to section indices,
    using the nearest chunked page at or after each bookmark's target page (a
    bookmark can point at a page whose text extraction was skipped, e.g. a
    blank divider). Bookmarks past the last chunked page are dropped."""
    try:
        doc = fitz.open(str(path))
        outline = doc.get_toc()
        doc.close()
    except Exception:
        return []
    if not outline:
        return []

    pages = [c["page"] for c in chunks]
    entries = []
    for _level, title, target_page in outline:
        idx = _nearest_page_index(pages, target_page)
        if idx is not None:
            entries.append(TocEntry(title=title, section_index=idx))
    return entries


def _nearest_page_index(pages: list[int], target_page: int) -> int | None:
    for i, page in enumerate(pages):
        if page >= target_page:
            return i
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader_document.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Add `reader_document` to the installed package modules**

In `pyproject.toml`, update `[tool.setuptools]`:

```toml
[tool.setuptools]
py-modules = ["main", "config", "db", "lang_filter", "listener", "ui", "downloader", "utils", "tgdctl", "reader_document"]
packages = ["search"]
```

(`reader.py` will be added to this same list in Task 3, once it exists.)

- [ ] **Step 6: Commit**

```bash
git add reader_document.py tests/test_reader_document.py pyproject.toml
git commit -m "feat: add reader_document.py -- PDF/EPUB text-to-reader adapter"
```

---

## Task 3: `textual` dependency + `reader.py` skeleton (list only)

Adds the new dependency, and the smallest useful slice of the TUI: `BrowseApp` boots and `LibraryScreen` shows every downloaded file in a table. No filtering, no row actions yet — those are Tasks 4, 5, and 8.

**Files:**
- Modify: `pyproject.toml` (add `textual` runtime dependency, `pytest-asyncio` dev dependency, `asyncio_mode` config, and `reader` to `py-modules`)
- Create: `reader.py`
- Test: `tests/test_reader.py`

**Interfaces:**
- Consumes: `db.Database.list_downloaded_files() -> list[dict]` (existing), `utils.dedupe_by_hash(items: list[dict]) -> list[dict]` (Task 1).
- Produces: `reader.BrowseApp(db: Database)` — a Textual `App` subclass; `reader.LibraryScreen(db: Database)` — a Textual `Screen` subclass with `id="library-table"` on its `DataTable`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to `dependencies`:

```toml
    "textual>=0.60",
```

Add to `[dependency-groups]`:

```toml
[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24"]
```

Add a new section so async tests don't need a per-test decorator:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Update `[tool.setuptools]` to include `reader`:

```toml
[tool.setuptools]
py-modules = ["main", "config", "db", "lang_filter", "listener", "ui", "downloader", "utils", "tgdctl", "reader_document", "reader"]
packages = ["search"]
```

- [ ] **Step 2: Sync dependencies**

Run: `uv sync`
Expected: `textual` and `pytest-asyncio` install without error; run `uv run python -c "import textual; print(textual.__version__)"` to confirm.

- [ ] **Step 3: Write the failing test**

Create `tests/test_reader.py`:

```python
import pytest
from db import Database
from reader import BrowseApp


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _channel(db, telegram_id=1, identifier="@ch", title="Chan") -> int:
    db.add_channel(telegram_id, identifier, title)
    with db._conn() as conn:
        return conn.execute(
            "SELECT id FROM channels WHERE telegram_id=?", (telegram_id,)
        ).fetchone()["id"]


def _downloaded(db, channel_id, msg_id, filename, ext="pdf", local_path=None, language=None) -> int:
    mid = db.save_media_message(
        channel_id=channel_id, message_id=msg_id, filename=filename, size=100,
        mime_type="application/octet-stream", ext=ext,
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, local_path or f"/tmp/{filename}", language=language)
    return mid


async def test_library_screen_lists_downloaded_files(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "book.pdf")
    _downloaded(db, ch, 2, "novel.epub")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 2


async def test_library_screen_is_empty_when_no_downloads(db):
    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 0


async def test_library_screen_dedupes_by_hash(db):
    ch = _channel(db)
    id1 = _downloaded(db, ch, 1, "book.pdf")
    id2 = _downloaded(db, ch, 2, "book_copy.pdf")
    with db._conn() as conn:
        conn.execute("UPDATE media_messages SET file_hash='samehash' WHERE id IN (?, ?)", (id1, id2))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 1
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reader'`

- [ ] **Step 5: Write `reader.py` (skeleton)**

```python
"""Terminal browser/reader for downloaded files -- `tgdctl browse`.

Textual application with two screens: LibraryScreen (a filterable table of
downloaded files -- see Task 4 for filtering) and ReaderScreen (opened per
file -- see Tasks 5-7). Delete is added in Task 8.
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock reader.py tests/test_reader.py
git commit -m "feat: add reader.py skeleton -- BrowseApp lists downloaded files"
```

---

## Task 4: `LibraryScreen` filtering (channel, language, text)

Adds `c` (cycle channel), `l` (cycle language), and `/` (text filter by filename/channel) to `LibraryScreen`, matching the spec's approved interaction model.

**Files:**
- Modify: `reader.py`
- Test: `tests/test_reader.py`

**Interfaces:**
- Produces: `reader.TextPromptScreen(placeholder: str, initial: str = "")` — a reusable `ModalScreen[str]` (also used by `ReaderScreen`'s search in Task 7 — built now so Task 7 doesn't duplicate it).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reader.py`:

```python
async def test_cycle_channel_filters_table(db):
    ch1 = _channel(db, telegram_id=1, identifier="@ch1", title="Channel One")
    ch2 = _channel(db, telegram_id=2, identifier="@ch2", title="Channel Two")
    _downloaded(db, ch1, 1, "a.pdf")
    _downloaded(db, ch2, 2, "b.pdf")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 2
        await pilot.press("c")  # All -> @ch1 (channels sorted alphabetically)
        assert table.row_count == 1
        await pilot.press("c")  # @ch1 -> @ch2
        assert table.row_count == 1
        await pilot.press("c")  # @ch2 -> back to All
        assert table.row_count == 2


async def test_cycle_language_filters_table(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "en.pdf", language="en")
    _downloaded(db, ch, 2, "unknown.pdf", language=None)

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 2
        await pilot.press("l")  # All -> __unknown__ (sorts before "en")
        assert table.row_count == 1
        await pilot.press("l")  # __unknown__ -> en
        assert table.row_count == 1
        await pilot.press("l")  # en -> back to All
        assert table.row_count == 2


async def test_text_filter_matches_filename(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "economist.pdf")
    _downloaded(db, ch, 2, "novel.epub")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        await pilot.press("slash")
        await pilot.press(*"econ")
        await pilot.press("enter")
        assert table.row_count == 1


async def test_text_filter_cancel_leaves_table_unchanged(db):
    ch = _channel(db)
    _downloaded(db, ch, 1, "a.pdf")
    _downloaded(db, ch, 2, "b.pdf")

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        await pilot.press("slash")
        await pilot.press("escape")
        assert table.row_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v -k "filter or cycle"`
Expected: FAIL — `c`/`l`/`/` have no bound actions yet, so table contents don't change (the `test_cycle_*` and `test_text_filter_matches_filename` assertions on `row_count` after the key press fail).

- [ ] **Step 3: Add filtering to `reader.py`**

Add these imports to the top of `reader.py`:

```python
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input
```

Add the `TextPromptScreen` class (before `LibraryScreen`):

```python
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
```

Replace the `LibraryScreen` class with this expanded version:

```python
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
```

(This replaces the entire `LibraryScreen` class from Task 3 — the `_load_rows` body changes to route through `_apply_filters`, so copy the full class above rather than patching individual methods.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (all tests, including Task 3's)

- [ ] **Step 5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: add channel/language/text filtering to LibraryScreen"
```

---

## Task 5: Open a file — `ReaderScreen` stub + all four open-path outcomes

Wires `Enter` on a row to open the file. `ReaderScreen` itself is a minimal stub in this task (just enough to prove the right `Document` reached it) — its actual TOC/scroll UI is built in Task 6. This task's job is getting the *decision* right: readable formats parse and open; everything else fails visibly and safely.

**Files:**
- Modify: `reader.py`
- Test: `tests/test_reader.py`

**Interfaces:**
- Consumes: `reader_document.build_document(path: Path, ext: str) -> Document | None` (Task 2).
- Produces: `reader.ReaderScreen(filename: str, document: Document)` — stub for now; Task 6 replaces its `compose()`/adds TOC+scroll but keeps this constructor signature.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reader.py`:

```python
from unittest.mock import patch
from reader_document import Document, Section, TocEntry


async def test_enter_on_pdf_opens_reader_screen(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    fake_doc = Document(
        sections=[Section(title="Page 1", text="hello world")],
        toc=[TocEntry(title="Page 1", section_index=0)],
    )
    app = BrowseApp(db)
    with patch("reader.build_document", return_value=fake_doc):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "ReaderScreen"


async def test_enter_on_unsupported_extension_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "comic.cbz"
    f.write_bytes(b"fake")
    _downloaded(db, ch, 1, "comic.cbz", ext="cbz", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.screen.__class__.__name__ == "LibraryScreen"


async def test_enter_on_missing_file_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    missing_path = str(tmp_path / "gone.pdf")  # never created on disk
    _downloaded(db, ch, 1, "gone.pdf", ext="pdf", local_path=missing_path)

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.screen.__class__.__name__ == "LibraryScreen"


async def test_enter_on_textless_pdf_shows_message_and_stays_on_library(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "scanned.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "scanned.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=None):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "LibraryScreen"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v -k "enter_on"`
Expected: FAIL — `Enter` currently does nothing (no `on_data_table_row_selected` handler exists yet), so `app.screen` never changes and there's no `ReaderScreen`.

- [ ] **Step 3: Add the open-file handler and `ReaderScreen` stub**

Add to the imports at the top of `reader.py`:

```python
import asyncio
from pathlib import Path

from reader_document import build_document, Document
```

Add the `ReaderScreen` stub (place it after `TextPromptScreen`, before `LibraryScreen` — `LibraryScreen` will reference it):

```python
class ReaderScreen(Screen):
    """Shows one opened document. TOC sidebar and scrolling content pane are
    added in Task 6; in-document search in Task 7."""

    def __init__(self, filename: str, document: Document) -> None:
        super().__init__()
        self.filename = filename
        self.document = document

    def compose(self) -> ComposeResult:
        yield Footer()
```

Add this method to `LibraryScreen` (anywhere inside the class body):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: open PDF/EPUB rows into ReaderScreen; message on unreadable/missing/textless files"
```

---

## Task 6: `ReaderScreen` — TOC sidebar, scrollable content, close

Builds out the real `ReaderScreen` UI: a TOC list on the left, scrollable section text on the right, `Esc`/`q` to go back.

**Files:**
- Modify: `reader.py`
- Test: `tests/test_reader.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reader.py`:

```python
from textual.widgets import Static, ListView


def _fake_two_section_document() -> Document:
    return Document(
        sections=[
            Section(title="Page 1", text="First page text."),
            Section(title="Page 2", text="Second page text."),
        ],
        toc=[
            TocEntry(title="Page 1", section_index=0),
            TocEntry(title="Page 2", section_index=1),
        ],
    )


async def test_reader_screen_shows_toc_entries_and_section_text(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            toc = app.query_one(ListView)
            assert len(toc.children) == 2
            first_section = app.query_one("#section-0", Static)
            assert "First page text." in str(first_section.renderable)


async def test_selecting_toc_entry_scrolls_to_section(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            toc = app.query_one(ListView)
            toc.index = 1
            await pilot.press("enter")  # select "Page 2" in the TOC
            second_section = app.query_one("#section-1", Static)
            assert second_section.region.y <= app.query_one("#reader-content").scroll_offset.y + app.query_one("#reader-content").size.height


async def test_escape_returns_to_library_screen(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_fake_two_section_document()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            assert app.screen.__class__.__name__ == "ReaderScreen"
            await pilot.press("escape")
            assert app.screen.__class__.__name__ == "LibraryScreen"
```

`test_selecting_toc_entry_scrolls_to_section`'s scroll-position assertion is deliberately loose (it checks the target section is within the visible viewport, not an exact pixel offset) — Textual's scroll animation timing makes exact-offset assertions flaky; "is it now visible" is the actual behavior that matters.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v -k "reader_screen or toc_entry or escape_returns"`
Expected: FAIL — `ReaderScreen.compose()` is currently just a `Footer()`, no `ListView`, no `#section-N` widgets, no key bindings.

- [ ] **Step 3: Build out `ReaderScreen`**

Add to the imports at the top of `reader.py`:

```python
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Label, ListItem, ListView, Static
```

Replace the `ReaderScreen` stub with:

```python
class ReaderScreen(Screen):
    BINDINGS = [
        ("escape", "close", "Back"),
        ("q", "close", "Back"),
    ]

    def __init__(self, filename: str, document: Document) -> None:
        super().__init__()
        self.filename = filename
        self.document = document

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
```

Add CSS to `BrowseApp` so the sidebar and content pane are visually distinct and the sidebar doesn't eat the whole screen. Add a `CSS` class attribute to `BrowseApp`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: build ReaderScreen -- TOC sidebar, scrollable content, Esc/q to close"
```

---

## Task 7: In-document search

Adds `/` (search), `n`/`N` (next/prev match) to `ReaderScreen`. Search is section-level: it jumps to the next/previous section containing the query, reusing `TextPromptScreen` from Task 4 rather than building a second input modal.

**Files:**
- Modify: `reader.py`
- Test: `tests/test_reader.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reader.py`:

```python
def _three_section_document_with_needle() -> Document:
    return Document(
        sections=[
            Section(title="Page 1", text="Nothing interesting here."),
            Section(title="Page 2", text="The needle is here."),
            Section(title="Page 3", text="Another needle appears."),
        ],
        toc=[
            TocEntry(title="Page 1", section_index=0),
            TocEntry(title="Page 2", section_index=1),
            TocEntry(title="Page 3", section_index=2),
        ],
    )


async def test_search_jumps_to_first_match(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"needle")
            await pilot.press("enter")
            reader_screen = app.screen
            assert reader_screen._matches == [1, 2]
            assert reader_screen._match_pos == 0


async def test_next_match_cycles_forward(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"needle")
            await pilot.press("enter")
            await pilot.press("n")
            reader_screen = app.screen
            assert reader_screen._match_pos == 1
            await pilot.press("n")
            assert reader_screen._match_pos == 0  # wraps around


async def test_search_no_matches_notifies_and_does_not_crash(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF-fake")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    with patch("reader.build_document", return_value=_three_section_document_with_needle()):
        async with app.run_test() as pilot:
            await pilot.press("enter")
            await pilot.press("slash")
            await pilot.press(*"xyzzy")
            await pilot.press("enter")
            reader_screen = app.screen
            assert reader_screen._matches == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v -k "search or next_match"`
Expected: FAIL with `AttributeError: 'ReaderScreen' object has no attribute '_matches'` (search isn't wired up yet).

- [ ] **Step 3: Add search to `ReaderScreen`**

Replace the `ReaderScreen` class's `BINDINGS` and `__init__`, and add the new methods:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: add in-document search (/, n, N) to ReaderScreen"
```

---

## Task 8: Delete flow

Adds `d` on `LibraryScreen` to delete the file under the cursor, with a confirmation modal. Reuses `db.mark_discarded_many` with the DB-write-before-disk-unlink ordering already fixed in `webui/app.py`, and re-reads the row from the DB immediately before acting (handling the case where the listener or web UI already removed it).

**Files:**
- Modify: `reader.py`
- Test: `tests/test_reader.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reader.py`:

```python
import sqlite3


async def test_delete_confirmed_removes_file_marks_discarded_and_removes_row(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        assert table.row_count == 1
        await pilot.press("d")
        await pilot.press("enter")  # confirm dialog's default "Delete" button
        assert table.row_count == 0

    assert not f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "discarded"
    assert row["local_path"] is None


async def test_delete_cancelled_keeps_file(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        table = app.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("escape")  # cancel
        assert table.row_count == 1

    assert f.exists()
    row = db.get_media(media_id)
    assert row["status"] == "downloaded"


async def test_delete_when_already_removed_notifies_and_refreshes(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    media_id = _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)
    async with app.run_test() as pilot:
        # Simulate the web UI discarding it concurrently, between listing and confirming.
        db.mark_discarded_many([media_id])
        f.unlink()

        table = app.query_one("#library-table")
        await pilot.press("d")
        await pilot.press("enter")
        assert table.row_count == 0  # still cleared from the visible table


async def test_delete_surfaces_locked_database_as_notification_not_crash(db, tmp_path):
    ch = _channel(db)
    f = tmp_path / "book.pdf"
    f.write_bytes(b"content")
    _downloaded(db, ch, 1, "book.pdf", ext="pdf", local_path=str(f))

    app = BrowseApp(db)

    def raise_locked(_ids):
        raise sqlite3.OperationalError("database is locked")

    async with app.run_test() as pilot:
        with patch.object(db, "mark_discarded_many", side_effect=raise_locked):
            table = app.query_one("#library-table")
            await pilot.press("d")
            await pilot.press("enter")
            assert table.row_count == 1  # unchanged -- delete did not go through

    assert f.exists()  # never unlinked -- DB write is attempted before disk unlink
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reader.py -v -k delete`
Expected: FAIL — `d` has no bound action yet.

- [ ] **Step 3: Add delete to `reader.py`**

Add to the imports:

```python
import sqlite3

from textual.widgets import Button
```

Add the `ConfirmDeleteScreen` class (place it after `TextPromptScreen`):

```python
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
```

(The `on_key` handler makes plain `Enter` confirm without needing to tab to the "Delete" button first — matches the tests above, which press `enter` directly after `d`.)

Add the `"d"` binding and delete methods to `LibraryScreen`:

```python
    BINDINGS = [
        ("c", "cycle_channel", "Channel"),
        ("l", "cycle_language", "Language"),
        ("/", "filter_text", "Filter"),
        ("d", "delete_selected", "Delete"),
    ]
```

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reader.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `uv run pytest -q`
Expected: PASS (every test in the project, not just the new ones)

- [ ] **Step 6: Commit**

```bash
git add reader.py tests/test_reader.py
git commit -m "feat: add delete (d + confirm) to LibraryScreen"
```

---

## Task 9: Wire `browse` into `main.py` and `tgdctl.py`

Makes the feature reachable: `tgdctl browse` on the host, dispatching into `main.py browse` inside the container, exactly like `discard` does today.

**Files:**
- Modify: `main.py`
- Modify: `tgdctl.py`
- Test: `tests/test_main_dispatch.py` (new — thin dispatch-only check, matching the existing implicit coverage level of `discard`'s dispatch)

- [ ] **Step 1: Write the failing test**

`discard`'s dispatch has no dedicated test today (checked: no existing test imports `main.run` or exercises its command branches directly), so this plan holds `browse` to the same bar `discard` already has, not a higher one — per the spec's testing section. Create `tests/test_main_dispatch.py`:

```python
"""Thin coverage for main.py's command dispatch -- matches the existing
(implicit, untested) coverage level of `discard`'s dispatch: this only
proves `browse` is wired to build_parser() and recognized as a DB-only
command, not a full behavioral test (that's tests/test_reader.py's job)."""
from main import build_parser


def test_browse_is_a_recognized_subcommand():
    parser = build_parser()
    args = parser.parse_args(["browse"])
    assert args.command == "browse"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_dispatch.py -v`
Expected: FAIL with `SystemExit` / argparse error — `browse` isn't a registered subcommand yet.

- [ ] **Step 3: Add `browse` to `main.py`**

In `main.py`'s `build_parser()`, add after the `discard` line (`main.py:81`):

```python
    sub.add_parser("discard", help="Review downloaded files and delete unwanted ones")

    sub.add_parser("browse", help="Browse, read (PDF/EPUB), and delete downloaded files in a terminal UI")

```

In `main.py`'s `run()` function, add a new DB-only branch alongside the existing `discard` branch (`main.py:139-155`):

```python
    if args.command == "discard":
        from ui import select_discard
        downloaded = db.get_downloaded_media()
        to_delete = await select_discard(downloaded)
        if to_delete:
            deleted = 0
            for item in to_delete:
                if item.get("local_path"):
                    p = Path(item["local_path"])
                    if p.exists():
                        p.unlink()
                        deleted += 1
                db.mark_discarded(item["id"])
            console.print(f"[green]Deleted {deleted}/{len(to_delete)} file(s).[/green]")
        else:
            console.print("[dim]Nothing deleted.[/dim]")
        return
    if args.command == "browse":
        from reader import BrowseApp
        await BrowseApp(db).run_async()
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_dispatch.py -v`
Expected: PASS

- [ ] **Step 5: Verify `App.run_async` is the right entry point**

`main.py`'s `run()` is already inside an `asyncio.run(...)` call (see `main.py`'s `main()` function), so `BrowseApp` must be driven with Textual's async-embedding entry point rather than the synchronous `App.run()` (which calls `asyncio.run()` itself and would raise `RuntimeError: asyncio.run() cannot be called from a running event loop`). Confirm the method exists and has this signature before relying on it:

Run: `uv run python -c "from textual.app import App; import inspect; print(inspect.signature(App.run_async))"`
Expected: prints a signature (e.g. `(self, *, headless: bool = False, ...) -> ReturnType | None`) with no arguments required beyond `self`. If this fails, `run_async` isn't the right name in the installed Textual version — search the installed package instead: `uv run python -c "from textual.app import App; print([m for m in dir(App) if 'run' in m])"` and use whichever async-capable run method it lists instead, updating `main.py`'s `browse` branch accordingly.

- [ ] **Step 6: Add `browse` to `tgdctl.py`**

In `tgdctl.py`'s argparse setup, add after the `discard` line:

```python
    sub.add_parser("discard")
    sub.add_parser("browse")
```

Add to the dispatch, after the `discard` branch:

```python
    elif args.command == "discard":
        # No listener restart needed — discard only manages local files and DB.
        sys.exit(app("discard", interactive=True))
    elif args.command == "browse":
        # No listener restart needed — browse only manages local files and DB.
        sys.exit(app("browse", interactive=True))
```

Add a help line to the docstring/help text block (find the `discard` line in the `app commands (proxied into the running container):` section near the top of `tgdctl.py` and add below it):

```
  discard                 Review downloaded files and delete unwanted ones
  browse                  Browse, read, and delete downloaded files in a terminal UI
```

- [ ] **Step 7: Manual end-to-end smoke test**

This step is not automated — run it by hand against the real deployment:

```bash
uv run tgdctl browse
```

Confirm: the library table appears with real downloaded files; arrow keys navigate; `Enter` on a PDF or EPUB opens the reader with a populated TOC; `Esc` returns to the list; `c`/`l`/`/` filter as expected; `d` on a test file (pick one you don't mind losing, or create a throwaway one first) shows the confirm dialog and actually deletes on confirm; `q` from the library quits the app cleanly back to the shell.

- [ ] **Step 8: Commit**

```bash
git add main.py tgdctl.py tests/test_main_dispatch.py
git commit -m "feat: wire tgdctl browse into main.py and tgdctl.py"
```

---

## Task 10: Documentation

Updates `CLAUDE.md` so the new command, file, and dependency are discoverable the way every other subcommand and file already is.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add `browse` to the subcommand list**

In `CLAUDE.md`'s `### Entry points` section, update the subcommand comment line:

```
# app subcommands: listen | subscribe | unsubscribe | channels | discard | browse | status | history | scrape
```

- [ ] **Step 2: Add `reader.py` and `reader_document.py` to the file layout table**

In `CLAUDE.md`'s `### File layout` table, add two rows after the `ui.py` row:

```
| `ui.py` | Interactive `select_discard` checkbox UI (InquirerPy) |
| `reader_document.py` | Adapts `search/chunker.py`'s extracted text into a reader-ready `Document` (sections + table of contents) for `reader.py` |
| `reader.py` | Textual TUI for `tgdctl browse` — list/filter/delete downloaded files, read PDF/EPUB in the terminal |
```

- [ ] **Step 3: Add `browse` to the `tgdctl` usage block**

In `CLAUDE.md`'s `### Usage (Docker)` section, add after the `discard` line:

```
uv run tgdctl discard                # review downloaded files and delete unwanted ones (no listener restart needed)
uv run tgdctl browse                 # browse, read (PDF/EPUB), and delete downloaded files in a terminal UI (no listener restart needed)
```

- [ ] **Step 4: Add `textual` to the Stack list**

In `CLAUDE.md`'s `### Stack` section, add after the `InquirerPy` line:

```
- **InquirerPy** — interactive checkbox file selection in the terminal (`discard`)
- **Textual** — terminal UI framework for `tgdctl browse` (list/filter/delete/read downloaded files; PDF/EPUB reading reuses `search/chunker.py`'s existing text extraction, no new C-extension dependencies)
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document tgdctl browse, reader.py, reader_document.py, and the textual dependency"
```

---

## Self-Review Notes (for the plan author — not part of the executable plan)

- **Spec coverage:** list (Tasks 3-4) ✓, delete (Task 8) ✓, PDF/EPUB read with TOC (Tasks 2, 5, 6) ✓, continuous scroll (Task 6, native Textual `VerticalScroll`) ✓, in-doc search (Task 7) ✓, unified single-app two-screen architecture (Tasks 3-8 all build one `BrowseApp`) ✓, `tgdctl` proxy invocation (Task 9) ✓, `discard`/`webui`/`chunker.py` untouched (verified — only `webui/app.py`'s dedupe call site changes, and that's explicitly in-spec) ✓, error handling for missing file / no text / malformed / large file / concurrent modification (Tasks 5, 8) ✓, testing approach via `Pilot` (Tasks 3-8) ✓, out-of-scope items not built (mobi/etc., search deep-link, rename/move — none appear in any task) ✓.
- **Type consistency checked:** `Document`/`Section`/`TocEntry` field names match between Task 2's definition and every later task's usage (`sections`, `toc`, `title`, `text`, `section_index`). `build_document(path: Path, ext: str) -> Document | None` signature matches its one call site in Task 5. `dedupe_by_hash(items: list[dict]) -> list[dict]` matches between Task 1's definition and Tasks 3/4's usage.
