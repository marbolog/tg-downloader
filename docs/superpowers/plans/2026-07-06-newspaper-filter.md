# Newspaper/Periodical Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-discard newspaper/periodical-shaped files (PDF/EPUB) after download, opt-in via config, using a filename-date-or-dateline-repetition heuristic — mirroring the existing German-language and topic-keyword filters.

**Architecture:** A new pure predicate `_looks_like_newspaper(filename, pages) -> bool` in `lang_filter.py` detects two independent signals: an explicit date in the filename, or a date-shaped token repeated across at least half of the sampled pages/chapters (a running masthead/footer date that ordinary books don't produce). It plugs into the existing `analyze_file()` combined-check pipeline (alongside language and topic detection) and into a new standalone `detect_newspaper()` for the retroactive `tgdctl scan-newspapers` command. The new `discard_newspapers` boolean threads through `downloader.py` and `listener.py` exactly the way `topic_keywords` already does.

**Tech Stack:** Python 3.11, existing deps only (PyMuPDF `fitz`, stdlib `re`/`zipfile`) — no new dependencies.

## Global Constraints

- Follow `lang_filter.py`'s existing docstring convention (module + function docstrings document invariants, not mechanics).
- Filters are opt-in via `config.yaml`, default `false`/off — matches `discard_topics` convention (absent config = no behavior change for existing installs).
- No new DB columns or schema changes — reuse `db.mark_discarded()` exactly like the topic filter.
- `tests/test_lang_filter.py`'s existing convention: no tests that require real PDF/EPUB file fixtures (see its module docstring) — only pure-Python logic gets unit tests; extraction plumbing changes are verified manually.
- Every CLAUDE.md-documented filter has a matching `tgdctl scan-*` retroactive command — this feature needs `scan-newspapers` for consistency with `scan-languages`/`scan-topics`.

---

### Task 1: Newspaper detection predicate

**Files:**
- Modify: `lang_filter.py`
- Test: `tests/test_lang_filter.py`

**Interfaces:**
- Produces: `_looks_like_newspaper(filename: str, pages: list[str]) -> bool` — consumed by Task 2's `analyze_file()` and `detect_newspaper()`.

- [ ] **Step 1: Write the failing tests**

Add `_looks_like_newspaper` to the existing import block at the top of `tests/test_lang_filter.py` (alphabetical position, between `_filename_is_german` and `_meets_occurrence_threshold`):

```python
from lang_filter import (
    _filename_is_german,
    _looks_like_newspaper,
    _meets_occurrence_threshold,
    _run_topic_detection,
    _strip_html,
    compile_topic_patterns,
)
```

Append a new test class at the end of the file:

```python
class TestLooksLikeNewspaper:
    def test_filename_iso_date_detected(self):
        assert _looks_like_newspaper("Der Spiegel 2026-07-06.pdf", []) is True

    def test_filename_dotted_date_detected(self):
        assert _looks_like_newspaper("Zeitung_06.07.2026.pdf", []) is True

    def test_filename_underscored_date_detected(self):
        assert _looks_like_newspaper("report_2024_01_15.pdf", []) is True

    def test_filename_without_date_and_no_pages_not_detected(self):
        assert _looks_like_newspaper("python_tutorial.pdf", []) is False

    def test_page_ratio_above_threshold_detected(self):
        pages = [
            "Ausgabe Nr. 27 . 6.7.2026 front page text",
            "regular article text with no date at all here",
            "back page dateline 6.7.2026 continues",
            "another dateline 6.7.2026 on this page too",
            "final page plain text",
        ]
        # 3 of 5 pages carry a dateline -> ratio 0.6 >= 0.5
        assert _looks_like_newspaper("masthead.pdf", pages) is True

    def test_page_ratio_below_threshold_not_detected(self):
        pages = [
            "front page dateline 6.7.2026 appears once",
            "chapter two, no dates mentioned anywhere",
            "chapter three, still no dates here",
            "chapter four, plain narrative text",
        ]
        # 1 of 4 pages -> ratio 0.25 < 0.5
        assert _looks_like_newspaper("book.pdf", pages) is False

    def test_below_min_pages_not_trusted_even_at_full_ratio(self):
        pages = [
            "dateline 6.7.2026 here",
            "dateline 6.7.2026 here too",
            "dateline 6.7.2026 again",
        ]
        # Only 3 sampled pages (< _NEWSPAPER_MIN_PAGES=4); ratio would be 1.0 but
        # the sample is too small to trust.
        assert _looks_like_newspaper("short.pdf", pages) is False

    def test_exactly_min_pages_at_threshold_detected(self):
        pages = [
            "dateline 6.7.2026 here",
            "dateline 6.7.2026 here too",
            "plain text, no date",
            "plain text, no date either",
        ]
        # 2 of 4 pages -> ratio exactly 0.5, meets the >= threshold
        assert _looks_like_newspaper("edge.pdf", pages) is True

    def test_empty_pages_list_not_detected(self):
        assert _looks_like_newspaper("plain.pdf", []) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest tests/test_lang_filter.py -v`
Expected: FAIL with `ImportError: cannot import name '_looks_like_newspaper' from 'lang_filter'`

- [ ] **Step 3: Implement the predicate**

In `lang_filter.py`, add these constants immediately after `_GERMAN_UMLAUTS = frozenset("äöüßÄÖÜ")` (around line 53):

```python
# Numeric, locale-agnostic date patterns (no month names -- unlike the German
# filename heuristic above, newspapers arrive in many languages).
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(\d{4}[-_.]\d{2}[-_.]\d{2}|\d{2}[-_.]\d{2}[-_.]\d{4})(?!\d)"
)
_DATELINE_RE = re.compile(
    r"(?<!\d)(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})(?!\d)"
)
_NEWSPAPER_PAGE_RATIO = 0.5   # fraction of sampled pages/chapters needing a dateline
_NEWSPAPER_MIN_PAGES = 4      # below this sample size the ratio is too noisy to trust
```

(Lookbehind/lookahead on digits only -- not `\b` -- so a date directly touching an
underscore or letter, e.g. `Zeitung_06.07.2026.pdf`, still matches: underscores are
word characters, so `\b` would fail to find a boundary right before the date.)

Add the function immediately after `_filename_is_german` (around line 256):

```python
def _looks_like_newspaper(filename: str, pages: list[str]) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Two independent signals, either one triggers a match:
      1. Filename carries an explicit date (numeric only, locale-agnostic).
      2. A dateline-shaped token repeats across at least half of the sampled
         pages/chapters -- a running masthead/footer date, which books rarely
         do but daily papers do by construction. Below _NEWSPAPER_MIN_PAGES
         samples the ratio is too noisy to trust, so it's skipped.
    """
    if _FILENAME_DATE_RE.search(filename):
        return True
    if len(pages) < _NEWSPAPER_MIN_PAGES:
        return False
    hits = sum(1 for p in pages if _DATELINE_RE.search(p))
    return (hits / len(pages)) >= _NEWSPAPER_PAGE_RATIO
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest tests/test_lang_filter.py -v`
Expected: PASS (all tests, including the 9 new `TestLooksLikeNewspaper` cases)

- [ ] **Step 5: Commit**

```bash
git add lang_filter.py tests/test_lang_filter.py
git commit -m "feat: add newspaper/periodical detection predicate"
```

---

### Task 2: Wire detection into extraction pipeline

**Files:**
- Modify: `lang_filter.py`
- Modify: `config.py`
- Modify: `config.yaml.example`

**Interfaces:**
- Consumes: `_looks_like_newspaper(filename: str, pages: list[str]) -> bool` (Task 1)
- Produces: `analyze_file(..., discard_newspapers: bool = False) -> tuple[str | None, str | None, bool]` (return type changes from 2-tuple to 3-tuple) and `detect_newspaper(file_path: Path, ext: str) -> bool` — both consumed by Task 3 (`downloader.py`) and Task 5 (`main.py`'s `cmd_scan_newspapers`).

- [ ] **Step 1: Extend `_pdf_text_parts` to also return the per-page list**

Replace the function (around line 275) with:

```python
def _pdf_text_parts(file_path: Path) -> tuple[str, str, str, list[str]]:
    """Open the PDF once and return (metadata, lang_body, topic_body, pages).

    Reads up to _PDF_TOPIC_PAGES pages; lang_body is the first _PDF_PAGES of those.
    pages is the per-page text list (same sample as topic_body) -- used by the
    newspaper dateline-repetition check, which needs page-level granularity.
    """
    doc = fitz.open(str(file_path))
    meta = doc.metadata or {}
    metadata = " ".join(filter(None, [
        meta.get("title", ""),
        meta.get("subject", ""),
        meta.get("keywords", ""),
    ]))
    topic_n = min(_PDF_TOPIC_PAGES, doc.page_count)
    pages_text = [doc[i].get_text() for i in range(topic_n)]
    lang_body = " ".join(pages_text[:_PDF_PAGES])
    topic_body = " ".join(pages_text)
    return metadata, lang_body, topic_body, pages_text
```

- [ ] **Step 2: Extend `_epub_text_parts` to also return the per-chapter list**

Replace the function (around line 334) with:

```python
def _epub_text_parts(file_path: Path) -> tuple[str, str, str, list[str]]:
    """Open the EPUB once and return (metadata, lang_body, topic_body, chapters).

    chapters is the per-content-file text list behind topic_body -- used by the
    newspaper dateline-repetition check at chapter granularity.
    """
    with zipfile.ZipFile(file_path) as zf:
        metadata = ""
        opf = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
        if opf:
            try:
                opf_content = zf.read(opf).decode("utf-8", errors="ignore")
                fields = re.findall(
                    r"<dc:(?:title|subject|description)[^>]*>([^<]+)</dc:\w+>",
                    opf_content,
                    re.IGNORECASE,
                )
                if fields:
                    metadata = " ".join(fields)
            except Exception:
                pass

        all_html = sorted(
            n for n in zf.namelist()
            if n.lower().endswith((".html", ".xhtml", ".htm"))
        )
        lang_html = [n for n in all_html if "toc" not in n.lower() and "nav" not in n.lower()]

        lang_body = _read_epub_chapters(zf, lang_html[:_EPUB_CHAPTERS])
        chapters = [_read_epub_chapters(zf, [name]) for name in all_html[:_EPUB_TOPIC_CHAPTERS]]
        topic_body = " ".join(chapters)
        return metadata, lang_body, topic_body, chapters
```

- [ ] **Step 3: Extend `_extract_text_parts`'s return type**

Replace the function (around line 163) with:

```python
def _extract_text_parts(file_path: Path, ext: str) -> tuple[str, str, str, list[str]]:
    """Single-pass extraction used by analyze_file. Returns (metadata, lang_body, topic_body, pages).

    Both bodies come from a single doc/zipfile open. lang_body uses the shallow
    page/chapter limits and excludes EPUB nav/TOC noise; topic_body uses the
    deeper limits and includes nav/TOC. pages is the per-page/chapter list behind
    topic_body, used by the newspaper detector. Empty values on error or unsupported format.
    """
    try:
        if ext == "pdf":
            return _pdf_text_parts(file_path)
        if ext == "epub":
            return _epub_text_parts(file_path)
    except Exception as exc:
        log.warning(f"{file_path.name}: text extraction error: {exc}")
    return "", "", "", []
```

- [ ] **Step 4: Update `analyze_file`'s signature and body**

Replace the function (around line 71) with:

```python
def analyze_file(
    file_path: Path,
    ext: str,
    topic_keywords: dict[str, list[str]] | None = None,
    topic_min_matches: int = 2,
    topic_min_occurrences: int = 1,
    *,
    compiled_patterns: CompiledPatterns | None = None,
    discard_newspapers: bool = False,
) -> tuple[str | None, str | None, bool]:
    """Extract text once and return (language, matched_topic, is_newspaper).

    More efficient than calling detect_language() + detect_topic() separately
    when both are needed — each of those calls extracts and parses the file.
    When topics are configured or discard_newspapers is set, extraction uses a
    larger page sample (_PDF_TOPIC_PAGES) and includes document metadata, which
    also gives langdetect more signal.
    """
    has_topics = bool(topic_keywords or compiled_patterns)
    needs_pages = has_topics or discard_newspapers

    if needs_pages:
        # Single open of the file; split output so each detector gets the right slice.
        metadata, lang_text, topic_body, pages = _extract_text_parts(file_path, ext)
        topic_text = (metadata + " " + topic_body).strip() if metadata else topic_body
    else:
        # Shallow path — no topic/newspaper detection needed, so don't read deeper than necessary.
        lang_text = _extract_text(file_path, ext, topic_depth=False)
        topic_text = None
        pages = []

    lang = _run_lang_detection(file_path.name, lang_text)
    if lang is None and ext in ("pdf", "epub") and _filename_is_german(file_path.name):
        log.debug(f"{file_path.name}: detected 'de' via filename heuristic")
        lang = "de"
    elif lang is not None:
        log.debug(f"{file_path.name}: detected '{lang}' via text extraction")

    topic = None
    if has_topics:
        patterns = compiled_patterns or compile_topic_patterns(topic_keywords or {})
        topic = _run_topic_detection(file_path.name, topic_text, patterns, topic_min_matches, topic_min_occurrences)

    is_newspaper = False
    if discard_newspapers and ext in ("pdf", "epub"):
        is_newspaper = _looks_like_newspaper(file_path.name, pages)
        if is_newspaper:
            log.debug(f"{file_path.name}: detected as newspaper/periodical")

    return lang, topic, is_newspaper
```

- [ ] **Step 5: Add the standalone `detect_newspaper` function**

Add immediately after `detect_topic` (around line 159, before the `# ── Internal helpers` divider):

```python
def detect_newspaper(file_path: Path, ext: str) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Used by the retroactive `scan-newspapers` command, which calls this
    directly per file rather than through analyze_file's combined
    language+topic path.
    """
    if ext not in ("pdf", "epub"):
        return False
    _, _, _, pages = _extract_text_parts(file_path, ext)
    return _looks_like_newspaper(file_path.name, pages)
```

- [ ] **Step 6: Update the module docstring**

At the top of `lang_filter.py`, after the "Combined analysis" paragraph (around line 21), add:

```python
Newspaper/periodical detection:
  _looks_like_newspaper() flags files via an explicit date in the filename, or
  a dateline-shaped token repeated across at least half of the sampled
  pages/chapters (a running masthead date, unlike ordinary books). This is a
  format signal, not a subject-matter one -- unlike discard_topics, it uses no
  vocabulary keywords, since a newspaper can be about any topic. Opt-in via
  filters.discard_newspapers in config.yaml; independent of language/topic
  filtering (analyze_file() runs all three and returns a 3-tuple).
"""
```

(Insert this text before the closing `"""` of the existing module docstring.)

- [ ] **Step 7: Add the config default**

In `config.py`, in `_apply_defaults` (around line 45-50), add one line after `filters.setdefault("topic_min_keyword_occurrences", 1)`:

```python
    filters = raw.setdefault("filters", {})
    filters.setdefault("extensions", [])
    filters["extensions"] = [e.lower().lstrip(".") for e in filters["extensions"]]
    filters.setdefault("discard_topics", {})
    filters.setdefault("topic_min_matches", 2)
    filters.setdefault("topic_min_keyword_occurrences", 1)
    filters.setdefault("discard_newspapers", False)
```

- [ ] **Step 8: Document the config key in `config.yaml.example`**

After the `topic_min_keyword_occurrences: 2` comment line (around line 56), add:

```yaml
  # Optional: auto-discard files that look like newspapers/periodicals rather
  # than books. Detected via an explicit date in the filename, or a dateline
  # repeated across at least half the sampled pages/chapters (a running
  # masthead date -- something ordinary books rarely do). Only PDF and EPUB
  # are scanned. Run `tgdctl scan-newspapers` to apply retroactively.
  # discard_newspapers: true
```

- [ ] **Step 9: Verify manually**

The project's test convention deliberately excludes real PDF/EPUB extraction from
`tests/test_lang_filter.py` (see its module docstring) — this step is a manual
sanity check instead of a committed test.

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest -q`
Expected: PASS, no regressions (confirms the 4-tuple change didn't break existing callers)

Then run this throwaway check (not committed) to confirm the wiring works end-to-end:

```bash
cd /home/marcello/devel-with-claude/tg-downloader && uv run python3 -c "
import fitz
from pathlib import Path
from lang_filter import analyze_file, detect_newspaper

path = Path('/tmp/claude-1000/-home-marcello-devel-with-claude-tg-downloader/5206d437-7f1a-4c2e-8eb2-5cab6207827b/scratchpad/test_newspaper.pdf')
doc = fitz.open()
for i in range(5):
    page = doc.new_page()
    page.insert_text((50, 50), f'Ausgabe Nr. {i} . 6.7.2026 Lorem ipsum dolor sit amet ' * 20)
doc.save(str(path))
doc.close()

print('detect_newspaper:', detect_newspaper(path, 'pdf'))
lang, topic, is_np = analyze_file(path, 'pdf', discard_newspapers=True)
print('analyze_file:', lang, topic, is_np)
print('analyze_file (opt-out):', analyze_file(path, 'pdf', discard_newspapers=False))
path.unlink()
"
```
Expected output: `detect_newspaper: True`, `analyze_file: None None True`, and the opt-out call's third element is `False`.

- [ ] **Step 10: Commit**

```bash
git add lang_filter.py config.py config.yaml.example
git commit -m "feat: wire newspaper detection into analyze_file and detect_newspaper"
```

---

### Task 3: Wire `discard_newspapers` through `downloader.py`

**Files:**
- Modify: `downloader.py`

**Interfaces:**
- Consumes: `analyze_file(..., discard_newspapers: bool = False) -> tuple[str | None, str | None, bool]` (Task 2)
- Produces: `download_item(..., discard_newspapers: bool = False)` — consumed by Task 4 (every call site in `listener.py`)

- [ ] **Step 1: Update `download_item`'s signature and body**

In `downloader.py`, replace the function signature (lines 14-25) and the `analyze_file` call plus discard checks (lines 54-68) with:

```python
async def download_item(
    client: TelegramClient,
    db: Database,
    item: dict,
    dest: Path,
    semaphore: asyncio.Semaphore,
    *,
    message=None,
    topic_keywords: dict | None = None,
    topic_min_matches: int = 2,
    topic_min_occurrences: int = 1,
    discard_newspapers: bool = False,
) -> bool:
```

```python
            ext = item.get("ext") or ""

            lang, topic, is_newspaper = analyze_file(
                filepath, ext, topic_keywords, topic_min_matches, topic_min_occurrences,
                discard_newspapers=discard_newspapers,
            )

            if lang == DISCARD_LANG:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (German): {item['filename']}")
                return True

            if topic:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (topic: {topic}): {item['filename']}")
                return True

            if is_newspaper:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (newspaper): {item['filename']}")
                return True
```

- [ ] **Step 2: Verify manually**

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run python3 -c "import downloader"`
Expected: no output, no exception (confirms no syntax errors)

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest -q`
Expected: PASS, no regressions

- [ ] **Step 3: Commit**

```bash
git add downloader.py
git commit -m "feat: thread discard_newspapers through download_item"
```

---

### Task 4: Wire `discard_newspapers` through `listener.py`

**Files:**
- Modify: `listener.py`

**Interfaces:**
- Consumes: `download_item(..., discard_newspapers: bool = False)` (Task 3)
- Produces: `run_listener(client, db, config)` now reads `config["filters"]["discard_newspapers"]` and threads it through every internal call — no external interface change (still just `run_listener(client, db, config)`), so nothing outside this file depends on the new parameter names.

This task is purely mechanical: add one new parameter to six functions and pass it through every call site, in the same position `topic_min_occurrences` already occupies (i.e., directly after it, before any additional keyword-only params like `warn_empty`).

- [ ] **Step 1: `run_listener`**

Replace lines 14-59 with:

```python
async def run_listener(client: TelegramClient, db: Database, config: dict) -> None:
    """Start the real-time listener. Blocks until the client disconnects."""
    destination = Path(config["download"]["destination"])
    allowed = set(config["filters"]["extensions"])
    retention_days = config["download"]["retention_days"]
    concurrent_downloads = config["download"]["concurrent_downloads"]
    topic_keywords = config["filters"].get("discard_topics") or {}
    topic_min_matches = config["filters"].get("topic_min_matches", 2)
    topic_min_occurrences = config["filters"].get("topic_min_keyword_occurrences", 1)
    discard_newspapers = config["filters"].get("discard_newspapers", False)
    destination.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrent_downloads)

    await _flush_pending(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers)
    await _heal_missing(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers)
    await _backfill_missed(client, db, allowed, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers)

    asyncio.create_task(_heal_search_index(db))
    asyncio.create_task(_cleanup_loop(db, retention_days))
    asyncio.create_task(_heartbeat_loop(db))
    asyncio.create_task(_backfill_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers,
    ))
    asyncio.create_task(_deep_reconcile_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers,
    ))

    channels = db.list_channels()
    log.info(f"Listening -- {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  . {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed, client, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    # Catch up on anything that arrived while we were offline / mid-reconnect.
    # Must run after the handler above is registered so loaded updates are processed.
    await client.catch_up()

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()
```

- [ ] **Step 2: `_flush_pending`**

Replace lines 62-79 with:

```python
async def _flush_pending(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    """Download all items that are pending in the DB (e.g. from a previous scrape)."""
    pending = db.get_pending_media()
    if not pending:
        return
    log.info(f"Flushing {len(pending)} pending item(s) from previous session(s)...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        discard_newspapers=discard_newspapers)
          for item in pending],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Flush complete: {ok}/{len(pending)} succeeded")
```

- [ ] **Step 3: `_heal_missing`**

Replace lines 82-103 with:

```python
async def _heal_missing(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    """Re-download files marked 'downloaded' in the DB but absent from disk."""
    downloaded = db.get_downloaded_media()
    missing = [
        item for item in downloaded
        if not item.get("local_path") or not Path(item["local_path"]).exists()
    ]
    if not missing:
        return
    log.info(f"Healing {len(missing)} file(s) present in DB but missing from disk...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        discard_newspapers=discard_newspapers)
          for item in missing],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Heal complete: {ok}/{len(missing)} restored")
```

- [ ] **Step 4: `_backfill_missed`**

Replace lines 106-174 with:

```python
async def _backfill_missed(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches,
    topic_min_occurrences, discard_newspapers, warn_empty: bool = True
) -> None:
    """Fetch messages that arrived while the service was down and download them.

    `warn_empty` controls the per-channel "no prior messages" warning: useful once
    at startup, but suppressed by the hourly safety-net loop so known-empty
    channels don't emit the same warning every hour (the heartbeat already
    surfaces `channels_no_messages`)."""
    for ch in db.list_channels():
        max_id = db.get_max_message_id(ch["id"])
        if max_id is None:
            if warn_empty:
                log.warning(
                    f"Backfill: no prior messages recorded for {ch['title']!r} -- "
                    f"run 'scrape --channel {ch['identifier']}' to pull existing history"
                )
            continue

        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            log.warning(f"Backfill: cannot resolve {ch['identifier']!r}: {exc}")
            continue

        tasks = []
        async for message in client.iter_messages(entity, min_id=max_id):
            if not message.media:
                continue
            item_meta = _extract_media(message)
            if item_meta is None:
                continue
            if allowed and item_meta["ext"] not in allowed:
                continue
            db_id = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item_meta["filename"],
                size=item_meta["size"],
                mime_type=item_meta["mime_type"],
                ext=item_meta["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if db_id:
                tasks.append(download_item(
                    client, db,
                    {
                        "id": db_id,
                        "channel_identifier": ch["identifier"],
                        "channel_telegram_id": ch["telegram_id"],
                        "channel_title": ch["title"],
                        "message_id": message.id,
                        "filename": item_meta["filename"],
                        "size": item_meta["size"],
                        "ext": item_meta["ext"],
                    },
                    dest, semaphore, message=message,
                    topic_keywords=topic_keywords,
                    topic_min_matches=topic_min_matches,
                    topic_min_occurrences=topic_min_occurrences,
                    discard_newspapers=discard_newspapers,
                ))

        if tasks:
            log.info(f"Backfilling {len(tasks)} missed item(s) from {ch['title']}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Backfill {ch['title']}: {ok}/{len(tasks)} succeeded")
```

- [ ] **Step 5: `_backfill_loop`**

Replace lines 231-252 with:

```python
async def _backfill_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    """Re-run backfill every hour as a safety net against silent update-stream
    stalls. Telethon's real-time update channel can go stale after a network blip
    while the TCP connection (and this asyncio loop) stays alive -- the process
    keeps logging heartbeats but no `events.NewMessage` ever fires, so downloads
    silently stop until the next restart. Polling each channel for messages newer
    than the last recorded id closes that gap within the hour, independent of why
    real-time delivery stopped. Harmless when real-time is healthy: `min_id` is
    already current, so nothing new is fetched and no message is double-downloaded
    (save_media_message dedups on message_id)."""
    while True:
        await asyncio.sleep(3600)
        try:
            await _backfill_missed(
                client, db, allowed, dest, semaphore,
                topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers,
                warn_empty=False,
            )
        except Exception as exc:
            log.error(f"Periodic backfill error: {exc}", exc_info=True)
```

- [ ] **Step 6: `_deep_reconcile_loop` and `_deep_reconcile`**

Replace lines 266-337 with:

```python
async def _deep_reconcile_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    """Once a day, re-scan each channel's recent window ignoring the backfill
    watermark, recovering media that real-time delivery dropped mid-burst."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            await _deep_reconcile(
                client, db, allowed, dest, semaphore,
                topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers,
            )
        except Exception as exc:
            log.error(f"Deep reconcile error: {exc}", exc_info=True)


async def _deep_reconcile(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    for ch in db.list_channels():
        recorded = db.get_recorded_message_ids(ch["id"])
        if not recorded:
            continue  # never seen this channel — that's `scrape`'s job, not reconcile's
        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            log.warning(f"Deep reconcile: cannot resolve {ch['identifier']!r}: {exc}")
            continue

        tasks = []
        async for message in client.iter_messages(entity, limit=RECONCILE_WINDOW):
            if not message.media or message.id in recorded:
                continue
            item_meta = _extract_media(message)
            if item_meta is None:
                continue
            if allowed and item_meta["ext"] not in allowed:
                continue
            db_id = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item_meta["filename"],
                size=item_meta["size"],
                mime_type=item_meta["mime_type"],
                ext=item_meta["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if db_id:
                tasks.append(download_item(
                    client, db,
                    {
                        "id": db_id,
                        "channel_identifier": ch["identifier"],
                        "channel_telegram_id": ch["telegram_id"],
                        "channel_title": ch["title"],
                        "message_id": message.id,
                        "filename": item_meta["filename"],
                        "size": item_meta["size"],
                        "ext": item_meta["ext"],
                    },
                    dest, semaphore, message=message,
                    topic_keywords=topic_keywords,
                    topic_min_matches=topic_min_matches,
                    topic_min_occurrences=topic_min_occurrences,
                    discard_newspapers=discard_newspapers,
                ))

        if tasks:
            log.info(f"Deep reconcile: recovered {len(tasks)} mid-burst miss(es) from {ch['title']}")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Deep reconcile {ch['title']}: {ok}/{len(tasks)} downloaded")
```

- [ ] **Step 7: `_handle`**

Replace lines 370-425 with:

```python
async def _handle(
    event, db, allowed, client, dest, semaphore,
    topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers
) -> None:
    if not event.message.media:
        return

    peer = event.message.peer_id
    if isinstance(peer, PeerChannel):
        raw_id = peer.channel_id
    elif isinstance(peer, PeerChat):
        raw_id = peer.chat_id
    else:
        return

    channel = db.get_channel_by_telegram_id(raw_id)
    if channel is None:
        return

    item_meta = _extract_media(event.message)
    if item_meta is None:
        return

    if allowed and item_meta["ext"] not in allowed:
        log.debug(f"Skipping {item_meta['filename']!r}: extension not in filter")
        return

    db_id = db.save_media_message(
        channel_id=channel["id"],
        message_id=event.message.id,
        filename=item_meta["filename"],
        size=item_meta["size"],
        mime_type=item_meta["mime_type"],
        ext=item_meta["ext"],
        date=event.message.date.isoformat(),
        caption=(event.message.message or "")[:120],
    )
    if db_id:
        log.info(f"[{channel['title']}] New media: {item_meta['filename']} ({item_meta['size']} B) -- queuing download")
        asyncio.create_task(download_item(
            client, db,
            {
                "id": db_id,
                "channel_identifier": channel["identifier"],
                "channel_telegram_id": channel["telegram_id"],
                "channel_title": channel["title"],
                "message_id": event.message.id,
                "filename": item_meta["filename"],
                "size": item_meta["size"],
                "ext": item_meta["ext"],
            },
            dest, semaphore, message=event.message,
            topic_keywords=topic_keywords,
            topic_min_matches=topic_min_matches,
            topic_min_occurrences=topic_min_occurrences,
            discard_newspapers=discard_newspapers,
        ))
```

- [ ] **Step 8: Verify manually**

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run python3 -c "import listener"`
Expected: no output, no exception

Run: `cd /home/marcello/devel-with-claude/tg-downloader && grep -n "def _flush_pending\|def _heal_missing\|def _backfill_missed\|def _backfill_loop\|def _deep_reconcile\|def _handle\|def run_listener" listener.py`

For each function listed, confirm its signature line includes `discard_newspapers` as a parameter, and confirm every `download_item(` / `_backfill_missed(` / `_deep_reconcile(` call inside it passes `discard_newspapers` through (either positionally or as `discard_newspapers=discard_newspapers`). This is a manual read-through, not an automated count — the code blocks in Steps 1-7 above are the source of truth for what each function should look like after this task.

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest -q`
Expected: PASS, no regressions

- [ ] **Step 9: Commit**

```bash
git add listener.py
git commit -m "feat: thread discard_newspapers through listener.py"
```

---

### Task 5: `tgdctl scan-newspapers` command + CLAUDE.md docs

**Files:**
- Modify: `main.py`
- Modify: `tgdctl.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `detect_newspaper(file_path: Path, ext: str) -> bool` (Task 2), `_run_file_batch(items, description, handle) -> int` (existing, `main.py:289`)

- [ ] **Step 1: Add `cmd_scan_newspapers` to `main.py`**

Add immediately after `cmd_scan_topics` (after line 417):

```python
def cmd_scan_newspapers(db: Database) -> None:
    from lang_filter import detect_newspaper

    items = db.get_downloaded_media()
    if not items:
        console.print("[yellow]No downloaded files found.[/yellow]")
        return

    console.print(f"[dim]Scanning {len(items)} file(s) for newspaper/periodical pattern…[/dim]")

    discarded = 0

    def handle(item, path):
        nonlocal discarded
        ext = (item.get("ext") or "").lower()
        if detect_newspaper(path, ext):
            path.unlink(missing_ok=True)
            db.mark_discarded(item["id"])
            discarded += 1
            log.info(f"Scan-discarded (newspaper): {item['filename']}")

    missing = _run_file_batch(items, "Detecting newspapers", handle)

    console.print(
        f"\n[green]Done.[/green] Scanned {len(items)} file(s): "
        f"[red]{discarded} discarded[/red], {missing} missing."
    )
```

- [ ] **Step 2: Register the subcommand and dispatch in `main.py`**

In `build_parser()`, add immediately after the `scan-topics` subparser (line 96):

```python
    sub.add_parser("scan-newspapers", help="Retroactively detect newspaper/periodical-shaped files among already-downloaded files; auto-discard matches")
```

In `run()`, add immediately after the `scan-topics` dispatch block (line 127):

```python
    if args.command == "scan-newspapers":
        cmd_scan_newspapers(db)
        return
```

- [ ] **Step 3: Proxy the command in `tgdctl.py`**

In the epilog string, add immediately after the `scan-topics` line (line 273):

```
  scan-newspapers              Detect newspaper/periodical-shaped files; discard matches
```

Add the subparser immediately after `sub.add_parser("scan-topics")` (line 306):

```python
    sub.add_parser("scan-newspapers")
```

Add the dispatch immediately after the `scan-topics` dispatch (line 354):

```python
    elif args.command == "scan-newspapers":
        sys.exit(app("scan-newspapers"))
```

- [ ] **Step 4: Verify manually**

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run python3 main.py --help`
Expected: `scan-newspapers` appears in the subcommand list

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run python3 tgdctl.py --help`
Expected: `scan-newspapers` appears in the epilog's "app commands" list

Run: `cd /home/marcello/devel-with-claude/tg-downloader && uv run pytest -q`
Expected: PASS, no regressions

- [ ] **Step 5: Update `CLAUDE.md`**

In the "Usage (Docker)" command list, add a line immediately after `uv run tgdctl scan-topics`:

```
uv run tgdctl scan-newspapers        # retroactively detect newspaper/periodical-shaped files; discard matches
```

In the "### Content filters (`lang_filter.py`)" section, add a new subsection immediately after the "Topic filtering" bullet block (before the "Formats with no text extraction support..." paragraph):

```markdown
**Newspaper/periodical detection** — opt-in via `filters.discard_newspapers: true` (default `false`). Two independent signals, either one triggers a discard:

1. **Filename date** — an explicit numeric date in the filename (`YYYY-MM-DD`, `DD.MM.YYYY`, `DD_MM_YYYY`, etc.), locale-agnostic (no month names).
2. **Dateline repetition** — a date-shaped token appears on at least half of the sampled pages (PDF) or content files (EPUB), when at least 4 are sampled. Ordinary books rarely repeat a date on most pages; a daily paper's running masthead/footer date does this by construction.

Detection is a format signal, not a subject-matter one — unlike `discard_topics`, it doesn't use vocabulary keywords, since newspapers can cover any topic. Implemented in `lang_filter._looks_like_newspaper()` / `detect_newspaper()`. Use `tgdctl scan-newspapers` to apply retroactively to already-downloaded files.
```

- [ ] **Step 6: Commit**

```bash
git add main.py tgdctl.py CLAUDE.md
git commit -m "feat: add tgdctl scan-newspapers retroactive command and docs"
```
