"""Recovery behaviour of the listener's backfill/retry paths.

Telegram is replaced by a minimal fake client; `listener.download_item` is
replaced by a recorder, so these tests observe which items the listener decides
to download -- not Telethon or the network.
"""
import asyncio
from types import SimpleNamespace

import pytest

import listener
from db import Database
from downloader import download_timeout
from lang_filter import FilterSettings


class FakeMessage:
    def __init__(self, msg_id: int):
        self.id = msg_id
        self.media = True
        self.document = True
        self.photo = None
        self.message = ""
        self.date = SimpleNamespace(isoformat=lambda: "2026-10-01T00:00:00+00:00")
        self.file = SimpleNamespace(
            name=f"f{msg_id}.pdf", size=10, mime_type="application/pdf", ext=".pdf"
        )


class FakeClient:
    def __init__(self, messages: list[FakeMessage]):
        self._messages = sorted(messages, key=lambda m: m.id, reverse=True)
        self.iter_calls: list[dict] = []

    async def get_entity(self, identifier):
        return identifier

    async def iter_messages(self, entity, min_id=0, limit=None):
        self.iter_calls.append({"min_id": min_id, "limit": limit})
        yielded = 0
        for m in self._messages:
            if m.id <= min_id:
                continue
            if limit is not None and yielded >= limit:
                return
            yielded += 1
            yield m


@pytest.fixture
def downloads(monkeypatch):
    """Record the message ids listener asks download_item to fetch."""
    requested: list[int] = []

    async def fake_download_item(client, db, item, *args, **kwargs):
        requested.append(item["message_id"])
        return True

    monkeypatch.setattr(listener, "download_item", fake_download_item)
    return requested


def _db_with_channel(tmp_path) -> tuple[Database, dict]:
    db = Database(str(tmp_path / "test.db"))
    db.add_channel(1, "@test", "Test")
    return db, db.list_channels()[0]


def _args(tmp_path):
    return dict(
        dest=tmp_path, semaphore=asyncio.Semaphore(1),
        filters=FilterSettings(), timeout_seconds=5,
    )


async def test_backfill_bootstraps_channel_with_no_recorded_messages(tmp_path, downloads):
    db, _ = _db_with_channel(tmp_path)
    client = FakeClient([FakeMessage(i) for i in (5, 6, 7)])

    await listener._backfill_missed(
        client, db, {"pdf"}, tmp_path, asyncio.Semaphore(1), FilterSettings(), 5
    )

    assert sorted(downloads) == [5, 6, 7]


async def test_retry_downloads_only_items_pending_across_two_checks(tmp_path, downloads):
    db, ch = _db_with_channel(tmp_path)
    stale_id = db.save_media_message(ch["id"], 10, "a.pdf", 1, "application/pdf", "pdf", "d", "")
    fresh_id = db.save_media_message(ch["id"], 11, "b.pdf", 1, "application/pdf", "pdf", "d", "")

    # fresh_id was not pending at the previous check: it may be a download still in flight.
    still_pending = await listener._retry_stale_pending(
        FakeClient([]), db, previously_pending={stale_id}, **_args(tmp_path)
    )

    assert downloads == [10]
    assert still_pending == {stale_id, fresh_id}


def test_download_timeout_grows_with_file_size():
    assert download_timeout(size_bytes=1_000, base_seconds=600) == 600
    assert download_timeout(size_bytes=114_000_000, base_seconds=600) > 600
