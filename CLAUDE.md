# CLAUDE.md

You are a human software engineer. Assume all code will be written and maintained by humans. Optimize for reasoning, regeneration, and debugging — with an eye on human readability.

Your goal: produce code that is predictable, debuggable, and easy for future LLMs to rewrite or extend.

## Workflow

- Work in discrete steps. Break complex tasks into smaller subtasks and complete them one at a time.
- Use `mcp__context7` or equivalent documentation tools to read relevant docs for any language, framework, or library before writing code. Never assume your training knowledge is current — always verify.
- Check your work before returning control to the user. Run tests if available, verify builds, lint. Never return incomplete or unverified work.
- Each time you complete a task or learn important project information, update this `CLAUDE.md` file to reflect new knowledge or required changes.

## Mandatory Coding Principles

1. **Structure**
   - Use a consistent, predictable project layout.
   - Group code by feature/screen; keep shared utilities minimal.
   - Create simple, obvious entry points.
   - Before scaffolding multiple files, identify shared structure first. Use framework-native composition patterns (layouts, base templates, providers, shared components) for elements that appear across pages. Duplication that requires the same fix in multiple places is a code smell, not a pattern to preserve.

2. **Architecture**
   - Prefer flat, explicit code over abstractions or deep hierarchies.
   - Avoid clever patterns, metaprogramming, and unnecessary indirection.
   - Minimize coupling so files can be safely regenerated.

3. **Functions and Modules**
   - Keep control flow linear and simple.
   - Use small-to-medium functions; avoid deeply nested logic.
   - Pass state explicitly; avoid globals.

4. **Naming and Comments**
   - Use descriptive-but-simple names.
   - Comment only to note invariants, assumptions, or external requirements.

5. **Logging and Errors**
   - Emit detailed, structured logs at key boundaries.
   - Make errors explicit and informative.

6. **Regenerability**
   - Write code so any file/module can be rewritten from scratch without breaking the system.
   - Prefer clear, declarative configuration (JSON/YAML/etc.).

7. **Platform Use**
   - Use platform conventions directly and simply without over-abstracting.

8. **Modifications**
   - When extending/refactoring, follow existing patterns.
   - Prefer full-file rewrites over micro-edits unless told otherwise.

9. **Quality**
   - Favor deterministic, testable behavior.
   - Keep tests simple and focused on verifying observable behavior.

---

## Project: tg-downloader

CLI tool that auto-downloads media from Telegram channels as messages arrive, with configurable retention cleanup and an interactive discard tool.

### Stack
- **Python 3.11**, managed by **uv** (`pyproject.toml` + `uv.lock`)
- **Telethon** — MTProto Telegram client (user account, not bot)
- **cryptg** — C extension giving Telethon AES-NI hardware crypto (avoids pure-Python AES pegging ARM cores on the Pi). No code references it; Telethon auto-detects it at import.
- **InquirerPy** — interactive checkbox file selection in the terminal
- **rich** — tables, styled output
- **PyYAML** — config file
- **FastAPI + uvicorn** — web UI service (`webui/`)
- **PyMuPDF** — PDF cover thumbnail extraction (webui) + text extraction for language detection (main app)
- **Pillow** — image resizing for thumbnails
- **langdetect** — language detection for automatic German-content filtering
- **httpx** — async HTTP client

### Entry points
```
uv run tgdctl <command>        # host-side management (Docker + DB stats)
uv run tg-downloader <command> # app CLI (runs inside the container/venv)
uv run python main.py <command>

# app subcommands: listen | subscribe | unsubscribe | channels | discard | status | history | scrape
```

### File layout
| File | Responsibility |
|---|---|
| `main.py` | Entry point; CLI subcommands (`listen`, `subscribe`, `unsubscribe`, `channels`, `discard`, `status`, `history`, `scrape`) |
| `config.py` | Load and validate `config.yaml` |
| `db.py` | SQLite schema and all query methods (`Database` class) |
| `listener.py` | Real-time listener; auto-downloads on arrival; startup backfill + flush pending; hourly retention cleanup |
| `lang_filter.py` | Post-download language detection; auto-discards German files |
| `ui.py` | Interactive `select_discard` checkbox UI (InquirerPy) |
| `downloader.py` | `download_item` — single-file daemon-mode download via Telethon |
| `utils.py` | Pure helpers: `human_size`, `unique_path` |
| `tgdctl.py` | Host-side management CLI; wraps docker compose + proxies app commands |
| `config.yaml.example` | Template config — copy to `config.yaml` to start |
| `webui/app.py` | FastAPI web UI — file grid with cover previews, discard, download. Uses the shared `Database` class (not raw SQL); the Dockerfile copies `db.py` into the image. All SQL lives in `db.py`. |
| `webui/static/index.html` | Single-page app (vanilla JS, all inline) |
| `webui/Dockerfile` | Separate image for the web UI service |
| `search/chunker.py` | Text extraction + chunking for PDF (per-page) and EPUB (per-chapter) |
| `search/indexer.py` | FTS5 insert / delete / query / is_indexed helpers |

### Setup (Docker — recommended)
1. Copy `config.yaml.example` → `config.yaml`; fill in `api_id`, `api_hash`
2. `mkdir -p data/downloads`
3. First-time Telegram auth (interactive — phone + OTP):
   `sudo docker compose run --rm -it tg-downloader uv run python main.py listen`
   Session is saved to `data/tg_session.session` and reused on subsequent runs. Ctrl+C once authenticated.
4. `sudo docker compose up -d --build` — builds image, starts listener as main process with `restart: always`

### Usage (Docker)
Use `tgdctl` — the host-side management wrapper:

```bash
uv run tgdctl start                  # build + start the listener container
uv run tgdctl stop / restart / logs  # service control
uv run tgdctl auth                   # first-time Telegram auth (interactive)
uv run tgdctl status                 # container state + per-channel DB stats
uv run tgdctl progress               # overall progress bar + recently downloaded files
uv run tgdctl progress -w            # same, live-updating every 2 seconds (Ctrl+C to exit)

uv run tgdctl subscribe @channel     # subscribe to a channel
uv run tgdctl channels               # list subscribed channels
uv run tgdctl discard                # review downloaded files and delete unwanted ones (no listener restart needed)
uv run tgdctl history [--limit N]    # show recently downloaded files
uv run tgdctl scrape --dry-run       # AUDIT: report media on Telegram missing from the DB, per channel (writes nothing)
uv run tgdctl scrape                 # RECOVER: queue every missing file, then download it on the wrapped listener restart
uv run tgdctl unsubscribe @channel   # unsubscribe from a channel
uv run tgdctl scan-languages         # retroactively detect language for untagged files; discard German ones
uv run tgdctl scan-topics            # retroactively apply topic filters to downloaded files; discard matches
uv run tgdctl scan-newspapers        # retroactively detect newspaper/periodical-shaped files; discard matches
uv run tgdctl scan-hashes            # compute SHA-256 for all downloaded files; enables duplicate detection in web UI
uv run tgdctl index                  # index all downloaded files into the FTS5 search table
```
Downloaded files appear in `./data/downloads/` on the host.

### Web UI
A second Docker service (`webui`) runs a FastAPI app on **port 8090** of the host:

```
http://RASPBERRY_PI_IP:8090
```

Features:
- Responsive card grid with cover thumbnails (PDF first page, EPUB cover image)
- Click cards to select; Select All / Clear buttons
- Delete Selected — permanently removes files from disk and marks `discarded` in DB
- Per-card Download button — downloads the file to the browser
- Filter by channel and by language; pagination (60 per page)
- Language badge on each card (ISO code chip, color-coded by language)
- Duplicate detection: by default shows one copy per unique file (identified by SHA-256 hash, falling back to filename+size); cards show an "N×" amber badge when more copies exist. Toggle with the "Hide dupes / Show dupes" button.
- Thumbnails are cached in `data/thumbs/` and generated on first request
- **In-browser PDF reader** — click "📖 Read" on any PDF card (or a paged search hit) to open a full-screen reader instead of downloading. Mobile-first: continuous vertical scroll with lazy per-page canvas rendering (`IntersectionObserver`), fit-width zoom (buttons + pinch), `#page=N` / search-result deep links, and a manual region-crop tool to isolate a single article (works on scanned PDFs too, since it operates on rendered pixels, not text). Backend: `GET /api/pdf/{id}` serves the file inline (`Content-Disposition: inline`) with HTTP Range support via `FileResponse`, so large magazines stream progressively; 404s if the row is missing, the file isn't on disk, or `ext != 'pdf'`. Frontend: `webui/static/reader.js` (single entry point `window.openReader(mediaId, { page })`) + `webui/static/reader.css`, built on **pdf.js 6.1.200** vendored at `webui/static/vendor/pdfjs/` (legacy ESM build, no npm/bundler — matches the project's no-build-step convention). EPUB has no reader; the Download button remains its only action. Design rationale in `docs/superpowers/specs/2026-06-30-pdf-reader-design.md`.

### Search
Full-text search over the downloaded library. Always enabled — zero startup cost. (The web UI and CLI previously also offered an "Ask AI" natural-language Q&A mode built on this index via the Anthropic API; it was removed — plain FTS search below is all that remains.)

- Index: SQLite FTS5 virtual table (`search_fts`) inside `tg_downloader.db`; BM25-ranked, accent-insensitive (`unicode61 remove_diacritics 1`)
- Chunking: one chunk per PDF page, one per EPUB chapter section; implemented in `search/chunker.py`
- Auto-indexed after each download; run `tgdctl index` to index existing files retroactively
- On `listen` startup, any `downloaded` file missing from `search_fts` is indexed in the background (startup heal)
- Only `pdf` and `epub` are indexed; other formats are skipped silently
- `index_file` sets `media_messages.indexed_at` after every completed attempt — including image-only/scanned PDFs that yield no text (no chunks). `search_fts_missing_media_ids()` excludes any row with `indexed_at IS NOT NULL`, so the startup heal does not re-scan textless PDFs on every restart (previously it re-attempted them forever, e.g. ~569 image PDFs taking ~6 min each boot). Files that *raise* during PDF parsing (e.g. "malformed page tree") still fall through unmarked and will retry — minor, low-volume.
- Web UI exposes `GET /api/search?q=` (FTS5)

### Container behaviour
- `restart: always` — containers restart automatically on crash or server reboot
- `./config.yaml` is bind-mounted read-only; `./data/` is bind-mounted read-write
- The downloader container's main process runs `main.py listen`; use `exec` for all other commands
- The webui container mounts the same `./data/` and the downloads directory read-write

### Setup (local, no Docker)
1. Copy `config.yaml.example` → `config.yaml`; fill in your values
2. `uv sync` — creates `.venv` and installs all dependencies
3. `uv run python main.py` — first run prompts for phone number and OTP

### Session file
Telethon writes a `tg_session.session` file after the first login. Subsequent runs reuse it without re-authenticating. Do not commit it.

### Download flow
On each `listen` startup the listener:
1. **Flushes pending** — downloads any DB records that are still `pending` (e.g. from a prior `scrape` or a crashed session).
2. **Heals missing** — re-downloads any files marked `downloaded` in the DB whose file is no longer on disk (manually deleted, disk replaced, etc.). The retention clock resets on re-download.
3. **Backfills gaps** — for each channel, finds the highest recorded `message_id` and fetches everything newer from Telegram, then downloads it. Only runs if at least one message was previously recorded for that channel (no reference point = skip; use `scrape` for initial history).
4. **Real-time** — downloads new messages immediately as they arrive via `asyncio.create_task`.
5. **Periodic backfill (safety net)** — `_backfill_loop` re-runs `_backfill_missed` every hour. Telethon's real-time update channel can go stale after a network blip while the TCP connection (and the asyncio loop) stays alive: the process keeps logging heartbeats but `events.NewMessage` stops firing, so downloads silently stop until the next restart. The hourly poll for messages newer than each channel's last recorded `message_id` closes that gap within the hour regardless of why real-time delivery stopped. Harmless when real-time is healthy — `min_id` is already current, so nothing is fetched and `save_media_message`'s `message_id` dedup prevents any double download. **Blind spot:** because `min_id` is anchored at `MAX(message_id)`, this loop only ever sees messages *newer* than the highest id recorded. A file dropped by real-time delivery *mid-burst*, while a higher id from the same burst landed, sits permanently below the watermark and is never recovered here (observed: 2 of a multi-file batch missed while neighbours downloaded). That case is handled by the deep-reconcile loop (point 5b) for recent drops, and by `scrape` for the historical backlog. The loop passes `warn_empty=False` so known-empty channels don't repeat the startup "no prior messages" warning hourly. Complemented by `catch_up=True` on the `TelegramClient` constructor + `await client.catch_up()` after handler registration, which recover gaps specifically on reconnect (the case the periodic loop can't distinguish from a stall). **Operator note:** a flat `+0/h` heartbeat is *not* by itself evidence of a stall — these channels are genuinely quiet between their ~daily newspaper drops. Confirm a real stall by checking whether the newest `media_messages.date` lags the expected delivery, not by the download rate alone.
5b. **Deep reconcile (mid-burst recovery)** — `_deep_reconcile_loop` runs once a day. For each channel it re-walks the last `RECONCILE_WINDOW` (400) messages with **no `min_id` watermark**, diffs every media id against `db.get_recorded_message_ids()`, and downloads anything missing. This is the counterpart to the backfill blind spot: it recovers files that real-time delivery dropped mid-burst within the recent window. Bounded by design (fixed window, daily) so it stays cheap on the Pi; the full historical backlog is recovered manually with `scrape` instead.
6. **Retention cleanup** — runs once on startup then every hour; deletes files older than `download.retention_days` days (set to 0 to disable). Default is 365 days.
7. **Heartbeat** — `_heartbeat_loop` logs one operational summary line every hour (`db.health_snapshot()`): `downloaded`, `+N/h`, `pending`, `indexed`, `index_pending`, `discarded`, `expired`, `channels_no_messages`. This is the at-a-glance "is anything being missed?" surface. `channels_no_messages > 0` flags channels that have never produced a message (not joined / wrong identifier / files delivered via bot/button instead of attachments).

The four batch-maintenance commands (`scan-languages`, `scan-topics`, `scan-hashes`, `index`) share the `_run_file_batch(items, description, handle)` helper in `main.py` for the Rich progress-bar loop and missing-on-disk handling; each command supplies only its query, per-file `handle` closure, and summary.

Concurrency is controlled by `download.concurrent_downloads` in `config.yaml` (default: 1). The listener constructs the `asyncio.Semaphore` from this value and passes it into `download_item`. Raise to 3 on faster connections; keep at 1 on Raspberry Pi — each concurrent download runs Telethon's MTProto crypto in software AES, which pegs ARM cores and spins the fan under sustained backfill load. The SHA-256 hash step runs via `asyncio.to_thread` so disk I/O doesn't block the event loop when concurrency > 1.

### Content filters (`lang_filter.py`)

After each successful download, `analyze_file(file_path, ext, topic_keywords, ...)` is called. It extracts text once and runs both language detection and topic filtering. If either check triggers, the file is deleted and the record is marked `discarded` without ever being marked `downloaded`.

**Language detection** — two-stage:

1. **Text extraction + langdetect** — extracts text from the first 4 pages (PDF via PyMuPDF) or first 3 content files (EPUB via zipfile). If ≥ 300 chars are found, `langdetect` is run; files detected as German (`de`) with confidence ≥ 0.90 are discarded. `DetectorFactory.seed = 0` ensures deterministic results.

2. **Filename heuristic** — fallback used only when text extraction yields nothing (image-based / scanned PDFs). Discards if the filename contains a German umlaut (`ä ö ü ß Ä Ö Ü`) or a German month name that differs from English (`januar`, `februar`, `märz`, `mai`, `juni`, `juli`, `oktober`, `dezember`). Months identical to English (`april`, `august`, `september`, `november`) are intentionally excluded to avoid false positives.

**Topic filtering** — keyword-based discard configured in `config.yaml`:

```yaml
filters:
  discard_topics:
    cars:
      - automobile
      - horsepower
      - ferrari
  topic_min_matches: 2              # distinct keywords that must match (default: 2)
  topic_min_keyword_occurrences: 2  # each keyword must appear this many times (default: 1)
```

Topic detection uses a deeper text sample than language detection: first 15 PDF pages (vs 4) plus document metadata (PDF `title`/`subject`/`keywords` properties; EPUB `dc:title`/`dc:subject`/`dc:description` from the OPF manifest). EPUB TOC/nav files are included for topic extraction since chapter titles are dense topic vocabulary. Metadata is excluded from language detection to avoid bias from English bibliographic fields in non-English books.

`compile_topic_patterns()` pre-compiles keyword regexes once per session. Matching uses whole-word boundaries (`\b`) with `finditer` + early exit to avoid scanning entire documents when the threshold is already met. The debug log records which specific keywords triggered each match.

Use `tgdctl scan-topics` to apply topic filters retroactively to already-downloaded files.

**Newspaper/periodical detection** — opt-in via `filters.discard_newspapers: true` (default `false`). Two independent signals, either one triggers a discard:

1. **Filename date** — an explicit numeric date in the filename (`YYYY-MM-DD`, `DD.MM.YYYY`, `DD_MM_YYYY`, etc.), locale-agnostic (no month names).
2. **Dateline repetition** — a date-shaped token appears on at least half of the sampled pages (PDF) or content files (EPUB), when at least 4 are sampled. Ordinary books rarely repeat a date on most pages; a daily paper's running masthead/footer date does this by construction.

Detection is a format signal, not a subject-matter one — unlike `discard_topics`, it doesn't use vocabulary keywords, since newspapers can cover any topic. Implemented in `lang_filter._looks_like_newspaper()` / `detect_newspaper()`. Use `tgdctl scan-newspapers` to apply retroactively to already-downloaded files.

Formats with no text extraction support (MOBI, AZW3, CBR, CBZ, DJVU, FB2) are always kept; filters only apply to `pdf` and `epub`.

**Critical**: the item dict passed to `download_item` must include `"ext"`. Both `_backfill_missed` and `_handle` in `listener.py` explicitly set this field; `_flush_pending` gets it from the full DB row automatically.

The detected language is stored in `media_messages.language` (ISO 639-1 code, nullable — `NULL` means undetected or unsupported format). Files that existed before this feature was added have `language = NULL`. New downloads are tagged automatically.

### Media statuses in DB
| Status | Meaning |
|---|---|
| `pending` | Recorded but not yet downloaded (should be 0 after startup flush) |
| `downloaded` | File is on disk |
| `discarded` | Deleted — by user via `discard`, by language/topic filter, or because the Telegram message was deleted before download completed |
| `expired` | Auto-deleted by retention cleanup |
| `skipped` | Legacy — dismissed without downloading in the old manual workflow |

### Known constraints
- Can only watch channels the authenticated user is a member of. The `subscribe` command adds the channel to the DB but does not join it; use the Telegram app to join first.
- Backfill only covers the gap since the last recorded message — it does not fetch all history. Use `scrape` for a full initial history pull. At startup, `listen` logs a `WARNING` for every channel with no recorded messages, naming the exact `scrape` command to run.
- The hourly backfill cannot recover a file dropped mid-burst *below* the highest recorded id (it polls `min_id = MAX(message_id)`). The daily deep-reconcile loop covers recent drops; for the historical backlog run `tgdctl scrape --dry-run` to audit, then `tgdctl scrape` to recover. `scrape` re-scans full history and only acts on ids never recorded in any status, so it never re-downloads files the language/topic filter already discarded.
- Photos from Telegram are always downloaded as JPEGs regardless of original format.
- Files without a `downloaded_at` timestamp (downloaded before this field was added) are not subject to automatic retention cleanup.
- If a Telegram message is deleted before its download completes, `download_item` marks the DB record as `discarded` (rather than leaving it `pending` forever) and logs a warning.
