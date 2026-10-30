"""Browsable 'category' views over the flat downloads folder, and a DB-safe delete.

The downloads directory is one flat folder. `build_views` generates a separate
directory of symlinks grouped by channel / language / type / month so a plain file
manager (yazi, via `tgdctl browse`) can navigate by category.

Invariants:
- Views are disposable: every category folder is rebuilt from the DB on each call.
  Only the CATEGORIES folders are ever touched; anything else in the views dir stays.
- A symlink delete would only remove the link, and deleting a real file behind the
  app's back makes the listener's "heal missing" step re-download it. So all deletes
  go through `discard_paths`, which records the discard in the DB *before* touching
  disk (same ordering as the web UI's POST /api/discard).
"""
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from db import Database

log = logging.getLogger(__name__)

CATEGORIES = ("by-channel", "by-language", "by-type", "by-month")
DISCARD_REASON = "manual:fs"


@dataclass
class DiscardReport:
    discarded: list[Path] = field(default_factory=list)   # real files deleted, DB row marked discarded
    untracked: list[Path] = field(default_factory=list)   # real files deleted that had no DB row
    skipped: dict[Path, str] = field(default_factory=dict)  # input path -> why it was left alone
    failed: dict[Path, str] = field(default_factory=dict)   # real file -> unlink error (DB already updated)


def build_views(db: Database, views_dir: Path, downloads_dir: Path | None = None) -> dict[str, int]:
    """Rebuild every category tree under `views_dir`. Returns {category: link count}."""
    views_dir = Path(views_dir)
    if downloads_dir is not None and _is_within(views_dir, Path(downloads_dir)):
        raise ValueError(f"views directory {views_dir} must not be inside the downloads directory")

    for category in CATEGORIES:
        shutil.rmtree(views_dir / category, ignore_errors=True)
        (views_dir / category).mkdir(parents=True, exist_ok=True)

    counts = dict.fromkeys(CATEGORIES, 0)
    skipped_missing = 0
    for row in db.get_downloaded_media():
        local_path = row["local_path"]
        if not local_path or not Path(local_path).exists():
            skipped_missing += 1
            continue
        buckets = {
            "by-channel": row["channel_title"] or row["channel_identifier"],
            "by-language": row["language"],
            "by-type": (row["ext"] or "").lower(),
            "by-month": (row["date"] or "")[:7],
        }
        for category, bucket in buckets.items():
            _link(views_dir / category / _folder_name(bucket), Path(local_path), row["id"])
            counts[category] += 1

    log.info("views rebuilt", extra={"views_dir": str(views_dir), "counts": counts, "skipped_missing_file": skipped_missing})
    return counts


def discard_paths(db: Database, paths: list[Path], downloads_dir: Path, views_dir: Path) -> DiscardReport:
    """Delete the real files behind `paths` (real files or view symlinks).

    Order matters: one DB transaction first, then unlink. If the DB write raises
    (e.g. "database is locked") nothing has been deleted and the error propagates.
    """
    report = DiscardReport()
    downloads_real = Path(os.path.realpath(downloads_dir))

    targets: dict[Path, Path] = {}  # real path -> the path the user selected
    for given in map(Path, paths):
        if given.is_dir() and not given.is_symlink():
            report.skipped[given] = "is a directory"
            continue
        real = Path(os.path.realpath(given))
        if not _is_within(real, downloads_real):
            report.skipped[given] = "outside the downloads directory"
        elif real.is_dir():
            report.skipped[given] = "is a directory"
        else:
            targets.setdefault(real, given)

    media_id_by_real = {
        os.path.realpath(row["local_path"]): row["id"]
        for row in db.get_downloaded_media() if row["local_path"]
    }
    tracked_ids = [media_id_by_real[str(real)] for real in targets if str(real) in media_id_by_real]
    db.mark_discarded_many(tracked_ids, reason=DISCARD_REASON)

    for real in targets:
        try:
            real.unlink(missing_ok=True)
        except OSError as exc:
            report.failed[real] = str(exc)
            log.error("unlink failed after DB update", extra={"path": str(real), "error": str(exc)})
            continue
        (report.discarded if str(real) in media_id_by_real else report.untracked).append(real)

    prune_dangling_links(views_dir)
    log.info("paths discarded", extra={
        "discarded": len(report.discarded), "untracked": len(report.untracked),
        "skipped": len(report.skipped), "failed": len(report.failed),
    })
    return report


def prune_dangling_links(views_dir: Path) -> None:
    """Remove symlinks whose target is gone, then folders left empty, in category trees only."""
    for category in CATEGORIES:
        root = Path(views_dir) / category
        if not root.is_dir():
            continue
        for link in root.rglob("*"):
            if link.is_symlink() and not link.exists():
                link.unlink()
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()


def _folder_name(bucket: str | None) -> str:
    """Safe single path component for a bucket label ('' / None -> 'unknown')."""
    name = (bucket or "").replace("/", "_").replace("\0", "_").strip()
    return "unknown" if name in ("", ".", "..") else name


def _link(folder: Path, target: Path, media_id: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / target.name
    if link.is_symlink() or link.exists():  # same basename from a different directory
        link = folder / f"{target.stem}~{media_id}{target.suffix}"
    link.symlink_to(target)


def _is_within(path: Path, parent: Path) -> bool:
    path, parent = Path(os.path.realpath(path)), Path(os.path.realpath(parent))
    return path == parent or parent in path.parents
