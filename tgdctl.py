#!/usr/bin/env python3
"""Host-side management CLI for tg-downloader.

Wraps docker compose for service control and proxies app commands through
docker compose exec. Also reads the SQLite DB directly from the host mount
for offline status queries.

Usage:
    uv run tgdctl <command> [args]
    # or, once installed:
    tgdctl <command> [args]
"""

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / "data" / "tg_downloader.db"
SERVICE = "tg-downloader"

# Commands that need a TTY (interactive prompts)
INTERACTIVE = {"download", "auth"}


def compose(*args) -> int:
    return subprocess.call(["sudo", "docker", "compose", *args], cwd=PROJECT_DIR)


def app(*app_args, interactive: bool = False) -> int:
    flags = ["-it"] if interactive else ["-T"]
    return subprocess.call(
        ["sudo", "docker", "compose", "exec", *flags, SERVICE,
         "uv", "run", "python", "main.py", *app_args],
        cwd=PROJECT_DIR,
    )


def cmd_start() -> int:
    return compose("up", "-d", "--build")


def cmd_stop() -> int:
    return compose("down")


def cmd_restart() -> int:
    return compose("restart", SERVICE)


def cmd_logs(follow: bool) -> int:
    args = ["logs", SERVICE]
    if follow:
        args.insert(1, "-f")
    return compose(*args)


def cmd_auth() -> int:
    """First-time Telegram authentication — runs listen interactively, Ctrl+C once done."""
    return subprocess.call(
        ["sudo", "docker", "compose", "run", "--rm", "-it", SERVICE,
         "uv", "run", "python", "main.py", "listen"],
        cwd=PROJECT_DIR,
    )


def run_with_restart(*app_args: str, interactive: bool = False) -> int:
    """Stop the listener, run an app command in a fresh container, restart.

    Needed for commands that open a Telethon client: two clients cannot share
    the same SQLite session file simultaneously.
    """
    print("Stopping listener to free Telegram session...")
    compose("stop", SERVICE)
    try:
        flags = ["-it"] if interactive else ["-T"]
        return subprocess.call(
            ["sudo", "docker", "compose", "run", "--rm", *flags, SERVICE,
             "uv", "run", "python", "main.py", *app_args],
            cwd=PROJECT_DIR,
        )
    finally:
        print("Restarting listener...")
        compose("up", "-d", SERVICE)


def cmd_status() -> None:
    compose("ps", SERVICE)

    if not DB_PATH.exists():
        print("\nNo database found — run 'tgdctl start' first.")
        return

    rows = _db_status_rows()
    if not rows:
        print("\nNo channels subscribed.")
        return

    try:
        from rich.console import Console
        from rich.table import Table
        _rich_status_table(rows)
    except ImportError:
        _plain_status_table(rows)


def _db_status_rows() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("""
            SELECT c.title,
                   SUM(CASE WHEN m.status='pending'    THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                   SUM(CASE WHEN m.status='skipped'    THEN 1 ELSE 0 END) AS skipped,
                   COUNT(m.id) AS total
            FROM channels c
            LEFT JOIN media_messages m ON m.channel_id = c.id
            GROUP BY c.id
            ORDER BY c.added_at
        """).fetchall()]
    finally:
        conn.close()


def _rich_status_table(rows: list[dict]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Download Status")
    table.add_column("Channel")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Downloaded", justify="right", style="green")
    table.add_column("Skipped", justify="right", style="dim")
    table.add_column("Total", justify="right")

    totals = [0, 0, 0, 0]
    for r in rows:
        p, d, s, t = r["pending"] or 0, r["downloaded"] or 0, r["skipped"] or 0, r["total"] or 0
        totals[0] += p
        totals[1] += d
        totals[2] += s
        totals[3] += t
        table.add_row(r["title"], str(p), str(d), str(s), str(t))

    table.add_section()
    table.add_row("[bold]Total[/bold]", *[f"[bold]{t}[/bold]" for t in totals])
    console.print(table)


def _plain_status_table(rows: list[dict]) -> None:
    print(f"\n{'Channel':<30} {'Pending':>8} {'Downloaded':>10} {'Skipped':>8} {'Total':>6}")
    print("-" * 66)
    totals = [0, 0, 0, 0]
    for r in rows:
        p, d, s, t = r["pending"] or 0, r["downloaded"] or 0, r["skipped"] or 0, r["total"] or 0
        totals[0] += p
        totals[1] += d
        totals[2] += s
        totals[3] += t
        print(f"{r['title']:<30} {p:>8} {d:>10} {s:>8} {t:>6}")
    print("-" * 66)
    print(f"{'Total':<30} {totals[0]:>8} {totals[1]:>10} {totals[2]:>8} {totals[3]:>6}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tgdctl",
        description="Manage the tg-downloader service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
service commands:
  start      Build and start the listener container
  stop       Stop the container
  restart    Restart the listener (picks up new config)
  logs       Tail container logs  (-n to print and exit)
  auth       First-time Telegram authentication (interactive)
  status     Container state + per-channel DB stats

app commands (proxied into the running container):
  subscribe  @channel    Subscribe to a channel (pauses listener briefly)
  unsubscribe @channel   Unsubscribe from a channel
  channels               List subscribed channels
  download               Select items to download or skip (pauses listener briefly)
  history                Show recently downloaded files
  scrape [--channel X] [--limit N]  Backfill media from channel history (pauses listener briefly)
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    p = sub.add_parser("logs")
    p.add_argument("-n", "--no-follow", action="store_true", help="Print and exit")
    sub.add_parser("auth")
    sub.add_parser("status")

    p = sub.add_parser("subscribe")
    p.add_argument("channel")
    p = sub.add_parser("unsubscribe")
    p.add_argument("channel")
    sub.add_parser("channels")
    sub.add_parser("download")
    p = sub.add_parser("history")
    p.add_argument("--limit", type=int, default=20, metavar="N")

    p = sub.add_parser("scrape")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None)
    p.add_argument("--limit", type=int, default=None, metavar="N")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None)

    args = parser.parse_args()

    if args.command == "start":
        sys.exit(cmd_start())
    elif args.command == "stop":
        sys.exit(cmd_stop())
    elif args.command == "restart":
        sys.exit(cmd_restart())
    elif args.command == "logs":
        sys.exit(cmd_logs(follow=not args.no_follow))
    elif args.command == "auth":
        sys.exit(cmd_auth())
    elif args.command == "status":
        cmd_status()
    elif args.command == "subscribe":
        sys.exit(run_with_restart("subscribe", args.channel))
    elif args.command == "unsubscribe":
        sys.exit(app("unsubscribe", args.channel))
    elif args.command == "channels":
        sys.exit(app("channels"))
    elif args.command == "download":
        sys.exit(run_with_restart("download", interactive=True))
    elif args.command == "history":
        sys.exit(app("history", "--limit", str(args.limit)))
    elif args.command == "scrape":
        extra = []
        if args.limit is not None:
            extra += ["--limit", str(args.limit)]
        if args.channel:
            extra += ["--channel", args.channel]
        if args.since:
            extra += ["--since", args.since]
        sys.exit(run_with_restart("scrape", *extra))


if __name__ == "__main__":
    main()
