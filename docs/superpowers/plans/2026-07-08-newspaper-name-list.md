# Newspaper Detection: Compact Dates + Known-Publication-Name List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the newspaper-detection gap found after running `scan-newspapers` on the existing library (only ~70 of 4,093 files flagged) by adding two new independent signals to `_looks_like_newspaper()`: compact/separator-less date formats, and a config-extensible known-publication-name filename matcher.

**Architecture:** Two new regex constants (`_COMPACT_DATE_RE`, `_COMPACT_LONGDATE_RE`) join the existing filename-date checks with no signature changes. A new predicate `_filename_matches_known_publication(filename, extra_names)` checks a built-in publication-name list plus a config-supplied `filters.newspaper_names` list, anchored at the start of the (normalized) filename. `_looks_like_newspaper()` gains a `newspaper_names` parameter threading the config list down from `analyze_file()`/`detect_newspaper()`, following exactly the same call-chain pattern already used for `discard_newspapers` (`config.yaml` → `config.py` → `main.py`/`downloader.py`/`listener.py`).

**Tech Stack:** Python 3.11, existing deps only (stdlib `re`) — no new dependencies.

**Full spec:** `docs/superpowers/specs/2026-07-08-newspaper-name-list-design.md`

## Global Constraints

- Follow `lang_filter.py`'s existing docstring convention (module + function docstrings document invariants, not mechanics).
- `filters.newspaper_names` is additive to the built-in list, never a replacement — matches the spec's approved design, and differs from `discard_topics`'s "empty by default" convention deliberately (see spec's Alternatives Considered).
- No new DB columns or schema changes — reuse `db.mark_discarded()` exactly like the existing newspaper filter.
- `tests/test_lang_filter.py`'s existing convention: no tests that require real PDF/EPUB file fixtures — only pure-Python logic gets unit tests.
- Every regex addition must use the same `(?<!\d)...(?!\d)` digit-boundary guard already used elsewhere in the module, so new patterns never match inside a longer digit run.
- Exclude common single dictionary-word names (e.g. "Time") from the built-in publication list — the spec explicitly defers this to avoid false-positiving on unrelated book titles.

---

### Task 1: Compact-date regexes

**Files:**
- Modify: `lang_filter.py`
- Test: `tests/test_lang_filter.py`

**Interfaces:**
- Produces: `_COMPACT_DATE_RE`, `_COMPACT_LONGDATE_RE` (module-level compiled regexes) — consumed by `_looks_like_newspaper()` in this same task. No signature changes; this task only widens what already-parameterless filename-date detection matches.

- [ ] **Step 1: Write the failing tests**

Add to the end of the `TestLooksLikeNewspaper` class in `tests/test_lang_filter.py` (the file currently ends at line 101 with `test_page_mentioning_month_and_year_once_not_detected`):

```python
    def test_filename_compact_ddmm_date_detected(self):
        assert _looks_like_newspaper("NYT 1602.pdf", []) is True

    def test_filename_compact_mmdd_date_detected(self):
        assert _looks_like_newspaper("NY Daily News_1204.pdf", []) is True

    def test_filename_compact_yyyymmdd_date_detected(self):
        assert _looks_like_newspaper("WAPO_20240413.pdf", []) is True

    def test_filename_two_digit_year_dotted_date_detected(self):
        assert _looks_like_newspaper("FT How to Spend it 7.3.26.pdf", []) is True

    def test_filename_bare_year_not_detected_as_compact_date(self):
        assert _looks_like_newspaper("Vietnam 1975.pdf", []) is False

    def test_filename_bare_recent_year_not_detected_as_compact_date(self):
        assert _looks_like_newspaper("My Book 2026.pdf", []) is False

    def test_filename_december_31_compact_date_detected(self):
        assert _looks_like_newspaper("Late Edition 1231.pdf", []) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lang_filter.py -k compact_date_detected -v`
Expected: FAIL — `test_filename_compact_ddmm_date_detected`, `test_filename_compact_mmdd_date_detected`, `test_filename_compact_yyyymmdd_date_detected`, `test_filename_december_31_compact_date_detected` all assert `True` but currently return `False` (no compact-date signal exists yet).

Run: `uv run pytest tests/test_lang_filter.py -k two_digit_year_dotted -v`
Expected: FAIL — `test_filename_two_digit_year_dotted_date_detected` asserts `True` but currently returns `False` (`_FILENAME_DATE_RE` requires a 4-digit year for dotted-separator dates).

The bare-year tests (`test_filename_bare_year_not_detected_as_compact_date`, `test_filename_bare_recent_year_not_detected_as_compact_date`) already pass against current code — that's expected; they're regression guards for this task, not new-behavior assertions.

- [ ] **Step 3: Implement the regexes**

In `lang_filter.py`, add after the existing `_MONTH_NAME_DATE_RE` block (after line 103, before the `_NEWSPAPER_PAGE_RATIO` constant):

```python
# Compact date formats: digits jammed together with no separator, or a dotted
# date with a 2-digit year. Real-world filenames use these constantly (e.g.
# "NYT 1602.pdf", "NY Daily News_1204.pdf", "WAPO_20240413.pdf") but the
# separator-based patterns above miss them entirely.
#
# _COMPACT_DATE_RE matches a bare 4-digit run only if it's plausible as a
# day+month pair in EITHER order (day 01-31 + month 01-12, or the reverse).
# This is what excludes bare years: "2026" fails both interpretations because
# "20" is not a valid month, so a book title ending in a year never matches.
_COMPACT_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])"  # DDMM
    r"|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"  # MMDD
    r")(?!\d)"
)
# A 19xx/20xx year directly followed by a valid month and day, no separators
# ("WAPO_20240413.pdf").
_COMPACT_LONGDATE_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)
```

Update the filename-date check inside `_looks_like_newspaper()` (currently):

```python
    if _FILENAME_DATE_RE.search(filename) or _MONTH_NAME_DATE_RE.search(filename):
        return True
```

to:

```python
    if (
        _FILENAME_DATE_RE.search(filename)
        or _MONTH_NAME_DATE_RE.search(filename)
        or _DATELINE_RE.search(filename)
        or _COMPACT_DATE_RE.search(filename)
        or _COMPACT_LONGDATE_RE.search(filename)
    ):
        return True
```

`_DATELINE_RE` (already defined above, used today only for page-content sampling) already accepts 2-4 digit years with `.`/`/` separators — reusing it here for the filename check closes the 2-digit-year-dotted gap (`7.3.26`) with no new regex needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lang_filter.py -v`
Expected: PASS — all tests, including the 7 new ones and all pre-existing ones (the full suite must stay green; this task only adds OR'd conditions, so no prior `True`/`False` result can flip).

- [ ] **Step 5: Commit**

```bash
git add lang_filter.py tests/test_lang_filter.py
git commit -m "feat: detect compact/separator-less dates in newspaper filenames"
```

---

### Task 2: Known-publication-name predicate

**Files:**
- Modify: `lang_filter.py`
- Test: `tests/test_lang_filter.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `_filename_matches_known_publication(filename: str, extra_names: frozenset[str] = frozenset()) -> bool` and `_KNOWN_PUBLICATION_NAMES: frozenset[str]` — consumed by Task 3's `_looks_like_newspaper()`.

- [ ] **Step 1: Write the failing tests**

Add a new test class at the end of `tests/test_lang_filter.py`, and update the import line at the top of the file from:

```python
from lang_filter import _looks_like_newspaper
```

to:

```python
from lang_filter import _filename_matches_known_publication, _looks_like_newspaper
```

Append:

```python
class TestFilenameMatchesKnownPublication:
    def test_exact_match(self):
        assert _filename_matches_known_publication("FT.pdf") is True

    def test_match_with_suffix(self):
        assert _filename_matches_known_publication("FT EU.pdf") is True

    def test_match_with_long_suffix(self):
        assert _filename_matches_known_publication("FT How to Spend it 7.3.26.pdf") is True

    def test_no_boundary_no_match(self):
        # "nationalgeo" has no separator before "geo" -- not a word-boundary
        # match against "national geographic", so this must NOT match.
        assert _filename_matches_known_publication("nationalgeo.pdf") is False

    def test_separator_insensitive_match(self):
        assert _filename_matches_known_publication("National_Geographic_USA.pdf") is True

    def test_hyphen_separator_match(self):
        assert _filename_matches_known_publication("The-Guardian-UK-18-June-2026.pdf") is True

    def test_no_match_mid_filename(self):
        assert _filename_matches_known_publication("Weekly FT Roundup.pdf") is False

    def test_no_match_unrelated_title(self):
        assert _filename_matches_known_publication("Laura Santini - Umami.epub") is False

    def test_short_name_does_not_match_superstring_word(self):
        assert _filename_matches_known_publication("draft.pdf") is False

    def test_config_supplied_extra_name_matches(self):
        assert _filename_matches_known_publication("Frankie Issue 108.pdf", extra_names=frozenset({"Frankie"})) is True

    def test_config_supplied_extra_name_not_matched_without_being_passed(self):
        assert _filename_matches_known_publication("Frankie Issue 108.pdf") is False

    def test_built_in_list_excludes_common_word_time(self):
        # "Time" is deliberately excluded from the built-in list (see spec) to
        # avoid false-positiving on unrelated titles starting with the word.
        assert _filename_matches_known_publication("Time Management for Busy People.epub") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lang_filter.py::TestFilenameMatchesKnownPublication -v`
Expected: FAIL with `ImportError: cannot import name '_filename_matches_known_publication'` (function doesn't exist yet).

- [ ] **Step 3: Implement the predicate**

In `lang_filter.py`, add after the `_COMPACT_LONGDATE_RE` block from Task 1:

```python
# Publications observed in this library's downloaded files that carry no date
# anywhere in the filename (e.g. "FT US.pdf", "NatGeo.pdf") -- there is no date
# signal to key off for these, so filename identity is the only option. Common
# single dictionary-word names (e.g. "Time") are deliberately excluded: they'd
# false-positive on unrelated titles that happen to start with the same word,
# and in practice such files are already caught by the compact-date regex
# above (e.g. "Time_2601.pdf" -> "2601" validates as a DDMM date).
_KNOWN_PUBLICATION_NAMES = frozenset({
    "ft", "financial times", "nyt", "new york times", "wapo", "washington post",
    "national geographic", "new scientist", "the guardian", "the economist",
    "wsj", "wall street journal", "vogue", "the week", "the new yorker",
    "happiful", "the simple things", "usa today", "newsweek", "new york post",
    "the independent", "toronto star", "der spiegel", "le monde",
    "corriere della sera", "el pais",
})

_PUBLICATION_EXT_RE = re.compile(r"\.(pdf|epub)$", re.IGNORECASE)
_PUBLICATION_SEP_RE = re.compile(r"[_\-]+")
_PUBLICATION_WS_RE = re.compile(r"\s+")


def _normalize_for_publication_match(filename: str) -> str:
    stem = _PUBLICATION_EXT_RE.sub("", filename)
    stem = _PUBLICATION_SEP_RE.sub(" ", stem)
    return _PUBLICATION_WS_RE.sub(" ", stem).strip().lower()


def _filename_matches_known_publication(filename: str, extra_names: frozenset[str] = frozenset()) -> bool:
    """Return True if filename starts with a known publication name.

    Matches only at the start of the normalized filename (lowercased,
    extension stripped, separators collapsed to single spaces), followed by
    either end-of-string or a space -- never mid-word and never elsewhere in
    the filename. extra_names (from config.yaml's filters.newspaper_names) is
    checked in addition to the built-in list, never in place of it.
    """
    normalized = _normalize_for_publication_match(filename)
    names = _KNOWN_PUBLICATION_NAMES | {n.lower() for n in extra_names}
    return any(normalized == name or normalized.startswith(name + " ") for name in names)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lang_filter.py -v`
Expected: PASS — all tests, including the 12 new ones. Case-insensitivity itself is exercised by `test_separator_insensitive_match` (`"National_Geographic_USA.pdf"`, mixed case); `test_no_boundary_no_match` covers a different risk — `"nationalgeo.pdf"` normalizes to `"nationalgeo"`, which does not equal `"national geographic"` and does not start with `"national geographic "` (no separator between the words at all), so it correctly returns `False`.

- [ ] **Step 5: Commit**

```bash
git add lang_filter.py tests/test_lang_filter.py
git commit -m "feat: add known-publication-name filename matcher for newspaper detection"
```

---

### Task 3: Wire `newspaper_names` through `_looks_like_newspaper`, `detect_newspaper`, `analyze_file`

**Files:**
- Modify: `lang_filter.py`
- Test: `tests/test_lang_filter.py`

**Interfaces:**
- Consumes: `_filename_matches_known_publication(filename, extra_names)` from Task 2.
- Produces: `_looks_like_newspaper(filename, pages, newspaper_names=frozenset())`, `detect_newspaper(file_path, ext, newspaper_names=frozenset())`, `analyze_file(..., newspaper_names=frozenset())` — consumed by Task 4 (config) and Task 5 (downloader/listener/main plumbing).

- [ ] **Step 1: Write the failing tests**

Append to `TestLooksLikeNewspaper` in `tests/test_lang_filter.py`:

```python
    def test_known_publication_name_with_no_date_detected(self):
        assert _looks_like_newspaper("FT US.pdf", []) is True

    def test_unrelated_date_free_title_not_detected(self):
        assert _looks_like_newspaper("Laura Santini - Umami.epub", []) is False

    def test_config_extra_name_detected_when_passed(self):
        assert _looks_like_newspaper("Frankie Issue 108.pdf", [], frozenset({"Frankie"})) is True

    def test_config_extra_name_not_detected_when_not_passed(self):
        assert _looks_like_newspaper("Frankie Issue 108.pdf", []) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lang_filter.py -k "known_publication_name_with_no_date or config_extra_name" -v`
Expected: FAIL — `test_known_publication_name_with_no_date_detected` and both `config_extra_name` tests fail (`_looks_like_newspaper` doesn't accept a third positional argument yet and doesn't consult the publication-name predicate). `test_unrelated_date_free_title_not_detected` already passes — expected, it's a regression guard.

- [ ] **Step 3: Update `_looks_like_newspaper`'s signature and body**

In `lang_filter.py`, replace the current `_looks_like_newspaper` function:

```python
def _looks_like_newspaper(filename: str, pages: list[str]) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Two independent signals, either one triggers a match:
      1. Filename carries an explicit date, either numeric (locale-agnostic)
         or spelled-out month name (EN/ES/FR/IT/DE -- the languages seen in
         this library's newspaper channels).
      2. A dateline-shaped token (numeric or month-name) repeats across at
         least half of the sampled pages/chapters -- a running masthead/footer
         date, which books rarely do but daily papers do by construction.
         Below _NEWSPAPER_MIN_PAGES samples the ratio is too noisy to trust,
         so it's skipped.
    """
    if (
        _FILENAME_DATE_RE.search(filename)
        or _MONTH_NAME_DATE_RE.search(filename)
        or _DATELINE_RE.search(filename)
        or _COMPACT_DATE_RE.search(filename)
        or _COMPACT_LONGDATE_RE.search(filename)
    ):
        return True
    if len(pages) < _NEWSPAPER_MIN_PAGES:
        return False
    hits = sum(
        1 for p in pages
        if _DATELINE_RE.search(p) or _MONTH_NAME_DATE_RE.search(p)
    )
    return (hits / len(pages)) >= _NEWSPAPER_PAGE_RATIO
```

with:

```python
def _looks_like_newspaper(
    filename: str, pages: list[str], newspaper_names: frozenset[str] = frozenset()
) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Three independent signals, any one triggers a match:
      1. Filename carries an explicit date -- numeric (locale-agnostic),
         spelled-out month name (EN/ES/FR/IT/DE), or compact/separator-less
         (e.g. "1602", "20240413").
      2. Filename starts with a known publication name (built-in list plus
         config.yaml's filters.newspaper_names) -- for filenames that carry
         no date at all (e.g. "FT US.pdf").
      3. A dateline-shaped token (numeric or month-name) repeats across at
         least half of the sampled pages/chapters -- a running masthead/footer
         date, which books rarely do but daily papers do by construction.
         Below _NEWSPAPER_MIN_PAGES samples the ratio is too noisy to trust,
         so it's skipped.
    """
    if (
        _FILENAME_DATE_RE.search(filename)
        or _MONTH_NAME_DATE_RE.search(filename)
        or _DATELINE_RE.search(filename)
        or _COMPACT_DATE_RE.search(filename)
        or _COMPACT_LONGDATE_RE.search(filename)
        or _filename_matches_known_publication(filename, newspaper_names)
    ):
        return True
    if len(pages) < _NEWSPAPER_MIN_PAGES:
        return False
    hits = sum(
        1 for p in pages
        if _DATELINE_RE.search(p) or _MONTH_NAME_DATE_RE.search(p)
    )
    return (hits / len(pages)) >= _NEWSPAPER_PAGE_RATIO
```

- [ ] **Step 4: Update `detect_newspaper`'s signature and body**

Replace:

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

with:

```python
def detect_newspaper(
    file_path: Path, ext: str, newspaper_names: frozenset[str] = frozenset()
) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Used by the retroactive `scan-newspapers` command, which calls this
    directly per file rather than through analyze_file's combined
    language+topic path.
    """
    if ext not in ("pdf", "epub"):
        return False
    _, _, _, pages = _extract_text_parts(file_path, ext)
    return _looks_like_newspaper(file_path.name, pages, newspaper_names)
```

- [ ] **Step 5: Update `analyze_file`'s signature and body**

Replace the signature:

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
```

with:

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
    newspaper_names: frozenset[str] = frozenset(),
) -> tuple[str | None, str | None, bool]:
```

And replace the newspaper-detection block near the end of the function body:

```python
    is_newspaper = False
    if discard_newspapers and ext in ("pdf", "epub"):
        is_newspaper = _looks_like_newspaper(file_path.name, pages)
        if is_newspaper:
            log.debug(f"{file_path.name}: detected as newspaper/periodical")
```

with:

```python
    is_newspaper = False
    if discard_newspapers and ext in ("pdf", "epub"):
        is_newspaper = _looks_like_newspaper(file_path.name, pages, newspaper_names)
        if is_newspaper:
            log.debug(f"{file_path.name}: detected as newspaper/periodical")
```

- [ ] **Step 6: Update the module docstring**

Replace the "Newspaper/periodical detection" paragraph at the top of `lang_filter.py`:

```python
Newspaper/periodical detection:
  _looks_like_newspaper() flags files via an explicit date in the filename, or
  a dateline-shaped token repeated across at least half of the sampled
  pages/chapters (a running masthead date, unlike ordinary books). Dates are
  recognized both numerically (2026-07-06) and as spelled-out month names in
  EN/ES/FR/IT/DE (16 Gennaio 2026, 7 de julio de 2026, JULY 4TH-10TH 2026),
  since real-world papers date themselves either way. This is a format
  signal, not a subject-matter one -- unlike discard_topics, it uses no
  vocabulary keywords, since a newspaper can be about any topic. Opt-in via
  filters.discard_newspapers in config.yaml; independent of language/topic
  filtering (analyze_file() runs all three and returns a 3-tuple).
```

with:

```python
Newspaper/periodical detection:
  _looks_like_newspaper() flags files via an explicit date in the filename
  (numeric, spelled-out month name in EN/ES/FR/IT/DE, or compact/separator-
  less like "1602"/"20240413"), a known-publication-name filename match (a
  built-in list plus config.yaml's filters.newspaper_names, for filenames
  with no date at all -- e.g. "FT US.pdf"), or a dateline-shaped token
  repeated across at least half of the sampled pages/chapters (a running
  masthead date, unlike ordinary books). The publication-name signal is the
  one exception to "no vocabulary keywords" below: it matches publication
  identity, not subject matter, so it doesn't undermine the topic-agnostic
  design. Opt-in via filters.discard_newspapers in config.yaml; independent
  of language/topic filtering (analyze_file() runs all three and returns a
  3-tuple).
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_lang_filter.py -v`
Expected: PASS — full suite green, including the 4 new tests from Step 1.

Run: `uv run pytest -q`
Expected: PASS — full project test suite green (confirms no other module imports broke from the signature changes).

- [ ] **Step 8: Commit**

```bash
git add lang_filter.py tests/test_lang_filter.py
git commit -m "feat: wire newspaper_names through analyze_file/detect_newspaper/_looks_like_newspaper"
```

---

### Task 4: Config wiring

**Files:**
- Modify: `config.py`
- Modify: `config.yaml.example`

**Interfaces:**
- Consumes: nothing new (pure config-loading change).
- Produces: `config["filters"]["newspaper_names"]` (list of str, default `[]`) — consumed by Task 5's `main.py`/`listener.py` reads.

- [ ] **Step 1: Add the config default**

In `config.py`, in `_apply_defaults`, change:

```python
    filters.setdefault("discard_newspapers", False)
```

to:

```python
    filters.setdefault("discard_newspapers", False)
    filters.setdefault("newspaper_names", [])
```

- [ ] **Step 2: Document the config key in `config.yaml.example`**

In `config.yaml.example`, change:

```yaml
  # Optional: auto-discard files that look like newspapers/periodicals rather
  # than books. Detected via an explicit date in the filename, or a dateline
  # repeated across at least half the sampled pages/chapters (a running
  # masthead date -- something ordinary books rarely do). Only PDF and EPUB
  # are scanned. Run `tgdctl scan-newspapers` to apply retroactively.
  # discard_newspapers: true
```

to:

```yaml
  # Optional: auto-discard files that look like newspapers/periodicals rather
  # than books. Detected via an explicit date in the filename (numeric,
  # spelled-out month name, or compact like "1602"), a known publication name
  # at the start of the filename, or a dateline repeated across at least half
  # the sampled pages/chapters (a running masthead date -- something ordinary
  # books rarely do). Only PDF and EPUB are scanned. Run `tgdctl
  # scan-newspapers` to apply retroactively.
  # discard_newspapers: true
  #
  # Additional publication names to match at the start of a filename, on top
  # of the built-in list (FT, NYT, The Guardian, National Geographic, etc.).
  # Use this for channel-specific publications the built-in list can't guess.
  # Avoid common dictionary words here (e.g. "Time") -- they risk matching
  # unrelated book titles that happen to start with the same word.
  # newspaper_names:
  #   - Frankie
  #   - Puzzle Life
  #   - AirForces Monthly
```

- [ ] **Step 3: Verify manually**

Run: `uv run python -c "from config import load_config; import json; c = load_config('config.yaml'); print(c['filters']['newspaper_names'])"`
Expected output: `[]` (assuming `config.yaml` doesn't already define the key — if it does, prints whatever list is configured there).

- [ ] **Step 4: Commit**

```bash
git add config.py config.yaml.example
git commit -m "feat: add filters.newspaper_names config key"
```

---

### Task 5: Thread `newspaper_names` through `downloader.py`, `listener.py`, `main.py`

**Files:**
- Modify: `downloader.py`
- Modify: `listener.py`
- Modify: `main.py`

**Interfaces:**
- Consumes: `analyze_file(..., newspaper_names=...)` and `detect_newspaper(..., newspaper_names=...)` from Task 3; `config["filters"]["newspaper_names"]` from Task 4.
- Produces: nothing further downstream — this is the final plumbing task.

- [ ] **Step 1: Update `download_item` in `downloader.py`**

Replace the full contents of `downloader.py` with:

```python
import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from db import Database
from lang_filter import DISCARD_LANG, analyze_file
from utils import compute_sha256, human_size, unique_path

log = logging.getLogger(__name__)


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
    newspaper_names: frozenset[str] = frozenset(),
) -> bool:
    """Download one media item to dest. Returns True on success.

    item must contain: id, message_id, filename, and either channel_identifier
    or channel_telegram_id for entity resolution.

    If message is provided (a live Telethon message object), it is used directly
    and no Telegram fetch is needed.

    The file is indexed via FTS5 after a successful download in a background asyncio task.
    """
    async with semaphore:
        filepath = unique_path(dest / item["filename"])
        label = item.get("channel_title") or item.get("channel_identifier") or str(item.get("channel_telegram_id", "?"))
        try:
            if message is None:
                identifier = item.get("channel_identifier") or item["channel_telegram_id"]
                entity = await client.get_entity(identifier)
                message = await client.get_messages(entity, ids=item["message_id"])
                if message is None:
                    db.mark_discarded(item["id"])
                    log.warning(
                        f"[{label}] Message {item['message_id']} not found on Telegram "
                        f"(deleted?) -- {item['filename']!r} marked discarded"
                    )
                    return True

            await client.download_media(message, file=str(filepath))

            ext = item.get("ext") or ""

            lang, topic, is_newspaper = analyze_file(
                filepath, ext, topic_keywords, topic_min_matches, topic_min_occurrences,
                discard_newspapers=discard_newspapers,
                newspaper_names=newspaper_names,
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

            file_hash = None
            try:
                file_hash = await asyncio.to_thread(compute_sha256, filepath)
            except Exception as exc:
                log.warning(f"[{label}] Hash failed for {item['filename']!r}: {exc}")

            db.mark_downloaded(item["id"], str(filepath), language=lang, file_hash=file_hash)
            size_str = human_size(filepath.stat().st_size) if filepath.exists() else "?"
            lang_tag = f" [{lang}]" if lang else ""
            log.info(f"[{label}] Downloaded: {item['filename']}  ({size_str}){lang_tag}")

            asyncio.create_task(_index_async(
                db,
                item["id"],
                str(filepath),
                item.get("ext", ""),
                item.get("filename", filepath.name),
                item.get("channel_identifier", ""),
            ))

            return True
        except Exception as exc:
            log.error(f"[{label}] Failed to download {item['filename']!r}: {exc}")
            return False


async def _index_async(
    db: Database, media_id: int, filepath: str, ext: str, filename: str, channel_identifier: str = ""
) -> None:
    """Index a downloaded file via FTS5 in the background. Errors are logged, not raised."""
    from search.indexer import index_file

    try:
        await asyncio.to_thread(index_file, db, media_id, filepath, ext, filename, channel_identifier)
    except Exception as exc:
        log.warning(f"FTS5 indexing failed for {filename!r}: {exc}")
```

- [ ] **Step 2: Update `listener.py`**

Replace the full contents of `listener.py` with:

```python
import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel, PeerChat

from db import Database
from downloader import download_item

log = logging.getLogger(__name__)


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
    newspaper_names = frozenset(config["filters"].get("newspaper_names") or [])
    destination.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrent_downloads)

    await _flush_pending(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names)
    await _heal_missing(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names)
    await _backfill_missed(client, db, allowed, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names)

    asyncio.create_task(_heal_search_index(db))
    asyncio.create_task(_cleanup_loop(db, retention_days))
    asyncio.create_task(_heartbeat_loop(db))
    asyncio.create_task(_backfill_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names,
    ))
    asyncio.create_task(_deep_reconcile_loop(
        client, db, allowed, destination, semaphore,
        topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names,
    ))

    channels = db.list_channels()
    log.info(f"Listening -- {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  . {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed, client, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    # Catch up on anything that arrived while we were offline / mid-reconnect.
    # Must run after the handler above is registered so loaded updates are processed.
    await client.catch_up()

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()


async def _flush_pending(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences,
    discard_newspapers, newspaper_names
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
                        discard_newspapers=discard_newspapers,
                        newspaper_names=newspaper_names)
          for item in pending],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Flush complete: {ok}/{len(pending)} succeeded")


async def _heal_missing(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences,
    discard_newspapers, newspaper_names
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
                        discard_newspapers=discard_newspapers,
                        newspaper_names=newspaper_names)
          for item in missing],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Heal complete: {ok}/{len(missing)} restored")


async def _backfill_missed(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches,
    topic_min_occurrences, discard_newspapers, newspaper_names, warn_empty: bool = True
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
                    newspaper_names=newspaper_names,
                ))

        if tasks:
            log.info(f"Backfilling {len(tasks)} missed item(s) from {ch['title']}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Backfill {ch['title']}: {ok}/{len(tasks)} succeeded")


async def _heal_search_index(db: Database) -> None:
    missing = db.search_fts_missing_media_ids()
    if not missing:
        return
    log.info(f"Search index heal: {len(missing)} file(s) not yet indexed, indexing in background...")
    from search.indexer import index_file
    indexed = 0
    textless = 0
    errors = 0
    for item in missing:
        try:
            result = await asyncio.to_thread(
                index_file, db,
                item["media_id"], item["local_path"], item["ext"],
                item["filename"], item.get("channel_identifier", ""),
            )
            # True = chunks stored; False = no extractable text (image-only PDF),
            # already marked processed by index_file so it won't be retried.
            if result:
                indexed += 1
            else:
                textless += 1
        except Exception as exc:
            errors += 1
            log.warning(f"Search heal failed for {item['filename']!r}: {exc}")
    # Distinguish the three outcomes: a high `textless` count is normal (scanned
    # magazines); a non-zero `errors` count is the only line worth acting on.
    log.info(
        f"Search index heal complete: {indexed} indexed, {textless} no text, "
        f"{errors} error(s) (of {len(missing)})"
    )


async def _heartbeat_loop(db: Database) -> None:
    """Log one structured operational summary line every hour. This is the surface
    an operator scans to answer 'is the listener keeping up / is anything being
    missed?' without writing SQL: download rate, queue depth, index backlog, and
    channels that have never produced a message (likely not joined / wrong id)."""
    while True:
        await asyncio.sleep(3600)
        try:
            s = db.health_snapshot()
            log.info(
                "Heartbeat: "
                f"downloaded={s['downloaded']} (+{s['downloaded_last_hour']}/h) "
                f"pending={s['pending']} indexed={s['indexed']} "
                f"index_pending={s['index_pending']} "
                f"discarded={s['discarded']} expired={s['expired']} "
                f"channels_no_messages={s['channels_no_messages']}"
            )
        except Exception as exc:
            log.error(f"Heartbeat error: {exc}", exc_info=True)


async def _backfill_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences,
    discard_newspapers, newspaper_names
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
                topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names,
                warn_empty=False,
            )
        except Exception as exc:
            log.error(f"Periodic backfill error: {exc}", exc_info=True)


# How many recent messages per channel the deep-reconcile pass re-examines, and
# how often. The hourly backfill only fetches ids *newer* than the highest one
# recorded, so a file dropped mid-burst (while a higher id from the same burst
# landed) is permanently below that watermark. This pass re-walks a fixed recent
# window with no watermark and lets save_media_message's dedup insert only the
# genuinely-missing ids -- closing that hole for recent drops without the cost of
# scanning full history. Older holes are recovered manually with `scrape`.
RECONCILE_WINDOW = 400
RECONCILE_INTERVAL_SECONDS = 86400  # daily


async def _deep_reconcile_loop(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences,
    discard_newspapers, newspaper_names
) -> None:
    """Once a day, re-scan each channel's recent window ignoring the backfill
    watermark, recovering media that real-time delivery dropped mid-burst."""
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            await _deep_reconcile(
                client, db, allowed, dest, semaphore,
                topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names,
            )
        except Exception as exc:
            log.error(f"Deep reconcile error: {exc}", exc_info=True)


async def _deep_reconcile(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences,
    discard_newspapers, newspaper_names
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
                    newspaper_names=newspaper_names,
                ))

        if tasks:
            log.info(f"Deep reconcile: recovered {len(tasks)} mid-burst miss(es) from {ch['title']}")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Deep reconcile {ch['title']}: {ok}/{len(tasks)} downloaded")


async def _cleanup_loop(db: Database, retention_days: int) -> None:
    """Run retention cleanup once on startup, then every hour. No-op if retention_days <= 0."""
    if retention_days <= 0:
        return
    while True:
        try:
            _run_cleanup(db, retention_days)
        except Exception as exc:
            log.error(f"Cleanup error: {exc}", exc_info=True)
        await asyncio.sleep(3600)


def _run_cleanup(db: Database, retention_days: int) -> None:
    expired = db.get_expired_files(retention_days)
    if not expired:
        return
    deleted = 0
    for item in expired:
        if item.get("local_path"):
            p = Path(item["local_path"])
            if p.exists():
                p.unlink()
                deleted += 1
        db.mark_expired(item["id"])
    log.info(
        f"Retention cleanup: {deleted} file(s) deleted, "
        f"{len(expired)} record(s) marked expired (>{retention_days}d)"
    )


async def _handle(
    event, db, allowed, client, dest, semaphore,
    topic_keywords, topic_min_matches, topic_min_occurrences, discard_newspapers, newspaper_names
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
            newspaper_names=newspaper_names,
        ))


def _extract_media(message) -> dict | None:
    if message.document:
        f = message.file
        ext = (f.ext or "").lstrip(".").lower()
        return {
            "filename": f.name or f"document_{message.id}{f.ext or ''}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "application/octet-stream",
            "ext": ext,
        }
    if message.photo:
        f = message.file
        ext = (f.ext or ".jpg").lstrip(".").lower()
        return {
            "filename": f"photo_{message.id}.{ext}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "image/jpeg",
            "ext": ext,
        }
    return None
```

- [ ] **Step 3: Update `cmd_scan_newspapers` in `main.py`**

In `main.py`, change the dispatch call (currently at line 130-132):

```python
    if args.command == "scan-newspapers":
        cmd_scan_newspapers(db)
        return
```

to:

```python
    if args.command == "scan-newspapers":
        cmd_scan_newspapers(db, config)
        return
```

Then replace the `cmd_scan_newspapers` function (currently at line 425):

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
```

with:

```python
def cmd_scan_newspapers(db: Database, config: dict) -> None:
    from lang_filter import detect_newspaper

    newspaper_names = frozenset(config["filters"].get("newspaper_names") or [])

    items = db.get_downloaded_media()
    if not items:
        console.print("[yellow]No downloaded files found.[/yellow]")
        return

    console.print(f"[dim]Scanning {len(items)} file(s) for newspaper/periodical pattern…[/dim]")

    discarded = 0

    def handle(item, path):
        nonlocal discarded
        ext = (item.get("ext") or "").lower()
        if detect_newspaper(path, ext, newspaper_names):
            path.unlink(missing_ok=True)
            db.mark_discarded(item["id"])
            discarded += 1
            log.info(f"Scan-discarded (newspaper): {item['filename']}")

    missing = _run_file_batch(items, "Detecting newspapers", handle)
```

(The remainder of `cmd_scan_newspapers` after the `missing = _run_file_batch(...)` line is unchanged — leave it as-is.)

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS — all tests green, confirming the signature changes across `downloader.py`, `listener.py`, and `main.py` didn't break any import or call site.

- [ ] **Step 5: Verify manually**

Run: `uv run python -c "import ast; ast.parse(open('listener.py').read()); ast.parse(open('downloader.py').read()); ast.parse(open('main.py').read()); print('syntax OK')"`
Expected output: `syntax OK`

Run: `uv run python main.py scan-newspapers`
Expected: the command runs against `data/tg_downloader.db` (scans downloaded files, reports newly-discarded newspaper/periodical files using the two new signals) without raising a `TypeError` on the new `newspaper_names` parameter. Compare the discarded count to the ~70 baseline mentioned in the spec — it should be substantially higher given the ~2,736 compact-date files and ~733 name-only files now covered.

- [ ] **Step 6: Commit**

```bash
git add downloader.py listener.py main.py
git commit -m "feat: thread newspaper_names through downloader/listener/main"
```

---

### Task 6: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing — documentation-only task.
- Produces: nothing — end of plan.

- [ ] **Step 1: Update the "Newspaper/periodical detection" section**

In `CLAUDE.md`, find the paragraph (under "Content filters (`lang_filter.py`)"):

```
**Newspaper/periodical detection** — opt-in via `filters.discard_newspapers: true` (default `false`). Two independent signals, either one triggers a discard:

1. **Filename date** — an explicit numeric date in the filename (`YYYY-MM-DD`, `DD.MM.YYYY`, `DD_MM_YYYY`, etc.), locale-agnostic (no month names).
2. **Dateline repetition** — a date-shaped token appears on at least half of the sampled pages (PDF) or content files (EPUB), when at least 4 are sampled. Ordinary books rarely repeat a date on most pages; a daily paper's running masthead/footer date does this by construction.

Detection is a format signal, not a subject-matter one — unlike `discard_topics`, it doesn't use vocabulary keywords, since newspapers can cover any topic. Implemented in `lang_filter._looks_like_newspaper()` / `detect_newspaper()`. Use `tgdctl scan-newspapers` to apply retroactively to already-downloaded files.
```

Replace it with:

```
**Newspaper/periodical detection** — opt-in via `filters.discard_newspapers: true` (default `false`). Three independent signals, any one triggers a discard:

1. **Filename date** — an explicit date in the filename: numeric with separators (`YYYY-MM-DD`, `DD.MM.YYYY`, `DD_MM_YYYY`), spelled-out month names in EN/ES/FR/IT/DE (`16 Gennaio 2026`, `7 de julio de 2026`, `JULY 4TH-10TH 2026`), or compact/separator-less (`1602`, `20240413`) — the last validated so only digit runs plausible as a day+month pair in either order match (a bare year like `2026` fails validation and is never mistaken for a date).
2. **Known publication name** — the filename starts with a recognized publication name (a built-in list of major outlets — FT, NYT, The Guardian, National Geographic, etc. — plus any names added to `filters.newspaper_names` in `config.yaml`, which is purely additive). This catches filenames with no date at all (e.g. `FT US.pdf`, `NatGeo.pdf`). Matching is anchored at the start of the filename and requires a word boundary, so it won't match a publication name embedded mid-title or as part of an unrelated word.
3. **Dateline repetition** — a date-shaped token (numeric or month-name) appears on at least half of the sampled pages (PDF) or content files (EPUB), when at least 4 are sampled. Ordinary books rarely repeat a date on most pages; a daily paper's running masthead/footer date does this by construction.

Signals 1 and 3 are pure format signals; signal 2 matches publication identity rather than subject matter, so — like the others — it doesn't undermine the "no vocabulary keywords" property that distinguishes this filter from `discard_topics` (newspapers can cover any topic). Implemented in `lang_filter._looks_like_newspaper()` / `detect_newspaper()`. Use `tgdctl scan-newspapers` to apply retroactively to already-downloaded files. Design rationale (including the data-driven analysis of the original detection gap) in `docs/superpowers/specs/2026-07-08-newspaper-name-list-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for compact-date and known-publication-name newspaper detection"
```
