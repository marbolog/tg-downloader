# CLI file browser, delete, and reader — Design

**Date:** 2026-09-18
**Status:** Approved (pending spec review)

## Goal

Let the user consume the library from the terminal, not just the web UI or
Telegram: list/browse downloaded files, delete unwanted ones, and read PDF/EPUB
content directly in the terminal. Confirmed requirements from the requester:

1. **List** downloaded files from the command line.
2. **Delete** files from the command line.
3. **Read** PDF and EPUB content in the terminal. Images don't matter for
   PDFs — text is what's being read, not a rendered page.
4. A **richer** terminal reading experience was explicitly chosen over a plain
   text pager: smooth/continuous scrolling, in-document search, and a table of
   contents / chapter-jump.
5. Other library formats (mobi, azw3, fb2, djvu, cbr, cbz) are **out of scope**
   for this pass — text extraction doesn't exist for them today, and several
   (djvu/cbr/cbz) are image-based formats where "read without images" has
   little value anyway.

## Key constraints and decisions that shaped the design

- **Invocation model:** new capability is proxied through `tgdctl` into the
  container (`docker compose exec -it`), exactly like the existing `discard`
  command, rather than a standalone host-native tool. This keeps all app logic
  and dependencies inside the container's Python environment (consistent with
  the project's Docker-first deployment model) and requires no new host
  tooling.
- **Coexistence with `discard`:** `discard`'s existing bulk multi-select
  InquirerPy checkbox-and-delete workflow is untouched. The new `browse`
  command is a separate, additive entry point better suited to "find this one
  file, read or delete it" than bulk cleanup.
- **Reuse over reimplementation:** PDF/EPUB text extraction already exists in
  `search/chunker.py` (`chunk_file()` — one chunk per PDF page, one per EPUB
  chapter, in document order, already used to build the FTS5 search index).
  The reader consumes this directly rather than re-implementing extraction.
  Deletion reuses `Database.mark_discarded_many` and the DB-write-before-
  disk-unlink ordering already fixed in `webui/app.py`'s `/api/discard`
  (commit `e6db52c` — batching that fix closed a bug where a lock error
  mid-batch could leave a file deleted from disk but still `downloaded` in
  the DB).
- **A single cohesive TUI, not two tools glued together:** list, read, and
  delete are one Textual application with two screens, not an InquirerPy list
  handing off to a separately-launched reader process. This was a genuine
  fork (Approach B below) — chosen against because gluing two different TUI
  frameworks together for one command reads as inconsistent and adds a
  process/handoff boundary for no real benefit.
- **PDF has no per-page titles.** `chunk_file()` gives page *numbers* for PDF,
  not headings, so a real PDF table of contents needs a second source:
  PyMuPDF can read a PDF's embedded outline/bookmarks via `doc.get_toc()`.
  Most published books/magazines have this; scans typically don't. When
  absent, the TOC sidebar falls back to a flat page-number list. EPUB chapters
  already carry titles via `chunk_file()`'s `chapter` field — no fallback
  needed there.

## Approach (selected: "A" — single Textual app, two screens)

One `tgdctl browse` command launches a Textual `App`:
- **`LibraryScreen`** (default): a filterable `DataTable` of downloaded files.
  `Enter` opens a file in `ReaderScreen`; `d` deletes (with confirmation).
- **`ReaderScreen`** (pushed on top when reading): TOC sidebar + scrollable
  content pane, in-document search. `Esc`/`q` pops back to the preserved list.

Textual was chosen because it's pure Python, built on `rich` (already a
project dependency, no C extensions — safe on the Pi/ARM), and its native
scrollable containers and widget-testing harness (`App.run_test()` /
`Pilot`) directly satisfy the "smooth scrolling" and "testable" requirements
without custom code.

### Rejected alternative: "B" — InquirerPy list + separate Textual reader

Keep `browse`'s list on `ui.py`'s existing InquirerPy checkbox/select pattern
(same convention as `discard`), and launch a standalone Textual reader only
when "Read" is chosen. Smaller diff, and isolates the new `textual` dependency
to just the reading path. Rejected because it produces two different
interaction models stitched into one command — visually and behaviorally
inconsistent — for a saving (one dependency's footprint) that doesn't
outweigh the UX cost, given the requester explicitly wants a cohesive
"browse and read" experience rather than two features bolted together.

### Rejected alternative: plain text pager (pre-empted before approach selection)

Page through extracted text with a simple pager (or Python's `pydoc.pager()`),
no TUI framework, no new dependency. This was the initial recommendation given
"images don't matter," but the requester explicitly chose the richer
Textual-based experience (scrolling, in-doc search, TOC) over this simpler
baseline.

## Architecture & components

### New files

- **`reader_document.py`** — pure logic, no TUI dependency. A `Document`
  dataclass (list of sections: `{title: str | None, text: str}`) and
  `build_document(path: Path, ext: str) -> Document | None`:
  - Wraps `search.chunker.chunk_file()` for the section text/order.
  - For `ext == "pdf"`: attempts `fitz.open(path).get_toc()` for section
    titles; falls back to `"Page {n}"` per section when the PDF has no
    outline.
  - For `ext == "epub"`: uses `chunk_file()`'s `chapter` field directly as
    each section's title.
  - Returns `None` when `chunk_file()` yields no chunks (textless/scanned PDF,
    unparseable file) or the path doesn't exist — callers show a message
    instead of opening an empty reader.
  - `chunker.py` itself is not modified — this is a thin adapter, so nothing
    about search indexing changes.

- **`reader.py`** — the Textual `App` and its two `Screen`s.
  - `LibraryScreen`: on mount, calls `db.list_downloaded_files(hide_dupes=True)`
    (the same method and dedup-by-hash semantics the web UI already uses, so
    what `browse` shows matches what the web UI shows) and populates a
    `DataTable` (channel, filename, size, ext, language, date). Filtering
    (`/` text filter, `c` cycle channel, `l` cycle language) is client-side
    over the already-fetched rows — library sizes here are in the thousands,
    not millions, so no pagination is needed (unlike the web UI's
    `per_page=60`, which exists for browser rendering cost, not data volume).
  - `Enter` on a `pdf`/`epub` row: calls `build_document()` (via
    `asyncio.to_thread`, so parsing a large file doesn't freeze the event
    loop) and pushes `ReaderScreen`. Any other extension: a status-bar
    message ("not readable in terminal — download via the web UI"); no crash,
    no screen change.
  - `d` deletes the current row; a confirm modal ("Delete {filename}?")
    precedes it. Whether to also support marking multiple rows for a single
    batched delete (`DataTable` has no InquirerPy-style built-in checkbox
    multi-select — it would need a tracked "marked" set rendered into the
    table) is an implementation-plan decision, not fixed here: `discard`
    already covers the bulk-delete case, so single-row delete alone may be
    sufficient for `browse`. Either way, the delete path itself runs
    `db.mark_discarded_many(ids)` *before* any disk unlink (mirroring
    `webui/app.py`'s fixed ordering), then removes files from disk; rows drop
    from the table on success.
  - `ReaderScreen`: TOC sidebar (`ListView`, from the `Document`'s section
    titles) + scrollable content pane (native Textual scrolling — continuous,
    not page-flip). `/` opens in-document search with `n`/`N` to jump between
    matches — separate from, and simpler than, the library-wide FTS5 search.
    Built once from the `Document`; no re-parsing while the screen is open.

### Changed files

- **`main.py`** — new `browse` subcommand, dispatched like `discard` (DB-only,
  no Telegram client needed).
- **`tgdctl.py`** — new `browse` case, proxied via `docker compose exec -it`
  exactly like `discard`.
- **`pyproject.toml` / Dockerfile** — add `textual` as a dependency (installs
  via the existing `uv sync` step; no new system packages, no image-size
  concern worth noting).

`discard`, `ui.py`, `webui/`, and `search/chunker.py` are all unchanged.

## Data flow

```
tgdctl browse
  → docker compose exec -it tg-downloader uv run python main.py browse
    → reader.BrowseApp().run()
      → LibraryScreen.on_mount(): db.list_downloaded_files(hide_dupes=True)
      → [Enter] → asyncio.to_thread(build_document, path, ext) → ReaderScreen
      → [d] → confirm modal → db.mark_discarded_many(ids) → unlink files → refresh table
      → [Esc/q from ReaderScreen] → back to LibraryScreen (state preserved)
```

## Error handling & edge cases

- **File missing on disk** (DB says `downloaded`, file absent — e.g. mid-heal
  or manually removed): `build_document()` checks `path.exists()` first;
  `ReaderScreen` shows an inline error state rather than crashing — mirrors
  the 404 pattern `webui/app.py` already uses for the same situation.
- **No extractable text** (image-only/scanned PDF — the same case
  `search/indexer.py` already special-cases via `db.mark_indexed` with zero
  chunks): `build_document()` returns `None`; `LibraryScreen` shows "no
  extractable text — this looks like a scanned/image PDF" instead of opening
  an empty reader.
- **Malformed PDF page:** `chunk_file()` already skips bad pages individually
  rather than aborting the whole document; the reader shows whatever pages
  did extract. A fully unparseable file behaves like the "no extractable
  text" case above.
- **Large file open latency** (library includes 80+MB magazines):
  `build_document()` runs via `asyncio.to_thread` so the Textual event loop
  stays responsive; `ReaderScreen` shows a loading state while parsing.
- **Concurrent modification** (webui or listener discards/deletes the same
  file while `browse` has it open): the delete-confirm path re-checks the row
  still exists before acting; a `sqlite3.OperationalError` (DB locked) surfaces
  as a status-bar error rather than an unhandled crash, matching
  `webui/app.py`'s `/api/discard` `503` pattern.

## Testing

- **`reader_document.py`** — plain pytest, same style as
  `tests/test_search_chunker.py`: fixture PDF/EPUB files; assert section
  titles/order/text are correct; assert a PDF with an outline uses bookmark
  titles and one without falls back to page numbers; assert `None` on a
  textless or malformed file.
- **`reader.py` screens** — Textual's own test harness
  (`App.run_test()` → `Pilot`, which simulates keypresses and asserts against
  the rendered widget tree) — the standard way Textual apps are tested, no
  bespoke harness needed. Covers: row navigation → `Enter` opens `ReaderScreen`
  with correct content; `d` → confirm → row removed + DB shows `discarded` +
  file gone from disk; unsupported extension → status message, screen
  unchanged; `Esc` from reader → list state preserved.
- **`main.py`/`tgdctl.py` wiring** — thin dispatch-only check (does `browse`
  route to the right function), matching the existing (implicit, untested)
  coverage level of `discard`'s dispatch — not holding `browse` to a higher
  bar than the command it sits next to.
- **Not automated:** actual terminal visual rendering. `Pilot` assertions
  cover widget/data state, which is what correctness depends on; visual
  smoke-testing (does it look right in a real terminal) is a manual step in
  the implementation plan, not a CI-automated one.

## Out of scope for this pass

- Reading mobi/azw3/fb2/djvu/cbr/cbz — no text extraction exists for these
  today; several are image-based formats where a text-only reader has little
  value.
- Deep-linking from `tgdctl`'s library-wide FTS5 search into a specific
  page/chapter in `ReaderScreen` (mirroring the web UI's `#page=N` links).
- Renaming/moving files from `browse`.

All three are natural follow-ups once the PDF/EPUB read/browse/delete path is
proven, but pulling any of them in now would grow this from "add CLI
consumption" into a second project.
