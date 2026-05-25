import pytest
from db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    return d


def test_search_fts_table_created(db):
    with db._conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='search_fts'"
        ).fetchone()
    assert row is not None


def test_index_and_query(db):
    chunks = [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "Alentejo wine country plains"},
        {"chunk_idx": 1, "page": 2, "chapter": None, "text": "Douro valley port wine"},
    ]
    db.search_fts_index_file(media_id=1, chunks=chunks, filename="portugal.pdf")

    results = db.search_fts_query("Alentejo", top_k=5)
    assert len(results) == 1
    assert results[0]["media_id"] == 1
    assert results[0]["filename"] == "portugal.pdf"
    assert results[0]["page"] == 1
    assert results[0]["chapter"] is None
    assert "text" in results[0]


def test_query_returns_ranked_results(db):
    chunks = [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "wine wine wine Douro valley"},
        {"chunk_idx": 1, "page": 2, "chapter": None, "text": "wine tasting in Alentejo"},
    ]
    db.search_fts_index_file(media_id=2, chunks=chunks, filename="wine.pdf")
    results = db.search_fts_query("wine", top_k=5)
    assert len(results) == 2
    # BM25 rank: page 1 has more "wine" occurrences → should rank first
    assert results[0]["page"] == 1


def test_delete_removes_chunks(db):
    chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "Alentejo travel guide"}]
    db.search_fts_index_file(media_id=3, chunks=chunks, filename="alentejo.pdf")
    assert len(db.search_fts_query("Alentejo")) == 1

    db.search_fts_delete_file(media_id=3)
    assert len(db.search_fts_query("Alentejo")) == 0


def test_missing_media_ids(db):
    # Insert a downloaded item without indexing it
    with db._conn() as conn:
        conn.execute("""
            INSERT INTO channels (telegram_id, identifier, title)
            VALUES (100, '@test', 'Test')
        """)
        conn.execute("""
            INSERT INTO media_messages
                (channel_id, message_id, filename, size, ext, status, local_path)
            VALUES (1, 1, 'test.pdf', 1000, 'pdf', 'downloaded', '/tmp/test.pdf')
        """)
        conn.commit()

    missing = db.search_fts_missing_media_ids()
    assert len(missing) == 1
    assert missing[0]["filename"] == "test.pdf"

    # After indexing, it should no longer appear
    chunks = [{"chunk_idx": 0, "page": 1, "chapter": None, "text": "test content"}]
    db.search_fts_index_file(media_id=1, chunks=chunks, filename="test.pdf")
    assert db.search_fts_missing_media_ids() == []
