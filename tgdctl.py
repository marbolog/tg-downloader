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
    the same session simultaneously.
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
        from rich.console import Console  # noqa: F401
        _rich_status_table(rows)
    except ImportError:
        _plain_status_table(rows)


def cmd_progress(watch: bool) -> None:
    if not DB_PATH.exists():
        print("No database found — run 'tgdctl start' first.")
        return

    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.progress import BarColumn, Progress, TextColumn, MofNCompleteColumn
    from rich.columns import Columns
    import time

    console = Console()

    def _build_display():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            counts = dict(conn.execute("""
                SELECT
                    SUM(CASE WHEN status='pending'    THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) AS downloaded
                FROM media_messages
            """).fetchone())

            recent = [dict(r) for r in conn.execute("""
                SELECT m.filename, m.size, m.downloaded_at, c.title AS channel_title
                FROM media_messages m
                JOIN channels c ON m.channel_id = c.id
                WHERE m.status = 'downloaded' AND m.downloaded_at IS NOT NULL
                ORDER BY m.downloaded_at DESC
                LIMIT 12
            """).fetchall()]
        finally:
            conn.close()

        pending = counts["pending"] or 0
        downloaded = counts["downloaded"] or 0
        total = downloaded + pending

        # Progress bar
        prog = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TextColumn("[yellow]{task.fields[pending]} pending"),
        )
        prog.add_task("Downloaded", completed=downloaded, total=total, pending=pending)

        # Recent downloads table
        table = Table(title="Recently Downloaded", box=None, padding=(0, 1))
        table.add_column("Time", style="dim", width=16)
        table.add_column("Channel", width=22)
        table.add_column("File")

        for r in recent:
            ts = (r.get("downloaded_at") or "")[:16]
            ch = (r.get("channel_title") or "")[:22]
            fn = (r.get("filename") or "")[:55]
            table.add_row(ts, ch, fn)

        from rich.panel import Panel
        from rich.console import Group
        return Group(Panel(prog, expand=False), table)

    if watch:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            try:
                while True:
                    live.update(_build_display())
                    time.sleep(2)
            except KeyboardInterrupt:
                pass
    else:
        console.print(_build_display())


def _db_status_rows() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("""
            SELECT c.title,
                   SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                   SUM(CASE WHEN m.status='pending'    THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN m.status='discarded'  THEN 1 ELSE 0 END) AS discarded,
                   SUM(CASE WHEN m.status='expired'    THEN 1 ELSE 0 END) AS expired,
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
    table.add_column("On Disk", justify="right", style="green")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Removed", justify="right", style="dim")
    table.add_column("Total", justify="right")

    totals = [0, 0, 0, 0]
    for r in rows:
        d = r["downloaded"] or 0
        p = r["pending"] or 0
        removed = (r["discarded"] or 0) + (r["expired"] or 0) + (r["skipped"] or 0)
        t = r["total"] or 0
        totals[0] += d
        totals[1] += p
        totals[2] += removed
        totals[3] += t
        table.add_row(r["title"], str(d), str(p), str(removed), str(t))

    table.add_section()
    table.add_row("[bold]Total[/bold]", *[f"[bold]{t}[/bold]" for t in totals])
    console.print(table)


def _plain_status_table(rows: list[dict]) -> None:
    print(f"\n{'Channel':<30} {'On Disk':>8} {'Pending':>8} {'Removed':>8} {'Total':>6}")
    print("-" * 66)
    totals = [0, 0, 0, 0]
    for r in rows:
        d = r["downloaded"] or 0
        p = r["pending"] or 0
        removed = (r["discarded"] or 0) + (r["expired"] or 0) + (r["skipped"] or 0)
        t = r["total"] or 0
        totals[0] += d
        totals[1] += p
        totals[2] += removed
        totals[3] += t
        print(f"{r['title']:<30} {d:>8} {p:>8} {removed:>8} {t:>6}")
    print("-" * 66)
    print(f"{'Total':<30} {totals[0]:>8} {totals[1]:>8} {totals[2]:>8} {totals[3]:>6}")


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
  progress   Live download progress + recent files  (-w to watch)

app commands (proxied into the running container):
  subscribe   @channel    Subscribe to a channel (pauses listener briefly)
  unsubscribe @channel    Unsubscribe from a channel
  channels                List subscribed channels
  discard                 Review downloaded files and delete unwanted ones
  history                 Show recently downloaded files
  scrape [--channel X] [--limit N] [--since DATE]  Backfill media from history (pauses listener briefly)
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
    p = sub.add_parser("progress")
    p.add_argument("-w", "--watch", action="store_true", help="Refresh every 2 seconds")

    p = sub.add_parser("subscribe")
    p.add_argument("channel")
    p = sub.add_parser("unsubscribe")
    p.add_argument("channel")
    sub.add_parser("channels")
    sub.add_parser("discard")
    p = sub.add_parser("history")
    p.add_argument("--limit", type=int, default=20, metavar="N")

    p = sub.add_parser("scrape")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None)
    p.add_argument("--limit", type=int, default=None, metavar="N")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None)

    args = parser.parse_args()

    if args.command == "start":
        sys.exit(cmd_start())
    elif args.command == "progress":
        cmd_progress(watch=args.watch)
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
    elif args.command == "discard":
        # No listener restart needed — discard only manages local files and DB.
        sys.exit(app("discard", interactive=True))
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
