# tg-downloader

A Telegram client that watches channels in real time, keeps track of every media file that appears, and lets you decide which ones to download — whenever you want.

## How it works

The tool has two separate concerns:

**Listener** (always running): connects to Telegram as your user account, watches subscribed channels, and silently records every incoming media message to a local SQLite database.

**Download** (on demand): you run a single command to open an interactive checklist of everything that has accumulated. Select the files you want, and they are downloaded in one shot.

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

download:
  destination: "data/downloads"

filters:
  extensions: []   # empty = track all types; e.g. ["pdf", "mp4", "jpg"]
```

Channels are **not** configured here — they are managed at runtime with the `subscribe` command.

### 2. Create the data directory

```bash
mkdir -p data/downloads
```

## Running with Docker (recommended)

### Start the container

```bash
sudo docker compose up -d --build
```

The container starts the listener automatically as its main process and restarts on crash or server reboot (`restart: always`).

### First-time Telegram authentication

The very first run requires an interactive login — Telegram sends a confirmation code to your app:

```bash
sudo docker compose exec -it tg-downloader uv run python main.py listen
```

Enter your phone number (with country code, e.g. `+39...`) and the code. After that the session is saved to `data/tg_session.session` and reused automatically — you will never be asked again unless the session expires.

Once authenticated, restart the container so it runs as a proper background service:

```bash
sudo docker compose restart
```

## Commands

All commands are run via `docker compose exec`:

```bash
alias tgd="sudo docker compose exec -it tg-downloader uv run python main.py"
```

### Subscribe to a channel

```bash
tgd subscribe @channel_username
tgd subscribe https://t.me/channel_username
tgd subscribe -1001234567890    # numeric ID, for private channels you are a member of
```

> You must already be a member of the channel on Telegram.

### List subscribed channels

```bash
tgd channels
```

Output:

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
tgd download
```

Opens an interactive checklist of everything that has arrived since your last download session:

```
Space=toggle  A=select all  ↑↓=navigate  Enter=confirm

? Select media to download (15 pending):
 ❯ ◯ [My Channel]  report_2024.pdf  (2.1 MB, .pdf, 2024-11-10)
   ◯ [My Channel]  lecture_01.mp4   (210.0 MB, .mp4, 2024-11-09)
   ◯ [Tech News]   diagram.png  [interesting chart]  (340.0 KB, .png, 2024-11-08)
   ...
```

Selected files are downloaded with progress bars and marked as downloaded in the database. Unselected files remain pending and appear again next time.

### Unsubscribe from a channel

```bash
tgd unsubscribe @channel_username
```

Removes the channel. Previously recorded media messages from that channel are kept in the database.

## Running locally (without Docker)

```bash
uv sync
uv run python main.py listen       # start the listener
uv run python main.py subscribe @channel
uv run python main.py download
```

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
├── config.yaml.example  # template — copy to config.yaml
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
