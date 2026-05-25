# FTS5 Search + Claude API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the entire RAG stack (chromadb, sentence-transformers, Ollama) with SQLite FTS5 full-text search and the Anthropic Claude Haiku API, eliminating the RAM pressure that freezes the Raspberry Pi.

**Architecture:** SQLite FTS5 virtual table lives inside `tg_downloader.db`; text is extracted per-page (PDF) or per-chapter (EPUB) and inserted at download time; web UI queries FTS5 for ranked results with snippet excerpts; the Claude Haiku API receives top-K chunks and streams an answer back.

**Tech Stack:** Python 3.11, sqlite3 (stdlib), PyMuPDF (already a dependency), zipfile (stdlib), anthropic Python SDK, FastAPI (existing).

---

## File Map

| Action | File |
|---|---|
| Create | `search/__init__.py` |
| Create | `search/chunker.py` |
| Create | `search/indexer.py` |
| Create | `search/generator.py` |
| Create | `tests/test_search_chunker.py` |
| Create | `tests/test_search_db.py` |
| Modify | `db.py` |
| Modify | `downloader.py` |
| Modify | `listener.py` |
| Modify | `main.py` |
| Modify | `webui/app.py` |
| Modify | `webui/static/index.html` |
| Modify | `pyproject.toml` |
| Modify | `config.yaml.example` |
| Modify | `docker-compose.yml` |
| Modify | `Dockerfile` |
| Modify | `webui/Dockerfile` |
| Delete | `rag/` (entire directory) |
| Delete | `tests/test_chunker.py` |

---

### Task 1: Create `search/chunker.py` + tests

**Files:**
- Create: `search/__init__.py`
- Create: `search/chunker.py`
- Create: `tests/test_search_chunker.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_search_chunker.py
import io
import zipfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from search.chunker import chunk_file, SUPPORTED_EXTS


def test_supported_exts():
    assert "pdf" in SUPPORTED_EXTS
    assert "epub" in SUPPORTED_EXTS
    assert "mobi" not in SUPPORTED_EXTS


def test_unsupported_format_returns_empty():
    chunks = chunk_file(Path("book.mobi"), "mobi")
    assert chunks == []


def test_pdf_chunks_have_required_keys():
    mock_doc = MagicMock()
    mock_doc.page_count = 2
    mock_page0 = MagicMock()
    mock_page0.get_text.return_value = "Page one content here."
    mock_page1 = MagicMock()
    mock_page1.get_text.return_value = "Page two content here."
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page0, mock_page1]))
    mock_doc.__getitem__ = MagicMock(side_effect=[mock_page0, mock_page1])

    with patch("search.chunker.fitz.open", return_value=mock_doc):
        chunks = chunk_file(Path("book.pdf"), "pdf")

    assert len(chunks) == 2
    for i, chunk in enumerate(chunks):
        assert "chunk_idx" in chunk
        assert "page" in chunk
        assert "chapter" in chunk
        assert "text" in chunk
        assert chunk["chapter"] is None
        assert chunk["page"] == i + 1


def test_pdf_skips_empty_pages():
    mock_doc = MagicMock()
    mock_doc.page_count = 2
    mock_page0 = MagicMock()
    mock_page0.get_text.return_value = "   \n  "
    mock_page1 = MagicMock()
    mock_page1.get_text.return_value = "Real content."
    mock_doc.__getitem__ = MagicMock(side_effect=[mock_page0, mock_page1])

    with patch("search.chunker.fitz.open", return_value=mock_doc):
        chunks = chunk_file(Path("book.pdf"), "pdf")

    assert len(chunks) == 1
    assert chunks[0]["text"] == "Real content."


def test_epub_chunks_have_required_keys(tmp_path):
    # Build a minimal EPUB in memory
    epub_path = tmp_path / "test.epub"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/content.opf", """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>""")
        zf.writestr("OEBPS/ch1.xhtml", "<html><body><h1>Chapter 1</h1><p>First chapter text.</p></body></html>")
        zf.writestr("OEBPS/ch2.xhtml", "<html><body><h1>Chapter 2</h1><p>Second chapter text.</p></body></html>")
    epub_path.write_bytes(buf.getvalue())

    chunks = chunk_file(epub_path, "epub")
    assert len(chunks) == 2
    for chunk in chunks:
        assert "chunk_idx" in chunk
        assert "page" in chunk
        assert "chapter" in chunk
        assert "text" in chunk
        assert chunk["page"] is None
    assert "Chapter 1" in chunks[0]["chapter"] or "First chapter" in chunks[0]["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/marcello/devel-with-claude/tg-downloader
uv run pytest tests/test_search_chunker.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'search'`

- [ ] **Step 3: Create `search/__init__.py`**

```python
# search/__init__.py
```

(empty file — makes `search` a package)

- [ ] **Step 4: Create `search/chunker.py`**

```python
# search/chunker.py
"""Text extraction and chunking for PDF and EPUB files.

Returns one chunk per PDF page; one chunk per EPUB chapter content file.
Each chunk dict: {chunk_idx, page, chapter, text}
page is 1-based for PDF, None for EPUB.
chapter is None for PDF, heading text for EPUB.
"""
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED_EXTS = {"pdf", "epub"}
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|nbsp|quot|apos);")
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"', "&apos;": "'"}


def chunk_file(path: Path, ext: str) -> list[dict]:
    if ext == "pdf":
        return _chunk_pdf(path)
    if ext == "epub":
        return _chunk_epub(path)
    return []


def _chunk_pdf(path: Path) -> list[dict]:
    import fitz
    try:
        doc = fitz.open(str(path))
    except Exception:
        return []
    chunks = []
    for page_num in range(doc.page_count):
        text = doc[page_num].get_text().strip()
        if not text:
            continue
        chunks.append({
            "chunk_idx": len(chunks),
            "page": page_num + 1,
            "chapter": None,
            "text": text,
        })
    return chunks


def _chunk_epub(path: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf(zf)
            if not opf_path:
                return []
            spine_hrefs = _parse_spine(zf, opf_path)
            chunks = []
            for href in spine_hrefs:
                try:
                    html = zf.read(href).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                text = _html_to_text(html)
                if not text.strip():
                    continue
                heading = _extract_heading(html)
                chunks.append({
                    "chunk_idx": len(chunks),
                    "page": None,
                    "chapter": heading,
                    "text": text.strip(),
                })
            return chunks
    except Exception:
        return []


def _find_opf(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _parse_spine(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    try:
        root = ET.fromstring(zf.read(opf_path))
    except Exception:
        return []
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    id_to_href: dict[str, str] = {}
    for item in root.findall(".//{http://www.idpf.org/2007/opf}item"):
        item_id = item.get("id", "")
        href = item.get("href", "")
        mt = item.get("media-type", "")
        if "xhtml" in mt or "html" in mt:
            id_to_href[item_id] = base + href
    hrefs = []
    for itemref in root.findall(".//{http://www.idpf.org/2007/opf}itemref"):
        idref = itemref.get("idref", "")
        if idref in id_to_href:
            hrefs.append(id_to_href[idref])
    return hrefs


def _extract_heading(html: str) -> str | None:
    m = _HEADING_RE.search(html)
    if m:
        return _TAG_RE.sub("", m.group(1)).strip() or None
    return None


def _html_to_text(html: str) -> str:
    # Strip tags, decode basic entities
    text = _TAG_RE.sub(" ", html)
    text = _ENTITY_RE.sub(lambda m: _ENTITY_MAP.get(m.group(0), m.group(0)), text)
    # Collapse whitespace
    return re.sub(r"\s+", " ", text).strip()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_search_chunker.py -v
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add search/__init__.py search/chunker.py tests/test_search_chunker.py
git commit -m "feat(search): add chunker for PDF and EPUB text extraction"
```

---

### Task 2: Add FTS5 schema and methods to `db.py`

**Files:**
- Modify: `db.py`
- Create: `tests/test_search_db.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_search_db.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_search_db.py -v 2>&1 | head -30
```

Expected: `AttributeError: 'Database' object has no attribute 'search_fts_index_file'`

- [ ] **Step 3: Add FTS5 table to `SCHEMA` in `db.py`**

In `db.py`, find the `SCHEMA` string and add after the last `CREATE TABLE` statement (before the closing `"""`):

```python
    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
        text,
        media_id           UNINDEXED,
        chunk_idx          UNINDEXED,
        page               UNINDEXED,
        chapter            UNINDEXED,
        filename           UNINDEXED,
        channel_identifier UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    );
```

- [ ] **Step 4: Add FTS5 methods to `Database` class in `db.py`**

Add these four methods to the `Database` class (place them after `mark_indexed`):

```python
def search_fts_index_file(
    self,
    media_id: int,
    chunks: list[dict],
    filename: str,
    channel_identifier: str = "",
) -> None:
    with self._conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
        conn.executemany(
            """INSERT INTO search_fts
               (text, media_id, chunk_idx, page, chapter, filename, channel_identifier)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    c["text"],
                    str(media_id),
                    c["chunk_idx"],
                    c.get("page"),
                    c.get("chapter"),
                    filename,
                    channel_identifier,
                )
                for c in chunks
            ],
        )
        conn.commit()

def search_fts_delete_file(self, media_id: int) -> None:
    with self._conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
        conn.commit()

def search_fts_query(
    self, q: str, top_k: int = 8, channel_identifier: str = ""
) -> list[dict]:
    with self._conn() as conn:
        if channel_identifier:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ? AND channel_identifier = ?
                   ORDER BY rank
                   LIMIT ?""",
                (q, channel_identifier, top_k),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (q, top_k),
            ).fetchall()
        return [
            {
                "media_id": int(r["media_id"]),
                "filename": r["filename"],
                "page": r["page"],
                "chapter": r["chapter"],
                "text": r["text"],
            }
            for r in rows
        ]

def search_fts_missing_media_ids(self) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            """SELECT m.id AS media_id, m.filename, m.ext,
                      m.local_path, c.identifier AS channel_identifier
               FROM media_messages m
               JOIN channels c ON m.channel_id = c.id
               WHERE m.status = 'downloaded'
                 AND m.ext IN ('pdf', 'epub')
                 AND CAST(m.id AS TEXT) NOT IN (
                     SELECT DISTINCT media_id FROM search_fts
                 )"""
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 5: Update `mark_discarded` to also delete from `search_fts`**

Find `mark_discarded` in `db.py` and update it to also delete from `search_fts` in the same transaction:

```python
def mark_discarded(self, media_id: int, local_path: str | None = None) -> None:
    with self._conn() as conn:
        if local_path:
            conn.execute(
                "UPDATE media_messages SET status='discarded', local_path=NULL WHERE id=?",
                (media_id,),
            )
        else:
            conn.execute(
                "UPDATE media_messages SET status='discarded' WHERE id=?",
                (media_id,),
            )
        conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
        conn.commit()
```

- [ ] **Step 6: Update `mark_expired` to also delete from `search_fts`**

Find `mark_expired` in `db.py` and add the FTS5 delete:

```python
def mark_expired(self, media_id: int) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE media_messages SET status='expired', local_path=NULL WHERE id=?",
            (media_id,),
        )
        conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
        conn.commit()
```

- [ ] **Step 7: Run all db tests to verify they pass**

```bash
uv run pytest tests/test_search_db.py tests/test_db_rag.py -v
```

Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add db.py tests/test_search_db.py
git commit -m "feat(db): add FTS5 search_fts table and query methods"
```

---

### Task 3: Create `search/indexer.py`

**Files:**
- Create: `search/indexer.py`

This module is the single entry point called by `downloader.py`, `listener.py`, and `main.py`. It glues chunker + db together.

- [ ] **Step 1: Create `search/indexer.py`**

```python
# search/indexer.py
"""High-level indexing entry point.

index_file(db, media_id, filepath, ext, filename, channel_identifier) — chunks and inserts.
Designed to run in a thread (asyncio.to_thread) — all I/O is synchronous.
"""
import logging
from pathlib import Path

from db import Database
from search.chunker import chunk_file, SUPPORTED_EXTS

log = logging.getLogger(__name__)


def index_file(
    db: Database,
    media_id: int,
    filepath: str | Path,
    ext: str,
    filename: str,
    channel_identifier: str = "",
) -> bool:
    if ext not in SUPPORTED_EXTS:
        return False
    path = Path(filepath)
    if not path.exists():
        log.warning(f"index_file: path not found: {path}")
        return False
    chunks = chunk_file(path, ext)
    if not chunks:
        log.debug(f"index_file: no chunks extracted from {filename}")
        return False
    db.search_fts_index_file(
        media_id=media_id,
        chunks=chunks,
        filename=filename,
        channel_identifier=channel_identifier,
    )
    log.info(f"Indexed {len(chunks)} chunk(s) for {filename!r} (media_id={media_id})")
    return True
```

- [ ] **Step 2: Verify import works**

```bash
uv run python -c "from search.indexer import index_file; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add search/indexer.py
git commit -m "feat(search): add indexer entry point"
```

---

### Task 4: Create `search/generator.py`

**Files:**
- Create: `search/generator.py`

- [ ] **Step 1: Create `search/generator.py`**

```python
# search/generator.py
"""Stream an AI answer via Claude Haiku API using FTS5 chunks as context."""
from typing import AsyncIterator

import anthropic

_SYSTEM = (
    "You are a helpful librarian assistant. "
    "Answer the user's question using only the provided book excerpts. "
    "Be concise. Cite sources by their [number] when you reference them."
)


async def generate(
    query: str,
    chunks: list[dict],
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> AsyncIterator[str]:
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        loc = f"page {chunk['page']}" if chunk.get("page") else f"chapter: {chunk.get('chapter', '?')}"
        context_parts.append(f"[{i}] {chunk['filename']} ({loc}):\n{chunk['text']}")
    context = "\n\n---\n\n".join(context_parts)
    user_message = f"Context:\n{context}\n\nQuestion: {query}"

    client = anthropic.AsyncAnthropic(api_key=api_key)
    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
```

- [ ] **Step 2: Verify import works**

```bash
uv run python -c "from search.generator import generate; print('ok')"
```

Expected: `ok` (the `anthropic` package must already be installed in the next task — if this fails with `ModuleNotFoundError: No module named 'anthropic'`, do Task 10 Step 1 first, then return here)

- [ ] **Step 3: Commit**

```bash
git add search/generator.py
git commit -m "feat(search): add Claude Haiku streaming generator"
```

---

### Task 5: Update `downloader.py`

**Files:**
- Modify: `downloader.py`

The `download_item` function currently accepts an `indexer=None` parameter from the old RAG stack. Remove it; always call the new FTS5 indexer after a successful download.

- [ ] **Step 1: Read the current `downloader.py` to find exact lines**

```bash
grep -n "indexer\|_index_async\|index_file\|rag" downloader.py
```

- [ ] **Step 2: Replace `_index_async` and update `download_item` signature**

Find the `_index_async` function and replace it:

```python
async def _index_async(db: Database, media_id: int, filepath: str, ext: str, filename: str, channel_identifier: str = "") -> None:
    from search.indexer import index_file
    try:
        await asyncio.to_thread(index_file, db, media_id, filepath, ext, filename, channel_identifier)
    except Exception as exc:
        log.warning(f"FTS5 indexing failed for {filename!r}: {exc}")
```

Remove `indexer=None` from `download_item`'s signature and all callers inside `downloader.py`.

Find the block inside `download_item` that calls `_index_async` conditionally (guarded by `if indexer is not None:`) and replace it with an unconditional call:

```python
asyncio.create_task(_index_async(
    db, media_id, str(local_path), ext,
    item.get("filename", local_path.name),
    item.get("channel_identifier", ""),
))
```

- [ ] **Step 3: Verify no references to old `indexer` parameter remain**

```bash
grep -n "indexer" downloader.py
```

Expected: no output (or only comments)

- [ ] **Step 4: Commit**

```bash
git add downloader.py
git commit -m "feat(downloader): wire FTS5 auto-indexing after download"
```

---

### Task 6: Update `listener.py`

**Files:**
- Modify: `listener.py`

Remove the `indexer = None` stub and its comment; add the startup heal step that indexes any downloaded files missing from `search_fts`.

- [ ] **Step 1: Remove `indexer = None` and all `indexer` parameters**

In `run_listener`, remove:

```python
# Auto-indexing on download is disabled in the listener: loading sentence-transformers
# alongside Telethon inside a running asyncio event loop causes segfaults on ARM.
# Run `tgdctl index` separately to index downloaded files.
indexer = None
```

Remove the `indexer` argument from every call to `_flush_pending`, `_heal_missing`, `_backfill_missed`, and `_handle`. Remove `indexer` from the signatures of all those functions and their internal `download_item` calls. (`download_item` no longer accepts `indexer`.)

- [ ] **Step 2: Add startup heal for search index**

Add this function to `listener.py`:

```python
async def _heal_search_index(db: Database) -> None:
    missing = db.search_fts_missing_media_ids()
    if not missing:
        return
    log.info(f"Search index heal: {len(missing)} file(s) not yet indexed, indexing in background...")
    from search.indexer import index_file
    ok = 0
    for item in missing:
        try:
            result = await asyncio.to_thread(
                index_file, db,
                item["media_id"], item["local_path"], item["ext"],
                item["filename"], item.get("channel_identifier", ""),
            )
            if result:
                ok += 1
        except Exception as exc:
            log.warning(f"Search heal failed for {item['filename']!r}: {exc}")
    log.info(f"Search index heal complete: {ok}/{len(missing)} indexed")
```

- [ ] **Step 3: Call `_heal_search_index` in `run_listener`**

After the three startup steps (`_flush_pending`, `_heal_missing`, `_backfill_missed`), add:

```python
asyncio.create_task(_heal_search_index(db))
```

- [ ] **Step 4: Verify no `indexer` references remain**

```bash
grep -n "indexer\|sentence.transformer\|rag\." listener.py
```

Expected: no output

- [ ] **Step 5: Commit**

```bash
git add listener.py
git commit -m "feat(listener): remove old RAG indexer; add FTS5 startup heal"
```

---

### Task 7: Update `main.py`

**Files:**
- Modify: `main.py`

Replace `cmd_index` and `cmd_ask` to use FTS5 and Claude API instead of ChromaDB/Ollama.

- [ ] **Step 1: Find the current cmd_index and cmd_ask in main.py**

```bash
grep -n "def cmd_index\|def cmd_ask\|rag\.\|Indexer\|Ollama\|RAG_" main.py | head -40
```

- [ ] **Step 2: Replace `cmd_index`**

Find the `cmd_index` function and replace its entire body:

```python
def cmd_index(args) -> None:
    from search.indexer import index_file
    db = _open_db()
    missing = db.search_fts_missing_media_ids()
    if not missing:
        print("All downloaded files are already indexed.")
        return
    print(f"Indexing {len(missing)} file(s)...")
    ok = 0
    for item in missing:
        result = index_file(
            db,
            item["media_id"],
            item["local_path"],
            item["ext"],
            item["filename"],
            item.get("channel_identifier", ""),
        )
        if result:
            ok += 1
            print(f"  [ok] {item['filename']}")
        else:
            print(f"  [skip] {item['filename']}")
    print(f"Done: {ok}/{len(missing)} indexed.")
```

- [ ] **Step 3: Replace `cmd_ask`**

Find the `cmd_ask` function and replace its entire body:

```python
def cmd_ask(args) -> None:
    import asyncio
    import os
    db = _open_db()
    cfg = _load_config()
    top_k = cfg.get("search", {}).get("top_k", 8)

    if args.sources_only:
        chunks = db.search_fts_query(args.query, top_k=top_k)
        if not chunks:
            print("No relevant content found.")
            return
        for i, c in enumerate(chunks, 1):
            loc = f"p.{c['page']}" if c.get("page") else c.get("chapter", "")
            print(f"[{i}] {c['filename']} ({loc})")
            print(f"    {c['text'][:200]}")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        return
    chunks = db.search_fts_query(args.query, top_k=top_k)
    if not chunks:
        print("No relevant content found in the library.")
        return

    async def _stream():
        from search.generator import generate
        async for token in generate(args.query, chunks, api_key):
            print(token, end="", flush=True)
        print()

    asyncio.run(_stream())
```

- [ ] **Step 4: Remove RAG config checks from `cmd_index` and `cmd_ask` argument parsing**

Ensure no code blocks check `cfg["rag"]["enabled"]` inside these commands. Search:

```bash
grep -n "rag\[.enabled.\]\|rag\.enabled\|RAG_ENABLED" main.py
```

Remove any such guards.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(main): wire cmd_index and cmd_ask to FTS5 and Claude API"
```

---

### Task 8: Update `webui/app.py`

**Files:**
- Modify: `webui/app.py`

Remove the lifespan model loading; replace RAG endpoints; add FTS5 delete to the discard endpoint.

- [ ] **Step 1: Remove the lifespan context manager and all RAG globals**

Delete these lines from the top of `webui/app.py`:

```python
RAG_INDEX_PATH = os.environ.get("RAG_INDEX_PATH", "/app/data/rag_index")
RAG_EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")
RAG_OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://host.docker.internal:11434")
RAG_OLLAMA_MODEL = os.environ.get("RAG_OLLAMA_MODEL", "phi3:mini")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))

_rag_indexer = None


def _get_indexer():
    return _rag_indexer


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _rag_indexer
    try:
        from rag.indexer import Indexer
        import asyncio
        _rag_indexer = await asyncio.to_thread(
            Indexer, {"index_path": RAG_INDEX_PATH, "embed_model": RAG_EMBED_MODEL}
        )
    except Exception as exc:
        log.warning(f"RAG indexer unavailable at startup: {exc}")
    yield
```

Replace `app = FastAPI(lifespan=_lifespan)` with `app = FastAPI()`.

Add at the top (after existing imports):

```python
SEARCH_TOP_K = int(os.environ.get("SEARCH_TOP_K", "8"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
```

Also remove the unused `asynccontextmanager` import from the import block.

- [ ] **Step 2: Add `/api/search` endpoint**

Replace the old `/api/rag/search` endpoint with:

```python
@app.get("/api/search")
def fts_search(q: str, channel: str = "", top_k: int = 0):
    if not q.strip():
        return {"chunks": []}
    conn = _db()
    try:
        k = top_k or SEARCH_TOP_K
        if channel:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ? AND channel_identifier = ?
                   ORDER BY rank LIMIT ?""",
                (q, channel, k),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (q, k),
            ).fetchall()
        chunks = [
            {
                "media_id": int(r["media_id"]),
                "filename": r["filename"],
                "page": r["page"],
                "chapter": r["chapter"],
                "text": r["text"],
            }
            for r in rows
        ]
        return {"chunks": chunks}
    except Exception as exc:
        log.warning(f"FTS5 search error for {q!r}: {exc}")
        return {"chunks": [], "error": str(exc)}
    finally:
        conn.close()
```

- [ ] **Step 3: Add `/api/ask` endpoint**

Replace the old `/api/rag/ask` endpoint with:

```python
class _AskRequest(BaseModel):
    query: str
    channel: str = ""
    top_k: int = 0


@app.post("/api/ask")
async def fts_ask(req: _AskRequest):
    from fastapi.responses import StreamingResponse
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    conn = _db()
    try:
        k = req.top_k or SEARCH_TOP_K
        if req.channel:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ? AND channel_identifier = ?
                   ORDER BY rank LIMIT ?""",
                (req.query, req.channel, k),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT media_id, filename, page, chapter,
                          snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
                   FROM search_fts
                   WHERE search_fts MATCH ? ORDER BY rank LIMIT ?""",
                (req.query, k),
            ).fetchall()
        chunks = [dict(r) for r in rows]
    finally:
        conn.close()

    if not chunks:
        return {"answer": "No relevant content found in the library.", "chunks": []}

    from search.generator import generate

    async def _stream():
        async for token in generate(req.query, chunks, ANTHROPIC_API_KEY):
            yield token

    return StreamingResponse(_stream(), media_type="text/plain")
```

- [ ] **Step 4: Add FTS5 delete to the discard endpoint**

In the `discard_files` function, inside the `for file_id in req.ids:` loop, after the existing `conn.execute("UPDATE media_messages SET status='discarded' ...")`, add:

```python
conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(file_id),))
```

- [ ] **Step 5: Verify the file has no remaining RAG references**

```bash
grep -n "rag\|RAG\|Ollama\|sentence.transformer\|chromadb\|_rag_indexer\|_lifespan" webui/app.py
```

Expected: no output

- [ ] **Step 6: Commit**

```bash
git add webui/app.py
git commit -m "feat(webui): replace RAG endpoints with FTS5 search and Claude API ask"
```

---

### Task 9: Update `webui/static/index.html`

**Files:**
- Modify: `webui/static/index.html`

Three changes: update the two endpoint URLs and fix the "Ollama" error message. Also update the snippet rendering to handle `<<term>>` highlight markers safely using DOM APIs (no `innerHTML`).

- [ ] **Step 1: Find the three strings to replace**

```bash
grep -n "rag/search\|rag/ask\|Ollama\|innerHTML\|chunk\.text" webui/static/index.html
```

- [ ] **Step 2: Update endpoint URLs**

Replace `/api/rag/search` with `/api/search` and `/api/rag/ask` with `/api/ask` (two replacements).

- [ ] **Step 3: Update the error message**

Find the line with `'Ask failed. Is Ollama running?'` (or similar) and replace it with:

```
'Ask AI failed. Check that ANTHROPIC_API_KEY is configured.'
```

- [ ] **Step 4: Update snippet rendering to use safe DOM manipulation**

Find the JS that renders chunk text (currently sets `txt.textContent = chunk.text` or similar). Replace it with a helper function that safely renders `<<term>>` markers as `<mark>` elements without using `innerHTML`:

```javascript
function renderSnippet(container, raw) {
  container.textContent = '';
  var parts = raw.split(/<<|>>/);
  var inMark = false;
  parts.forEach(function(part) {
    if (inMark) {
      var mark = document.createElement('mark');
      mark.textContent = part;
      container.appendChild(mark);
    } else {
      container.appendChild(document.createTextNode(part));
    }
    inMark = !inMark;
  });
}
```

Then replace the call site (e.g., `txt.textContent = chunk.text`) with:

```javascript
renderSnippet(txt, chunk.text || '');
```

- [ ] **Step 5: Verify no RAG endpoint strings remain**

```bash
grep -n "rag/search\|rag/ask\|Ollama running\|innerHTML" webui/static/index.html
```

Expected: no output

- [ ] **Step 6: Commit**

```bash
git add webui/static/index.html
git commit -m "feat(webui): update JS to use FTS5 endpoints and safe snippet rendering"
```

---

### Task 10: Update dependencies and configuration files

**Files:**
- Modify: `pyproject.toml`
- Modify: `config.yaml.example`
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Modify: `webui/Dockerfile`

- [ ] **Step 1: Update `pyproject.toml`**

Remove `chromadb` and `sentence-transformers` from `dependencies`. Add `anthropic>=0.40`. Change `packages = ["rag"]` to `packages = ["search"]`:

```toml
# In [project] dependencies, remove:
#   "chromadb>=0.6",
#   "sentence-transformers>=3.0",
# Add:
    "anthropic>=0.40",

# In [tool.uv] or wherever packages is defined, change:
packages = ["search"]
```

- [ ] **Step 2: Run `uv sync` to update the lock file**

```bash
uv sync
```

Expected: resolves successfully; `anthropic` installed; `chromadb` and `sentence-transformers` removed.

- [ ] **Step 3: Verify `anthropic` is importable**

```bash
uv run python -c "import anthropic; print(anthropic.__version__)"
```

Expected: prints a version number (e.g., `0.40.0`)

- [ ] **Step 4: Now verify `search/generator.py` import works**

```bash
uv run python -c "from search.generator import generate; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Update `config.yaml.example`**

Find the `rag:` block:

```yaml
rag:
  enabled: false
  embed_model: "all-MiniLM-L6-v2"
  ollama_url: "http://host.docker-internal:11434"
  ollama_model: "phi3:mini"
  top_k: 5
```

Replace with:

```yaml
search:
  top_k: 8           # chunks returned per search/ask query
```

- [ ] **Step 6: Update `docker-compose.yml`**

In the `tg-downloader` service, remove `CUDA_VISIBLE_DEVICES=`.

In the `webui` service, remove all PyTorch workaround env vars:
```yaml
# Remove these four lines:
- CUDA_VISIBLE_DEVICES=
- OMP_NUM_THREADS=1
- OPENBLAS_NUM_THREADS=1
- MKL_NUM_THREADS=1
- TOKENIZERS_PARALLELISM=false
```

Add to the `webui` service environment:
```yaml
- ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

Also remove `CUDA_VISIBLE_DEVICES=` from the `tg-downloader` service.

- [ ] **Step 7: Update `Dockerfile` (main container)**

Find `COPY rag/ ./rag/` and change to `COPY search/ ./search/`.

- [ ] **Step 8: Update `webui/Dockerfile`**

Find `COPY rag/ ./rag/` and change to `COPY search/ ./search/`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock config.yaml.example docker-compose.yml Dockerfile webui/Dockerfile
git commit -m "chore: replace rag deps with anthropic; update docker and config for FTS5"
```

---

### Task 11: Delete `rag/` directory and old tests

**Files:**
- Delete: `rag/` (entire directory)
- Delete: `tests/test_chunker.py`

- [ ] **Step 1: Run existing tests to ensure none import from `rag/` at this point**

```bash
uv run pytest tests/ -v --ignore=tests/test_chunker.py 2>&1 | tail -20
```

Expected: all tests PASS (or only pre-existing failures unrelated to this feature)

- [ ] **Step 2: Delete `rag/` directory**

```bash
rm -rf rag/
```

- [ ] **Step 3: Delete `tests/test_chunker.py`**

```bash
rm tests/test_chunker.py
```

- [ ] **Step 4: Run all tests to confirm nothing broke**

```bash
uv run pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove rag/ package and old chunker tests"
```

---

### Task 12: Final verification

**Files:** none (read-only verification)

- [ ] **Step 1: Verify all success criteria from the spec**

```bash
# 1. No sentence-transformers or chromadb in the installed packages
uv run pip list 2>/dev/null | grep -iE "sentence|chromadb|torch" || echo "CLEAN — no ML packages"

# 2. search/ package importable
uv run python -c "from search.chunker import chunk_file; from search.indexer import index_file; from search.generator import generate; print('search package OK')"

# 3. Run the full test suite
uv run pytest tests/ -v
```

- [ ] **Step 2: Check for any remaining references to old stack**

```bash
grep -rn "rag\.\|from rag\|chromadb\|sentence.transformer\|Ollama\|phi3" \
    --include="*.py" \
    --exclude-dir=".venv" \
    --exclude-dir=".git" \
    . | grep -v "test_db_rag\|# previously\|# old"
```

Expected: no output (or only comments documenting what was removed)

- [ ] **Step 3: Build Docker images**

```bash
docker compose build 2>&1 | tail -20
```

Expected: both images build successfully

- [ ] **Step 4: Create `.env` file for the API key (if not already present)**

```bash
test -f .env || echo "ANTHROPIC_API_KEY=your-key-here" > .env
echo ".env already in .gitignore?"
grep -q "^\.env" .gitignore && echo "yes" || echo "add .env to .gitignore"
```

If `.env` is not in `.gitignore`, add it:

```bash
echo ".env" >> .gitignore
git add .gitignore
git commit -m "chore: ensure .env is gitignored"
```

- [ ] **Step 5: Start services and smoke-test**

```bash
docker compose up -d
sleep 5
# Test search endpoint
curl -s "http://localhost:8090/api/search?q=test" | python3 -m json.tool
```

Expected: `{"chunks": []}` (empty — no content indexed yet) or actual results if the library has indexed content.

- [ ] **Step 6: Run `tgdctl index` to populate the search index**

```bash
uv run tgdctl index
```

Expected: indexes PDF and EPUB files; prints progress.

- [ ] **Step 7: Test search returns results**

```bash
curl -s "http://localhost:8090/api/search?q=the&top_k=3" | python3 -m json.tool
```

Expected: JSON with `chunks` array containing filename, page/chapter, and text excerpt.

- [ ] **Step 8: Final commit if any loose changes**

```bash
git status
# If clean, no commit needed
```

---

## Done

The RAG stack is fully replaced. The Pi no longer loads PyTorch or chromadb; no Ollama is needed. FTS5 search starts with zero RAM overhead. The `ANTHROPIC_API_KEY` env var gates AI generation — search works without it.
