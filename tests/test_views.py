"""Tests for views.py: the symlink 'category' trees and the DB-safe delete used by
`tgdctl browse` (a file manager rooted at the views directory)."""
import os
from pathlib import Path

import pytest

from db import Database
from views import build_views, discard_paths


@pytest.fixture
def env(tmp_path):
    """A DB, a downloads dir with real files, and an (empty) views dir."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    views = tmp_path / "views"
    db = Database(str(tmp_path / "test.db"))
    return db, downloads, views


def _add_file(db, downloads, name, *, channel="@mags", title="Mags", telegram_id=1,
              ext="pdf", language="en", date="2026-03-15T10:00:00", content=b"x", msg_id=None):
    """Create a real file plus its `downloaded` DB row; returns (media_id, path)."""
    db.add_channel(telegram_id, channel, title)
    channel_id = next(c["id"] for c in db.list_channels() if c["telegram_id"] == telegram_id)
    msg_id = msg_id or (abs(hash(name)) % 10**6)
    media_id = db.save_media_message(channel_id, msg_id, name, len(content), "application/pdf", ext, date, "")
    path = downloads / name
    path.write_bytes(content)
    db.mark_downloaded(media_id, str(path), language=language)
    return media_id, path


def _links(views: Path, category: str) -> dict[str, Path]:
    """{relative path inside category: symlink target} for every link in a category."""
    root = views / category
    return {str(p.relative_to(root)): Path(os.readlink(p)) for p in root.rglob("*") if p.is_symlink()}


class TestBuildViews:
    def test_groups_files_by_channel_language_type_and_month(self, env):
        db, downloads, views = env
        _, a = _add_file(db, downloads, "a.pdf", channel="@mags", title="Mags", language="en")
        _, b = _add_file(db, downloads, "b.epub", channel="@books", title="Books", telegram_id=2,
                         ext="epub", language="fr", date="2026-04-01T00:00:00")

        build_views(db, views)

        assert _links(views, "by-channel") == {"Mags/a.pdf": a, "Books/b.epub": b}
        assert _links(views, "by-language") == {"en/a.pdf": a, "fr/b.epub": b}
        assert _links(views, "by-type") == {"pdf/a.pdf": a, "epub/b.epub": b}
        assert _links(views, "by-month") == {"2026-03/a.pdf": a, "2026-04/b.epub": b}

    def test_missing_metadata_goes_to_unknown_buckets(self, env):
        db, downloads, views = env
        _, a = _add_file(db, downloads, "a.pdf", language=None, date="")

        build_views(db, views)

        assert _links(views, "by-language") == {"unknown/a.pdf": a}
        assert _links(views, "by-month") == {"unknown/a.pdf": a}

    def test_channel_title_with_slash_cannot_escape_its_folder(self, env):
        db, downloads, views = env
        _add_file(db, downloads, "a.pdf", title="AC/DC ../x")

        build_views(db, views)

        dirs = [p.name for p in (views / "by-channel").iterdir()]
        assert len(dirs) == 1 and "/" not in dirs[0] and dirs[0] != ".."

    def test_rows_whose_file_is_gone_are_skipped(self, env):
        db, downloads, views = env
        _, a = _add_file(db, downloads, "a.pdf")
        a.unlink()

        build_views(db, views)

        assert _links(views, "by-channel") == {}

    def test_discarded_rows_are_not_listed(self, env):
        db, downloads, views = env
        media_id, _ = _add_file(db, downloads, "a.pdf")
        db.mark_discarded(media_id, reason="manual:cli")

        build_views(db, views)

        assert _links(views, "by-channel") == {}

    def test_rebuild_drops_stale_links_and_leaves_other_folders_alone(self, env):
        db, downloads, views = env
        _, a = _add_file(db, downloads, "a.pdf")
        build_views(db, views)
        (views / "notes").mkdir()
        (views / "notes" / "keep.txt").write_text("hi")
        a.unlink()

        build_views(db, views)

        assert _links(views, "by-channel") == {}
        assert (views / "notes" / "keep.txt").read_text() == "hi"

    def test_refuses_views_dir_inside_downloads(self, env):
        db, downloads, _ = env
        with pytest.raises(ValueError, match="views"):
            build_views(db, downloads / "views", downloads_dir=downloads)


class TestDiscardPaths:
    def test_discarding_a_link_removes_real_file_marks_db_and_prunes_links(self, env):
        db, downloads, views = env
        media_id, real = _add_file(db, downloads, "a.pdf")
        build_views(db, views)
        link = views / "by-channel" / "Mags" / "a.pdf"

        report = discard_paths(db, [link], downloads, views)

        assert not real.exists()
        assert db.get_media(media_id)["status"] == "discarded"
        assert db.get_media(media_id)["discard_reason"] == "manual:fs"
        assert _links(views, "by-language") == {}   # every category link to it is gone
        assert report.discarded == [real]
        assert report.skipped == {}

    def test_discarding_the_real_file_directly_is_also_safe(self, env):
        db, downloads, views = env
        media_id, real = _add_file(db, downloads, "a.pdf")

        discard_paths(db, [real], downloads, views)

        assert not real.exists()
        assert db.get_media(media_id)["status"] == "discarded"

    def test_db_is_updated_before_any_file_is_deleted(self, env, monkeypatch):
        db, downloads, views = env
        _, real = _add_file(db, downloads, "a.pdf")

        def boom(*a, **kw):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db, "mark_discarded_many", boom)

        with pytest.raises(RuntimeError):
            discard_paths(db, [real], downloads, views)

        assert real.exists()   # a failed DB write must never leave a deleted-but-'downloaded' row

    def test_file_with_no_db_row_is_deleted_and_reported(self, env):
        db, downloads, views = env
        stray = downloads / "stray.pdf"
        stray.write_bytes(b"x")

        report = discard_paths(db, [stray], downloads, views)

        assert not stray.exists()
        assert report.untracked == [stray]

    def test_paths_outside_downloads_are_never_touched(self, env, tmp_path):
        db, downloads, views = env
        outside = tmp_path / "precious.txt"
        outside.write_text("keep me")
        sneaky = views / "sneaky"
        views.mkdir()
        sneaky.symlink_to(outside)

        report = discard_paths(db, [outside, sneaky], downloads, views)

        assert outside.read_text() == "keep me"
        assert set(report.skipped) == {outside, sneaky}
        assert report.discarded == []

    def test_directories_are_skipped(self, env):
        db, downloads, views = env
        _add_file(db, downloads, "a.pdf")
        build_views(db, views)
        folder = views / "by-channel" / "Mags"

        report = discard_paths(db, [folder], downloads, views)

        assert (folder / "a.pdf").is_symlink()
        assert folder in report.skipped

    def test_duplicate_selection_of_same_file_counts_once(self, env):
        db, downloads, views = env
        _, real = _add_file(db, downloads, "a.pdf")
        build_views(db, views)
        links = [views / "by-channel" / "Mags" / "a.pdf", views / "by-type" / "pdf" / "a.pdf"]

        report = discard_paths(db, links, downloads, views)

        assert report.discarded == [real]

    def test_dangling_link_whose_row_still_says_downloaded_is_still_recorded(self, env):
        db, downloads, views = env
        media_id, real = _add_file(db, downloads, "a.pdf")
        build_views(db, views)
        link = views / "by-channel" / "Mags" / "a.pdf"
        real.unlink()   # file removed behind our back

        discard_paths(db, [link], downloads, views)

        assert db.get_media(media_id)["status"] == "discarded"
