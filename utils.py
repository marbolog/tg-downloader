import asyncio
import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Strong references to fire-and-forget tasks. The event loop only keeps a weak
# reference to tasks, so an un-referenced task can be garbage-collected while
# still running (documented asyncio footgun) — fatal for the listener's
# long-lived background loops.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def create_tracked_task(coro, *, name: str | None = None) -> asyncio.Task:
    """asyncio.create_task + a strong reference until done + error logging.

    Use for every fire-and-forget task. Exceptions that would otherwise vanish
    with the task object (e.g. a background loop dying) are logged on completion.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"Background task {task.get_name()!r} died: {exc}", exc_info=exc)


def human_size(n_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def compute_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of the file, reading in 64 KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_path(path: Path) -> Path:
    """Return path unchanged if it does not exist, otherwise append _1, _2, etc."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
