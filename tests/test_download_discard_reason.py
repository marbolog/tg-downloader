"""download_item must record *why* it auto-discarded a file."""
import asyncio
from types import SimpleNamespace

import pytest

import downloader
from db import Database
from lang_filter import FilterSettings


class FakeClient:
    async def download_media(self, message, file):
        open(file, "wb").write(b"x")


def _setup(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.add_channel(1, "@c", "C")
    ch = db.list_channels()[0]
    media_id = db.save_media_message(ch["id"], 5, "f.pdf", 1, "application/pdf", "pdf", "d", "")
    item = {"id": media_id, "message_id": 5, "filename": "f.pdf", "size": 1, "ext": "pdf",
            "channel_identifier": "@c"}
    return db, item


def _reason(db, media_id):
    with db._conn() as conn:
        row = conn.execute(
            "SELECT status, discard_reason FROM media_messages WHERE id=?", (media_id,)
        ).fetchone()
    return row["status"], row["discard_reason"]


@pytest.mark.parametrize("analysis, expected", [
    (("de", None, False), "german"),
    ((None, "cars", False), "topic:cars"),
    (("en", None, True), "newspaper"),
])
async def test_auto_discard_records_reason(tmp_path, monkeypatch, analysis, expected):
    db, item = _setup(tmp_path)
    monkeypatch.setattr(downloader, "analyze_file", lambda *a, **k: analysis)

    await downloader.download_item(
        FakeClient(), db, item, tmp_path, asyncio.Semaphore(1), FilterSettings(),
        message=SimpleNamespace(),
    )

    assert _reason(db, item["id"]) == ("discarded", expected)


async def test_deleted_telegram_message_records_reason(tmp_path):
    db, item = _setup(tmp_path)

    class GoneClient(FakeClient):
        async def get_entity(self, identifier):
            return identifier

        async def get_messages(self, entity, ids):
            return None

    await downloader.download_item(
        GoneClient(), db, item, tmp_path, asyncio.Semaphore(1), FilterSettings()
    )

    assert _reason(db, item["id"]) == ("discarded", "message_deleted")
