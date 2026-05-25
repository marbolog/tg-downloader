import argparse
import asyncio
import logging
import sys

from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config
from db import Database

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%H:%M:%S",
    handlers=[RichHandler(console=console, show_path=False)],
)
# Telethon logs reconnection noise (idle TCP teardowns) at WARNING; suppress to ERROR.
logging.getLogger("telethon").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

DB_PATH = "data/tg_downloader.db"


def _load_session(session_base: str) -> StringSession | str:
    """Return a StringSession loaded from <session_base>.string if it exists.

    Falls back to the plain path string so Telethon opens the legacy SQLite
    .session file — this lets existing installs migrate without re-authing.
    """
    string_path = Path(session_base + ".string")
    if string_path.exists():
        return StringSession(string_path.read_text().strip())
    return session_base  # Telethon treats a str as a SQLiteSession path


def _save_session(client: TelegramClient, session_base: str) -> None:
    """Persist the current session to <session_base>.string.

    If the active session is a legacy SQLiteSession (first run), we extract its
    credentials and export them into a new StringSession before saving.
    """
    if isinstance(client.session, StringSession):
        session_str = client.session.save()
    else:
        # Migrate from SQLiteSession: copy dc/auth_key into a fresh StringSession.
        migrated = StringSession()
        migrated.set_dc(
            client.session.dc_id,
            client.session.server_address,
            client.session.port,
        )
        migrated.auth_key = client.session.auth_key
        session_str = migrated.save()

    if session_str:
        Path(session_base + ".string").write_text(session_str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("listen", help="Start the real-time listener (main service process)")

    p = sub.add_parser("subscribe", help="Subscribe to a channel")
    p.add_argument("channel", help="@username, t.me/... URL, or numeric Telegram ID")

    p = sub.add_parser("unsubscribe", help="Unsubscribe from a channel")
    p.add_argument("channel", help="Same identifier used when subscribing")

    sub.add_parser("channels", help="List subscribed channels and pending counts")

    sub.add_parser("discard", help="Review downloaded files and delete unwanted ones")

    sub.add_parser("status", help="Show download stats per channel")

    p = sub.add_parser("history", help="Show recently downloaded files")
    p.add_argument("--limit", type=int, default=20, metavar="N", help="Number of entries (default: 20)")

    p = sub.add_parser("scrape", help="Backfill media from channel history into the pending queue")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None, help="Specific channel (default: all)")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="Max messages to scan per channel (default: unlimited)")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None, help="Stop at messages older than this date")

    sub.add_parser("scan-languages", help="Retroactively detect language for untagged downloaded files; auto-discard German ones")

    sub.add_parser("scan-topics", help="Retroactively apply topic filters from config to already-downloaded files; auto-discard matches")

    sub.add_parser("scan-hashes", help="Compute SHA-256 for downloaded files without a hash (enables duplicate detection)")

    sub.add_parser("index", help="Index downloaded files into the RAG vector store")

    p = sub.add_parser("ask", help="Query the RAG index with a natural language question")
    p.add_argument("query", help="Natural language question")
    p.add_argument("--sources-only", action="store_true",
                   help="Show matching sources only, skip AI generation")
    p.add_argument("--top-k", type=int, default=None, metavar="N",
                   help="Number of chunks to retrieve (default: from config)")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None,
                   help="Restrict search to one channel")

    return parser


async def run(args) -> None:
    config = load_config("config.yaml")
    db = Database(DB_PATH)

    # These commands only touch the local DB — no Telegram connection needed.
    if args.command == "status":
        cmd_status(db)
        return
    if args.command == "history":
        cmd_history(db, args.limit)
        return
    if args.command == "channels":
        cmd_channels(db)
        return
    if args.command == "unsubscribe":
        cmd_unsubscribe(db, args.channel)
        return
    if args.command == "scan-languages":
        cmd_scan_languages(db)
        return
    if args.command == "scan-topics":
        cmd_scan_topics(db, config)
        return
    if args.command == "scan-hashes":
        cmd_scan_hashes(db)
        return
    if args.command == "index":
        await cmd_index(db, config)
        return
    if args.command == "ask":
        await cmd_ask(db, config, args)
        return
    if args.command == "discard":
        from ui import select_discard
        downloaded = db.get_downloaded_media()
        to_delete = await select_discard(downloaded)
        if to_delete:
            deleted = 0
            for item in to_delete:
                if item.get("local_path"):
                    p = Path(item["local_path"])
                    if p.exists():
                        p.unlink()
                        deleted += 1
                db.mark_discarded(item["id"])
            console.print(f"[green]Deleted {deleted}/{len(to_delete)} file(s).[/green]")
        else:
            console.print("[dim]Nothing deleted.[/dim]")
        return

    tg = config["telegram"]
    session = _load_session(tg["session_file"])
    client = TelegramClient(session, tg["api_id"], tg["api_hash"])
    await client.start()
    # Persist session immediately — migrates legacy SQLite session to StringSession
    # so subsequent runs never touch the SQLite session file again.
    _save_session(client, tg["session_file"])

    try:
        if args.command == "listen":
            from listener import run_listener
            await run_listener(client, db, config)

        elif args.command == "subscribe":
            await cmd_subscribe(client, db, args.channel)

        elif args.command == "scrape":
            await cmd_scrape(client, db, config, args.channel, args.limit, args.since)
    finally:
        _save_session(client, tg["session_file"])
        await client.disconnect()


async def cmd_subscribe(client: TelegramClient, db: Database, identifier: str) -> None:
    try:
        entity = await client.get_entity(identifier)
    except Exception as exc:
        console.print(f"[red]Cannot resolve {identifier!r}: {exc}[/red]")
        return
    title = getattr(entity, "title", identifier)
    db.add_channel(entity.id, identifier, title)
    console.print(f"[green]Subscribed:[/green] {title}  (id={entity.id})")

    if getattr(entity, "left", False):
        console.print(
            f"[yellow]Warning:[/yellow] your Telegram account is not a member of {title!r}. "
            f"Real-time message updates will not be received until you join in the Telegram app."
        )
    console.print(
        f"[dim]Tip:[/dim] run 'scrape --channel {identifier}' to download existing content from this channel."
    )


def cmd_unsubscribe(db: Database, identifier: str) -> None:
    if db.remove_channel(identifier):
        console.print(f"[green]Unsubscribed:[/green] {identifier}")
    else:
        console.print(f"[yellow]Not found:[/yellow] {identifier}")


def cmd_channels(db: Database) -> None:
    channels = db.list_channels()
    if not channels:
        console.print("[yellow]No subscribed channels. Use 'subscribe' to add one.[/yellow]")
        return

    pending = db.pending_counts()
    table = Table(title="Subscribed Channels")
    table.add_column("Title")
    table.add_column("Identifier")
    table.add_column("Pending", justify="right")
    table.add_column("Since")

    for c in channels:
        table.add_row(
            c["title"],
            c["identifier"],
            str(pending.get(c["id"], 0)),
            c["added_at"][:10],
        )
    console.print(table)


def cmd_status(db: Database) -> None:
    rows = db.get_status_counts()
    if not rows:
        console.print("[yellow]No channels subscribed.[/yellow]")
        return

    table = Table(title="Download Status")
    table.add_column("Channel")
    table.add_column("On Disk", justify="right", style="green")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Removed", justify="right", style="dim")
    table.add_column("Total", justify="right")

    totals = {"downloaded": 0, "pending": 0, "removed": 0, "total": 0}
    for r in rows:
        removed = (r["discarded"] or 0) + (r["expired"] or 0) + (r["skipped"] or 0)
        totals["downloaded"] += r["downloaded"] or 0
        totals["pending"] += r["pending"] or 0
        totals["removed"] += removed
        totals["total"] += r["total"] or 0
        table.add_row(
            r["title"],
            str(r["downloaded"] or 0),
            str(r["pending"] or 0),
            str(removed),
            str(r["total"] or 0),
        )
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{totals['downloaded']}[/bold]",
        f"[bold]{totals['pending']}[/bold]",
        f"[bold]{totals['removed']}[/bold]",
        f"[bold]{totals['total']}[/bold]",
    )
    console.print(table)


def cmd_history(db: Database, limit: int) -> None:
    rows = db.get_download_history(limit)
    if not rows:
        console.print("[yellow]No downloads yet.[/yellow]")
        return

    table = Table(title=f"Recent Downloads (last {limit})")
    table.add_column("Downloaded")
    table.add_column("Channel")
    table.add_column("File")
    table.add_column("Saved as")

    for r in rows:
        table.add_row(
            (r.get("downloaded_at") or r.get("date") or "")[:16],
            (r.get("channel_title") or "")[:25],
            (r.get("filename") or "")[:40],
            (r.get("local_path") or "")[:60],
        )
    console.print(table)


def cmd_scan_languages(db: Database) -> None:
    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TextColumn, TimeElapsedColumn
    from lang_filter import DISCARD_LANG, detect_language

    items = db.get_untagged_downloaded()
    if not items:
        console.print("[green]All downloaded files already have a language tag.[/green]")
        return

    console.print(f"[dim]Scanning {len(items)} untagged file(s)…[/dim]")

    counts: dict[str, int] = {}
    discarded = 0
    missing = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Detecting languages", total=len(items))
        for item in items:
            path = Path(item["local_path"])
            if not path.exists():
                missing += 1
                progress.advance(task)
                continue

            lang = detect_language(path, (item.get("ext") or "").lower())

            if lang == DISCARD_LANG:
                path.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                discarded += 1
                counts["de"] = counts.get("de", 0) + 1
                log.info(f"Scan-discarded (German): {item['filename']}")
            else:
                db.set_language(item["id"], lang)
                key = lang or "__unknown__"
                counts[key] = counts.get(key, 0) + 1

            progress.advance(task)

    table = Table(title="Scan Results")
    table.add_column("Language")
    table.add_column("Files", justify="right")
    for lang_key, n in sorted(counts.items(), key=lambda x: -x[1]):
        style = "red" if lang_key == "de" else ""
        label = f"{lang_key} [dim](discarded)[/dim]" if lang_key == "de" else lang_key
        table.add_row(label, str(n), style=style)
    if missing:
        table.add_row("[dim]missing on disk[/dim]", str(missing))
    console.print(table)
    console.print(
        f"\n[green]Done.[/green] Scanned {len(items)} file(s): "
        f"[red]{discarded} discarded[/red], {missing} missing, "
        f"{len(items) - discarded - missing} tagged."
    )


def cmd_scan_topics(db: Database, config: dict) -> None:
    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TextColumn, TimeElapsedColumn
    from lang_filter import compile_topic_patterns, detect_topic

    topic_keywords = config["filters"].get("discard_topics") or {}
    topic_min_matches = config["filters"].get("topic_min_matches", 2)
    topic_min_occurrences = config["filters"].get("topic_min_keyword_occurrences", 1)

    if not topic_keywords:
        console.print("[yellow]No discard_topics configured — nothing to scan.[/yellow]")
        return

    items = db.get_downloaded_media()
    if not items:
        console.print("[yellow]No downloaded files found.[/yellow]")
        return

    console.print(f"[dim]Scanning {len(items)} file(s) against {len(topic_keywords)} topic(s)…[/dim]")

    compiled = compile_topic_patterns(topic_keywords)
    counts: dict[str, int] = {}
    discarded = 0
    missing = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Detecting topics", total=len(items))
        for item in items:
            path = Path(item["local_path"])
            if not path.exists():
                missing += 1
                progress.advance(task)
                continue

            ext = (item.get("ext") or "").lower()
            topic = detect_topic(path, ext, topic_keywords, topic_min_matches, topic_min_occurrences, compiled_patterns=compiled)

            if topic:
                path.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                discarded += 1
                counts[topic] = counts.get(topic, 0) + 1
                log.info(f"Scan-discarded (topic: {topic}): {item['filename']}")

            progress.advance(task)

    if counts:
        table = Table(title="Scan Results")
        table.add_column("Topic")
        table.add_column("Discarded", justify="right")
        for topic_name, n in sorted(counts.items(), key=lambda x: -x[1]):
            table.add_row(topic_name, str(n), style="red")
        if missing:
            table.add_row("[dim]missing on disk[/dim]", str(missing))
        console.print(table)
    else:
        console.print("[green]No topic matches found.[/green]")
        if missing:
            console.print(f"[dim]{missing} file(s) not found on disk.[/dim]")

    console.print(
        f"\n[green]Done.[/green] Scanned {len(items)} file(s): "
        f"[red]{discarded} discarded[/red], {missing} missing."
    )


def cmd_scan_hashes(db: Database) -> None:
    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TextColumn, TimeElapsedColumn
    from utils import compute_sha256

    items = db.get_untagged_for_hash()
    if not items:
        console.print("[green]All downloaded files already have a hash.[/green]")
        return

    console.print(f"[dim]Hashing {len(items)} file(s)…[/dim]")
    hashed = 0
    missing = 0
    errors = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Computing SHA-256", total=len(items))
        for item in items:
            path = Path(item["local_path"])
            if not path.exists():
                missing += 1
                progress.advance(task)
                continue
            try:
                h = compute_sha256(path)
                db.set_file_hash(item["id"], h)
                hashed += 1
            except Exception as exc:
                log.warning(f"Hash failed for {item['filename']!r}: {exc}")
                errors += 1
            progress.advance(task)

    console.print(
        f"\n[green]Done.[/green] {hashed} file(s) hashed, {missing} missing on disk"
        + (f", {errors} errors" if errors else "") + "."
    )

    # Show duplicate groups found
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect("data/tg_downloader.db")
    conn.row_factory = _sqlite3.Row
    try:
        dupes = conn.execute("""
            SELECT file_hash, COUNT(*) AS copies, MIN(filename) AS example
            FROM media_messages
            WHERE status = 'downloaded' AND file_hash IS NOT NULL
            GROUP BY file_hash
            HAVING copies > 1
            ORDER BY copies DESC
        """).fetchall()
    finally:
        conn.close()

    if dupes:
        table = Table(title=f"Duplicate Groups ({len(dupes)} found)")
        table.add_column("Copies", justify="right", style="yellow")
        table.add_column("Example filename")
        for row in dupes[:20]:
            table.add_row(str(row["copies"]), row["example"])
        if len(dupes) > 20:
            table.add_row("…", f"…and {len(dupes) - 20} more groups")
        console.print(table)
    else:
        console.print("[green]No duplicate files found.[/green]")


async def cmd_index(db: Database, config: dict) -> None:
    from rich.progress import Progress, BarColumn, MofNCompleteColumn, TextColumn, TimeElapsedColumn

    rag_config = config.get("rag", {})
    if not rag_config.get("enabled"):
        console.print("[yellow]RAG not enabled -- set rag.enabled: true in config.yaml[/yellow]")
        return

    items = db.get_unindexed_downloaded()
    if not items:
        console.print("[green]All downloaded files are already indexed.[/green]")
        return

    from rag.indexer import Indexer
    indexer = Indexer(rag_config)

    _SUPPORTED = {"pdf", "epub"}
    indexed = skipped = errors = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Indexing {len(items)} file(s)", total=len(items))
        for item in items:
            ext = (item.get("ext") or "").lower()
            local_path = item.get("local_path")
            if ext not in _SUPPORTED or not local_path or not Path(local_path).exists():
                skipped += 1
                progress.advance(task)
                continue
            progress.update(task, description=item["filename"][:45])
            try:
                count = await asyncio.to_thread(
                    indexer.index_file,
                    item["id"],
                    Path(local_path),
                    {
                        "filename": item["filename"],
                        "channel_title": item.get("channel_title", ""),
                        "channel_identifier": item.get("channel_identifier", ""),
                        "ext": ext,
                    },
                )
                if count > 0:
                    db.mark_indexed(item["id"])
                    indexed += 1
                else:
                    skipped += 1
            except Exception as exc:
                log.warning(f"Index failed for {item['filename']!r}: {exc}")
                errors += 1
            progress.advance(task)

    console.print(
        f"\n[green]Done.[/green] {indexed} indexed, "
        f"{skipped} skipped (unsupported/missing), "
        + (f"[red]{errors} errors[/red]." if errors else "0 errors.")
    )


async def cmd_ask(db: Database, config: dict, args) -> None:
    rag_config = config.get("rag", {})
    if not rag_config.get("enabled"):
        console.print("[yellow]RAG not enabled -- set rag.enabled: true in config.yaml[/yellow]")
        return

    from rag.indexer import Indexer
    from rag.retriever import retrieve
    from rag.generator import generate, OllamaUnavailableError

    indexer = Indexer(rag_config)
    top_k = args.top_k or rag_config.get("top_k", 5)
    channel = getattr(args, "channel", None)

    chunks = retrieve(args.query, indexer, top_k=top_k, channel_identifier=channel)
    if not chunks:
        console.print("[yellow]No matching content found in the index.[/yellow]")
        return

    if getattr(args, "sources_only", False):
        console.print("\n[bold]Sources:[/bold]")
        for chunk in chunks:
            loc = f"p. {chunk['page']}" if chunk.get("page") is not None else (chunk.get("chapter") or "")
            console.print(f"  . {chunk['filename']:<45} {loc}")
        return

    console.print("\n[bold]Answer:[/bold]")
    try:
        async for token in generate(
            args.query,
            chunks,
            rag_config.get("ollama_url", "http://localhost:11434"),
            rag_config.get("ollama_model", "phi3:mini"),
        ):
            print(token, end="", flush=True)
        print()
    except OllamaUnavailableError as exc:
        console.print(f"\n[red]Error:[/red] {exc}")

    console.print("\n[bold]Sources:[/bold]")
    seen: set[str] = set()
    for chunk in chunks:
        fn = chunk["filename"]
        if fn not in seen:
            seen.add(fn)
            loc = f"p. {chunk['page']}" if chunk.get("page") is not None else (chunk.get("chapter") or "")
            console.print(f"  . {fn:<45} {loc}")


async def cmd_scrape(
    client: TelegramClient,
    db: Database,
    config: dict,
    identifier: str | None,
    limit: int | None,
    since: str | None,
) -> None:
    from datetime import datetime, timezone
    from listener import _extract_media

    channels = db.list_channels()
    if identifier:
        channels = [c for c in channels if c["identifier"] == identifier or str(c["telegram_id"]) == identifier]
        if not channels:
            console.print(f"[red]Channel not found:[/red] {identifier}")
            return

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)

    allowed = set(config["filters"]["extensions"])
    total_new = 0

    for ch in channels:
        label = f"since {since}" if since_dt else f"up to {limit} messages" if limit else "all history"
        console.print(f"[dim]Scanning {ch['title']} ({label})…[/dim]")
        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            console.print(f"[red]Cannot resolve {ch['identifier']!r}: {exc} — skipping[/red]")
            continue
        count = 0
        scanned = 0
        async for message in client.iter_messages(entity, limit=limit):
            if since_dt and message.date < since_dt:
                break
            scanned += 1
            if not message.media:
                continue
            item = _extract_media(message)
            if item is None:
                continue
            if allowed and item["ext"] not in allowed:
                continue
            inserted = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item["filename"],
                size=item["size"],
                mime_type=item["mime_type"],
                ext=item["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if inserted:
                count += 1
        console.print(f"  [green]+{count} new item(s)[/green] ({scanned} messages scanned)")
        total_new += count

    console.print(
        f"\n[green]Done — {total_new} new item(s) queued. "
        f"Will be downloaded when the listener next starts.[/green]"
    )


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
