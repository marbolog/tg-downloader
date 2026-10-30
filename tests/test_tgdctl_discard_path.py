"""CLI layer of the safe delete: confirmation prompt and wiring to views.discard_paths.
(The delete rules themselves are covered in test_views.py.)"""
from pathlib import Path

import pytest

import tgdctl
from db import Database


@pytest.fixture
def library(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    views = tmp_path / "views"
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.add_channel(1, "@mags", "Mags")
    channel_id = db.list_channels()[0]["id"]
    real = downloads / "a.pdf"
    real.write_bytes(b"x")
    media_id = db.save_media_message(channel_id, 1, "a.pdf", 1, "application/pdf", "pdf", "2026-03-01T00:00:00", "")
    db.mark_downloaded(media_id, str(real), language="en")
    monkeypatch.setattr(tgdctl, "DB_PATH", db_path)
    monkeypatch.setattr(tgdctl, "_views_settings", lambda: (downloads, views))
    return db, media_id, real


def test_answering_no_deletes_nothing(library, monkeypatch):
    db, media_id, real = library
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    assert tgdctl.cmd_discard_path([str(real)], assume_yes=False) == 0

    assert real.exists()
    assert db.get_media(media_id)["status"] == "downloaded"


def test_yes_flag_deletes_file_and_records_it(library):
    db, media_id, real = library

    assert tgdctl.cmd_discard_path([str(real)], assume_yes=True) == 0

    assert not real.exists()
    assert db.get_media(media_id)["discard_reason"] == "manual:fs"


def test_db_failure_deletes_nothing_and_returns_error(library, monkeypatch):
    _, _, real = library
    monkeypatch.setattr("builtins.input", lambda *_: "")  # "Press Enter to return"

    def locked(*a, **kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr("views.Database.mark_discarded_many", locked)

    assert tgdctl.cmd_discard_path([str(real)], assume_yes=True) == 1
    assert real.exists()
