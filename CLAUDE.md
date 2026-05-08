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
- **InquirerPy** — interactive checkbox file selection in the terminal
- **rich** — tables, styled output
- **PyYAML** — config file
- **FastAPI + uvicorn** — web UI service (`webui/`)
- **PyMuPDF** — PDF cover thumbnail extraction (webui) + text extraction for language detection (main app)
- **Pillow** — image resizing for thumbnails
- **langdetect** — language detection for automatic German-content filtering

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
| `webui/app.py` | FastAPI web UI — file grid with cover previews, discard, download |
| `webui/static/index.html` | Single-page app (vanilla JS, all inline) |
| `webui/Dockerfile` | Separate image for the web UI service |

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
uv run tgdctl unsubscribe @channel   # unsubscribe from a channel
uv run tgdctl scan-languages         # retroactively detect language for untagged files; discard German ones
uv run tgdctl scan-topics            # retroactively apply topic filters to downloaded files; discard matches
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
- Thumbnails are cached in `data/thumbs/` and generated on first request

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
2. **Backfills gaps** — for each channel, finds the highest recorded `message_id` and fetches everything newer from Telegram, then downloads it. Only runs if at least one message was previously recorded for that channel (no reference point = skip; use `scrape` for initial history).
3. **Real-time** — downloads new messages immediately as they arrive via `asyncio.create_task`.
4. **Retention cleanup** — runs once on startup then every hour; deletes files older than `download.retention_days` days (set to 0 to disable).

`downloader.py` limits concurrency to `CONCURRENT_DOWNLOADS = 1` via `asyncio.Semaphore`. Raise to 3 on faster connections; keep at 1 on Raspberry Pi — each concurrent download runs Telethon's MTProto crypto in software AES, which pegs ARM cores and spins the fan under sustained backfill load.

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

Formats with no text extraction support (MOBI, AZW3, CBR, CBZ, DJVU, FB2) are always kept; filters only apply to `pdf` and `epub`.

**Critical**: the item dict passed to `download_item` must include `"ext"`. Both `_backfill_missed` and `_handle` in `listener.py` explicitly set this field; `_flush_pending` gets it from the full DB row automatically.

The detected language is stored in `media_messages.language` (ISO 639-1 code, nullable — `NULL` means undetected or unsupported format). Files that existed before this feature was added have `language = NULL`. New downloads are tagged automatically.

### Media statuses in DB
| Status | Meaning |
|---|---|
| `pending` | Recorded but not yet downloaded (should be 0 after startup flush) |
| `downloaded` | File is on disk |
| `discarded` | Deleted — either by user via `discard` command or automatically by the language filter |
| `expired` | Auto-deleted by retention cleanup |
| `skipped` | Legacy — dismissed without downloading in the old manual workflow |

### Known constraints
- Can only watch channels the authenticated user is a member of.
- Backfill only covers the gap since the last recorded message — it does not fetch all history. Use `scrape` for a full initial history pull.
- Photos from Telegram are always downloaded as JPEGs regardless of original format.
- Files without a `downloaded_at` timestamp (downloaded before this field was added) are not subject to automatic retention cleanup.
