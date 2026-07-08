# Newspaper Detection: Compact Dates + Known-Publication-Name List

**Status:** Approved design, not yet implemented.

## Problem

`tgdctl scan-newspapers` was run against the existing library (4,035 downloaded PDF/EPUB files) and flagged only ~70. Manual inspection showed many obvious newspapers/magazines still present and undiscarded. Measuring the current filename-date signal (`_FILENAME_DATE_RE`, `_MONTH_NAME_DATE_RE`) against all 4,035 downloaded filenames found:

| Bucket | Count | % of total |
|---|---|---|
| Matched by current filename signal | 566 | 14% |
| **Not matched** — compact numeric date, no separators (`NYT 1602.pdf`, `NY Daily News_1204.pdf`) | 2,706 | 67% |
| Not matched — 2-digit-year dotted date (`FT How to Spend it 7.3.26.pdf`) | 30 | 1% |
| Not matched — no date-like token at all (`FT US.pdf`, `NatGeo.pdf`, `MWeek.pdf`) | 733 | 18% |

The 733 date-free files are dominated by publication-brand-only filenames with no date anywhere: `FT EU`/`FT US`/`FT UK`/`FT Magazine`/`FT How to Spend It` alone account for ~280 of them. The remaining 2,736 are almost entirely a single format gap: dates present but written without separators or with a 2-digit year, which the existing regex requires.

Two independent, additive detection gaps, closed by two independent, additive signals.

## Non-goals

- No change to the dateline-repetition (page-content) signal, PDF/EPUB text extraction, or the `discard_newspapers` config toggle's existing behavior.
- No new DB columns or schema changes — this only adds detection signals feeding the existing `db.mark_discarded()` path.
- No attempt at exhaustive worldwide publication-name coverage — the built-in list is seeded from what's actually observed in this library's downloaded files, not a generic "top N newspapers" list.

## Design

### 1. Compact/compressed date formats (closes the 2,736-file gap)

Three new regex additions, OR'd alongside the existing `_FILENAME_DATE_RE`/`_MONTH_NAME_DATE_RE` checks in `_looks_like_newspaper()`. All use the same `(?<!\d)...(?!\d)` digit-boundary guards already used elsewhere in the module, so they never match inside a longer digit run.

- **`_COMPACT_DATE_RE`** — a bare 4-digit run, range-validated so it only matches digit pairs plausible as day+month in *either* order: `day(01–31) + month(01–12)` OR `month(01–12) + day(01–31)`. This is what rejects bare years: `2026` fails both interpretations (`20` is not a valid month), so a book title ending in a year does not false-positive.
- **`_COMPACT_LONGDATE_RE`** — an 8-digit run shaped as `YYYYMMDD`: a `19xx`/`20xx` year immediately followed by a valid month and day, no separators (`WAPO_20240413.pdf`).
- **2-digit-year dotted dates** — loosen the existing separator-based filename regex to also accept a 2-digit year (`7.3.26`), matching what `_DATELINE_RE` (used for page-content sampling) already allows for dotted dates.

**Validated against false positives:** the range-validated DDMM/MMDD regex was run against all 123 currently-downloaded EPUB filenames (non-periodical books, in principle) — zero matches. The range check is narrow by construction (needs the first two digits ≤31 *and* the second two ≤12, or vice versa), so it excludes years entirely and only catches genuinely date-shaped 4-digit runs.

### 2. Known-publication-name filename matching (closes the 733-file gap)

**New predicate:** `_filename_matches_known_publication(filename: str, extra_names: frozenset[str] = frozenset()) -> bool`

**Matching mechanic:**
1. Normalize the filename: lowercase, strip the extension, collapse `_`/`-`/whitespace runs to single spaces.
2. Normalize each candidate name (built-in list + `extra_names`) the same way.
3. Match only if the normalized filename **starts with** a candidate name followed by either end-of-string or a separator (space) — never mid-word and never as a substring elsewhere in the filename.

This anchoring mirrors the real-world convention observed in the data (`"<Publication> - <date/issue>.ext"`) and specifically avoids false-positiving on a book whose title merely mentions a publication's name (e.g. `"The Guardian Angel's Secret.epub"` does not match `"the guardian"`, because `"angel's"` follows `"guardian"` without a name-boundary-then-separator... concretely: normalized filename `"the guardian angel's secret"` does start with `"the guardian"` followed by a space — this specific example **would** still match. This is a known residual risk of prefix-anchored matching and is accepted as a tradeoff: the alternative (requiring the trailing token look date/issue-shaped) was considered and explicitly deferred — see Alternatives Considered.

**Built-in default list** (`_KNOWN_PUBLICATION_NAMES` constant in `lang_filter.py`, always active when `discard_newspapers: true`):

```
FT, Financial Times, NYT, New York Times, WAPO, Washington Post,
National Geographic, New Scientist, The Guardian, The Economist,
WSJ, Wall Street Journal, Vogue, The Week, The New Yorker, Happiful,
The Simple Things, USA Today, Newsweek, New York Post, The Independent,
Toronto Star, Der Spiegel, Le Monde, Corriere della Sera, El Pais
```

**Deliberate exclusion:** common single dictionary-word names (e.g. **"Time"**) are left out of the built-in default even though `Time_2601.pdf` appears in the library, because:
- It's already caught by the compact-date regex (`2601` validates as a DDMM date), so no detection is lost.
- Left in the name list, it would false-positive on any legitimate book whose title starts with the word "Time" (e.g. `"Time Management for Busy People.epub"`).

This same reasoning applies to any future addition to the config-supplied list: names that double as common English words carry false-positive risk and should be added deliberately, not by default.

**Config wiring:** new `filters.newspaper_names: []` key in `config.yaml`, purely additive to the built-in list — for niche channel-specific publication names the built-in list has no way to guess (e.g. `Frankie`, `Puzzle Life`, `AirForces Monthly`, `Grazia`). Empty by default; absence changes nothing beyond the built-in list already being active.

### 3. Plumbing

`_looks_like_newspaper(filename, pages, extra_names=frozenset())` gains a third parameter, passed straight through to `_filename_matches_known_publication`, which internally unions it with the built-in `_KNOWN_PUBLICATION_NAMES` constant — merging happens inside `lang_filter.py`, not in `config.py`. `detect_newspaper()` and `analyze_file()` thread `extra_names` through exactly the way `discard_newspapers` already threads today, reusing the same call chain:

```
config.yaml (filters.newspaper_names, loaded as-is, no merging)
  -> config.py (Config.newspaper_names: list[str], default [])
  -> main.py (scan-newspapers command + live download path)
  -> downloader.download_item
  -> listener.py call sites
```

No changes to `db.py` or `tgdctl.py` — no new DB columns, and the retroactive `scan-newspapers` command already exists and calls into the same `detect_newspaper()` entry point.

## Testing plan

All tests live in `tests/test_lang_filter.py`, following the file's existing convention: no real PDF/EPUB fixtures, pure-Python logic only.

**Compact-date regex:**
- Valid DDMM/MMDD 4-digit runs are accepted (both digit-pair orderings).
- Bare years (`2026`, `1984`) are rejected.
- Valid `YYYYMMDD` 8-digit runs are accepted.
- 2-digit-year dotted dates (`7.3.26`) are accepted where they previously were not.

**Known-publication-name matching:**
- Exact match (`"ft.pdf"` matches `"FT"`).
- Match with trailing suffix (`"ft eu.pdf"`, `"ft how to spend it.pdf"`).
- Case-insensitivity and separator-insensitivity (`_`/`-`/space all equivalent).
- No match when the name appears but not at the start of the filename.
- No match for an unrelated book title with no built-in-list overlap.
- Config-supplied `extra_names` entry matches when passed explicitly.
- A short built-in name does not match as a substring of a longer unrelated word (`"ft"` does not match `"draft.pdf"`).

**Integration (`_looks_like_newspaper`):**
- A date-free known-publication filename (e.g. `"FT US.pdf"`) now returns `True`.
- An unrelated, date-free, non-listed book title still returns `False`.

## Alternatives considered

- **Stricter anchoring** (name match at start AND the remainder of the filename must look like a date/issue/volume tag, not further prose) — would eliminate the `"The Guardian Angel's Secret"`-style residual risk entirely, but adds real implementation complexity (defining what counts as a "date/issue-shaped" remainder) for a risk that, in practice, no file in the current 4,035-file library actually triggers. Deferred; revisit if a real false positive is observed after rollout.
- **config.yaml as the sole source of publication names** (empty by default, exactly mirroring `discard_topics`) — rejected because it would require the user to manually populate ~25 names before seeing any benefit against an already-downloaded library where the gap is known and measured.
