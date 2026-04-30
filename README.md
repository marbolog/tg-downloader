# tg-downloader

A Telegram client that watches channels in real time, keeps track of every media file that appears, and lets you decide which ones to download — whenever you want.

## How it works

The tool has two separate concerns:

**Listener** (always running): connects to Telegram as your user account, watches subscribed channels, and silently records every incoming media message to a local SQLite database.

**Download** (on demand): you run a single command to open an interactive checklist of everything that has accumulated. Select the files you want and they are downloaded in one shot.

## Requirements

- Docker and Docker Compose (recommended), **or** Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A Telegram account
- API credentials from [my.telegram.org](https://my.telegram.org)

### Getting your API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in
2. Open **API Development tools**
3. Create an application (name and description do not matter)
4. Copy the `api_id` (a number) and `api_hash` (a hex string)

## Setup

### 1. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:

```yaml
telegram:
  api_id: 12345
  api_hash: "your_api_hash_here"
  session_file: "data/tg_session"    # where the Telegram session is stored

download:
  destination: "data/downloads"      # where downloaded files are saved

filters:
  extensions: []   # empty = track all types; e.g. ["pdf", "mp4", "jpg"]
```

Channels are **not** configured here — they are managed at runtime with the `subscribe` command.

### 2. Create the data directory

```bash
mkdir -p data/downloads
```

### 3. Install dependencies

```bash
uv sync
```

## Running with Docker (recommended)

### First-time Telegram authentication

Before starting the background service, authenticate once interactively:

```bash
uv run tgdctl auth
```

Enter your phone number (with country code, e.g. `+39...`) and the code Telegram sends to your app. Once connected and the listener starts, press `Ctrl+C`. The session is saved to `data/tg_session.session` and reused from this point on.

### Start the background service

```bash
uv run tgdctl start
```

Builds the Docker image and starts the listener as a background service with `restart: always` — it survives crashes and server reboots.

## Managing the service with `tgdctl`

`tgdctl` is the host-side management tool. Always use it instead of running app commands directly — it handles the Docker lifecycle and avoids Telegram session conflicts.

### Service control

```bash
uv run tgdctl start      # build and start the listener container
uv run tgdctl stop       # stop the container
uv run tgdctl restart    # restart the listener
uv run tgdctl logs       # tail container logs (Ctrl+C to stop)
uv run tgdctl logs -n    # print recent logs and exit
uv run tgdctl status     # container state + per-channel download stats
```

### Subscribe to a channel

```bash
uv run tgdctl subscribe @channel_username
uv run tgdctl subscribe https://t.me/channel_username
uv run tgdctl subscribe -1001234567890    # numeric ID, for private channels
```

> You must already be a member of the channel on Telegram.

The listener is briefly paused while the channel is resolved, then automatically restarted. New subscriptions take effect immediately.

### List subscribed channels

```bash
uv run tgdctl channels
```

```
         Subscribed Channels
┌──────────────┬──────────────┬─────────┬────────────┐
│ Title        │ Identifier   │ Pending │ Since      │
├──────────────┼──────────────┼─────────┼────────────┤
│ My Channel   │ @mychannel   │      12 │ 2024-11-01 │
│ Tech News    │ @technews    │       3 │ 2024-11-03 │
└──────────────┴──────────────┴─────────┴────────────┘
```

### Download pending media

```bash
uv run tgdctl download
```

The listener pauses briefly, then an interactive checklist opens with everything that has accumulated:

```
Space=toggle  A=select all  ↑↓=navigate  Enter=confirm

? Select media to download (15 pending):
 ❯ ◯ [My Channel]  report_2024.pdf  (2.1 MB, .pdf, 2024-11-10)
   ◯ [My Channel]  lecture_01.mp4   (210.0 MB, .mp4, 2024-11-09)
   ◯ [Tech News]   diagram.png  [interesting chart]  (340.0 KB, .png, 2024-11-08)
   ...
```

Selected files are downloaded with progress bars and marked as downloaded. Unselected files remain pending and appear again next time. The listener restarts automatically when done.

### Skip pending media

```bash
uv run tgdctl skip
```

Same interactive checklist as `download`, but marks the selected items as skipped instead of downloading them. Skipped files no longer appear in the pending list.

### View download history

```bash
uv run tgdctl history           # last 20 downloads
uv run tgdctl history --limit 50
```

### Download stats

```bash
uv run tgdctl status
```

Shows container state and a per-channel breakdown of pending, downloaded, and skipped file counts. Reads the database directly — works even when the container is stopped.

### Unsubscribe from a channel

```bash
uv run tgdctl unsubscribe @channel_username
```

Removes the channel from tracking. Previously recorded media messages are kept in the database.

## Running locally (without Docker)

```bash
uv sync
uv run tg-downloader listen           # start the listener
uv run tg-downloader subscribe @channel
uv run tg-downloader channels
uv run tg-downloader download
uv run tg-downloader skip
uv run tg-downloader history
uv run tg-downloader status
uv run tg-downloader unsubscribe @channel
```

> Do not run these commands while the Docker listener is running — they share the same Telegram session file and will conflict. Use `tgdctl` when Docker is active.

## Project structure

```
tg-downloader/
├── main.py              # entry point and CLI subcommands
├── config.py            # load and validate config.yaml
├── db.py                # SQLite schema and queries
├── listener.py          # real-time Telethon event handler
├── ui.py                # interactive file selection
├── downloader.py        # download selected files with progress bars
├── utils.py             # shared helpers
├── tgdctl.py            # host-side management CLI
├── config.yaml.example  # template — copy to config.yaml to get started
├── pyproject.toml       # dependencies managed by uv
├── Dockerfile
└── docker-compose.yml
```

## Data layout

Everything lives in `./data/` on the host, bind-mounted into the container:

```
data/
  tg_session.session    — Telegram auth session
  tg_downloader.db      — SQLite database (channels + media message history)
  downloads/            — downloaded files
```

> **Security**: treat `tg_session.session` and `config.yaml` like passwords — they give full access to your Telegram account. Both are excluded from git via `.gitignore`.
