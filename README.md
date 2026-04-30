# tg-downloader

A CLI tool that scans Telegram channels for media files (PDFs, videos, images, archives, etc.) and lets you interactively select which ones to download.

## How it works

1. Connects to Telegram using your personal account via the [Telethon](https://docs.telethon.dev/) MTProto client
2. Scans each configured channel and collects all messages that contain a file
3. Displays a summary table and an interactive checklist of everything found
4. Downloads your selection to a local folder, with per-file progress bars

## Requirements

- Docker and Docker Compose (recommended), **or** Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A Telegram account
- API credentials from [my.telegram.org](https://my.telegram.org)

### Getting your API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in
2. Open **API Development tools**
3. Create an application (the name and description do not matter)
4. Copy the `api_id` (a number) and `api_hash` (a hex string)

## Setup

### 1. Configure

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your credentials and channels:

```yaml
telegram:
  api_id: 12345                      # from my.telegram.org
  api_hash: "your_api_hash_here"
  session_file: "data/tg_session"    # where the session is stored

channels:
  - "@channel_username"
  - "https://t.me/another_channel"
  - "-1001234567890"                 # numeric ID for private channels

download:
  destination: "data/downloads"      # where files are saved
  max_messages_per_channel: 500      # how far back to scan

filters:
  extensions: []                     # empty = all types; e.g. ["pdf", "mp4", "jpg"]
```

> **Channel access**: you must already be a member of every channel you list. The tool uses your personal account, not a bot.

### 2. Create the data directory

```bash
mkdir -p data/downloads
```

## Running with Docker (recommended)

Docker keeps the service always available and restarts it automatically if the server reboots.

### Start the container

```bash
sudo docker compose up -d --build
```

### First-time authentication

The first run requires an interactive login — Telegram will send a confirmation code to your app:

```bash
sudo docker compose exec -it tg-downloader uv run python main.py
```

Enter your phone number (with country code) and the code when prompted. The session is saved to `data/tg_session.session` and reused automatically from that point on.

### Everyday use

```bash
sudo docker compose exec -it tg-downloader uv run python main.py
```

Downloaded files appear in `./data/downloads/` on the host.

### Container lifecycle

| Command | Effect |
|---|---|
| `sudo docker compose up -d --build` | Build image and start container in background |
| `sudo docker compose down` | Stop and remove the container |
| `sudo docker compose logs` | View container logs |

The container uses `restart: always`, so it comes back automatically after a server reboot without any manual intervention. The Docker daemon itself must be enabled as a system service (it is by default on most Linux distributions).

## Running locally (without Docker)

```bash
# Install dependencies
uv sync

# Run
uv run python main.py
```

## Interactive file selection

When you run the tool you will see a scan summary followed by a checklist:

```
  Scan Summary
 ┌─────────────────┬───────┬────────────┐
 │ Channel         │ Files │ Total size │
 ├─────────────────┼───────┼────────────┤
 │ My Channel      │    12 │   340.5 MB │
 │ Another Channel │     5 │    89.1 MB │
 ├─────────────────┼───────┼────────────┤
 │ Total           │    17 │   429.6 MB │
 └─────────────────┴───────┴────────────┘

Space=toggle  A=select all  ↑↓=navigate  Enter=confirm

? Select files to download (17 available):
 ❯ ◯ [My Channel]  report_2024.pdf  (2.1 MB, .pdf, 2024-11-03)
   ◯ [My Channel]  lecture_01.mp4   (210.0 MB, .mp4, 2024-10-28)
   ...
```

Press `Space` to toggle individual files, `A` to select all, and `Enter` to start downloading.

## Project structure

```
tg-downloader/
├── main.py              # entry point
├── config.py            # load and validate config.yaml
├── scraper.py           # scan channels, return MediaItem list
├── ui.py                # scan summary table + interactive selection
├── downloader.py        # download selected files with progress bars
├── utils.py             # shared helpers (human_size, unique_path, ...)
├── config.yaml.example  # template — copy to config.yaml
├── pyproject.toml       # dependencies managed by uv
├── Dockerfile
└── docker-compose.yml
```

## Data persistence

All runtime data lives in `./data/` on the host, bind-mounted into the container:

```
data/
  tg_session.session   — Telegram auth session (do not share or commit)
  downloads/           — downloaded files
```

> **Security**: treat `tg_session.session` like a password. Anyone with this file can access your Telegram account. Add `data/` and `config.yaml` to `.gitignore` if you use version control.
