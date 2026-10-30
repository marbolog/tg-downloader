# tg-downloader

Watches Telegram channels with your user account and **automatically downloads** every media file as it arrives. Downloads are filtered (language, topic, newspapers), indexed for full-text search, cleaned up after a retention period, and can be browsed from a web UI or a terminal file manager.

## How it works

- **Listener** (always running, in Docker): connects to Telegram, downloads new media in real time, and records every file in a local SQLite database. On each start it flushes pending items, re-downloads files that vanished from disk, and backfills anything missed while it was offline. An hourly backfill and a daily deep reconcile catch messages that real-time delivery dropped.
- **Filters** (after each download): German-language files, configurable topic keywords and newspaper/periodical-shaped files are discarded automatically. Only PDF and EPUB are inspected.
- **Search**: PDF pages and EPUB chapters are indexed in SQLite FTS5 (BM25, accent-insensitive).
- **Retention**: files older than `download.retention_days` are deleted hourly.
- **Web UI** (port 8090): thumbnails, filters, duplicate detection, an in-browser PDF reader, search, delete and download.
- **`tgdctl browse`**: yazi on generated category folders, with safe delete and PDF/EPUB/image previews.

## Requirements

- Docker and Docker Compose (recommended), **or** Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A Telegram account that is already a member of the channels you want
- API credentials from [my.telegram.org](https://my.telegram.org) → **API Development tools** → create an app → copy `api_id` and `api_hash`
- Optional, for `tgdctl browse`: [yazi](https://yazi-rs.github.io/) (`apt install yazi`) and `poppler-utils` (PDF previews)

## Setup

```bash
cp config.yaml.example config.yaml    # fill in api_id and api_hash
mkdir -p data/downloads
uv sync
uv run tgdctl auth                    # one-time phone + code login; Ctrl+C once connected
uv run tgdctl start                   # build and start the listener (restart: always)
uv run tgdctl subscribe @channel      # you must already be a member on Telegram
```

The session is saved to `data/tg_session.session` and reused. Treat it and `config.yaml` like passwords (both are git-ignored). `config.yaml.example` documents every option: `download.*` (destination, retention, concurrency, timeout, views directory), `filters.*` (extensions, `discard_topics`, `discard_newspapers`, `newspaper_names`) and `search.top_k`.

> Keep `concurrent_downloads: 1` on a Raspberry Pi: Telethon's crypto pegs ARM cores otherwise.

## `tgdctl` commands

`tgdctl` runs on the host and wraps Docker; use it instead of the app commands while the container is up (they share one Telegram session).

| Group | Command | What it does |
|---|---|---|
| Service | `start` / `stop` / `restart` | Control the listener container |
| | `logs [-n]` | Tail logs (`-n` prints and exits) |
| | `auth` | First-time Telegram login |
| | `status` | Container state and per-channel stats (works with the container stopped) |
| | `progress [-w]` | Overall progress bar and recent files; `-w` live-updates |
| Channels | `subscribe @ch` / `unsubscribe @ch` / `channels` | Manage subscriptions (also accepts `https://t.me/...` or a numeric id) |
| Files | `history [--limit N]` | Recently downloaded files |
| | `discard` | Interactive checklist to delete unwanted files (file + DB record) |
| | `browse` | yazi on category folders, see below |
| | `views` | Rebuild the category folders without opening yazi |
| | `discard-path FILE... [-y]` | Delete files and record it in the DB (what yazi's delete keys call) |
| Recovery | `scrape --dry-run` | Audit: media on Telegram missing from the DB, per channel (writes nothing) |
| | `scrape [--channel X] [--limit N] [--since DATE]` | Queue every missing file; downloaded on the listener restart |
| Maintenance | `scan-languages`, `scan-topics`, `scan-newspapers` | Apply the filters retroactively |
| | `scan-hashes` | SHA-256 for every file; enables duplicate detection in the web UI |
| | `index` | Index existing files into the search table |

> Never wrap `tgdctl scrape` in `timeout`: it stops the listener, runs, then restarts it, and killing it midway leaves the listener stopped.

Without Docker: `uv run tg-downloader <listen|subscribe|unsubscribe|channels|discard|status|history|scrape>`.

## Browsing files in the terminal

```bash
uv run tgdctl browse
```

Builds `data/views/` (symlinks grouped `by-channel/`, `by-language/`, `by-type/`, `by-month/`) and opens yazi on it with an isolated config (your own yazi is untouched).

- `d` / `D` / `Delete` delete the marked or hovered files **and** record them in the database, so the listener never re-downloads them. `x p P r a` are disabled because moving or renaming real files would desync the DB.
- `T` shows or hides the preview pane (hidden at startup so browsing stays fast on a Pi). PDF: first page. Image: the image (needs a terminal image protocol). **EPUB: the cover image**, from `yazi/plugins/epub-cover.yazi`; a book without a declared cover shows a text message.
- EPUBs are untrusted input: the cover helper reads archive members as bytes, caps sizes, refuses XML with a DTD/entities, and caches covers in `~/.cache/tg-downloader-yazi/`.

Full illustrated guide: <https://marbolog.github.io/tgdctl-browse/>

## Web UI

`http://<host>:8090`. Set `WEBUI_PASSWORD` (and optionally `WEBUI_USERNAME`, default `tg`) in a `.env` next to `docker-compose.yml` to enable HTTP Basic Auth; unset means no auth.

Card grid with PDF/EPUB cover thumbnails, channel and language filters, language badges, duplicate collapsing, multi-select delete, per-file download, full-text search, and a mobile-friendly PDF reader (lazy pages, zoom, deep links to a page, region crop).

## Reliability notes

- `restart: always` restarts crashed containers; the `autoheal` service also restarts the listener if its healthcheck fails (a hung process does not exit by itself). The healthcheck fails when `data/.heartbeat` is older than 90 minutes; the listener touches it after every processed item.
- The listener logs an hourly summary (downloaded, `+N/h`, pending, discards by reason). A flat `+0/h` is normal for quiet channels; check the newest `media_messages.date` before assuming a stall.
- Every discard records a reason (`german`, `topic:<name>`, `newspaper`, `message_deleted`, `manual:cli|webui|fs`).

## Project structure

```
main.py            app CLI entry point and subcommands
config.py          load and validate config.yaml
db.py              SQLite schema and all queries (shared with the web UI)
listener.py        real-time listener, backfill, reconcile, retention, heartbeat
downloader.py      single-file download with timeouts
lang_filter.py     language, topic and newspaper detection
views.py           category symlink folders and DB-safe delete
tgdctl.py          host-side management CLI
ui.py              interactive discard checklist
utils.py           shared helpers
search/            FTS5 chunking (PDF pages, EPUB chapters) and indexing
webui/             FastAPI app, static SPA, PDF reader, own Dockerfile
yazi/              isolated yazi config, key bindings, plugins (toggle-pane, epub-cover)
tests/             pytest suite
docs/superpowers/  design specs and implementation plans
```

`CLAUDE.md` holds the detailed engineering notes (incidents, invariants, design rationale).

## Data layout

```
data/
  tg_session.session   Telegram auth session
  tg_downloader.db     SQLite database (WAL mode)
  downloads/           downloaded files (flat)
  views/               generated category symlinks (safe to delete)
  thumbs/              cached web UI thumbnails
  .heartbeat           healthcheck marker
```
