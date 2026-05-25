# RAG System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local RAG system to tg-downloader so downloaded PDFs/EPUBs can be searched semantically and queried with Ollama.

**Architecture:** A new `rag/` Python package handles chunking (PyMuPDF + zipfile), embedding (sentence-transformers), vector storage (ChromaDB), retrieval, and Ollama generation. Auto-indexing fires after each successful download. Two new CLI commands (`tgdctl index`, `tgdctl ask`) and a web UI search/ask panel are wired on top.

**Tech Stack:** `chromadb>=0.6`, `sentence-transformers>=3.0`, `httpx>=0.27`, PyMuPDF (already present), Python 3.11 async

---

## File map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `rag/__init__.py` | Package marker |
| Create | `rag/chunker.py` | file to `[{text, page, chapter, chunk_idx}]` |
| Create | `rag/indexer.py` | `Indexer` — embeds chunks, reads/writes ChromaDB |
| Create | `rag/retriever.py` | `retrieve()` — query to top-k chunks |
| Create | `rag/generator.py` | `generate()` — chunks + query to Ollama streaming |
| Create | `tests/test_chunker.py` | Unit tests for chunker |
| Create | `tests/test_db_rag.py` | Unit tests for new DB methods |
| Modify | `pyproject.toml` | Add deps + `packages = ["rag"]` + dev pytest |
| Modify | `config.py` | Add `_apply_rag_defaults()` |
| Modify | `config.yaml` + `config.yaml.example` | Add `rag:` section |
| Modify | `db.py` | `indexed_at` migration + `get_unindexed_downloaded` + `mark_indexed` |
| Modify | `downloader.py` | Accept `indexer` kwarg; schedule `_index_async` after download |
| Modify | `listener.py` | Construct `Indexer` if enabled; thread through all call sites |
| Modify | `main.py` | Add `index` + `ask` subcommands |
| Modify | `tgdctl.py` | Add `index` + `ask` proxy commands |
| Modify | `webui/Dockerfile` | Change build context; add `COPY rag/` |
| Modify | `docker-compose.yml` | Point webui build context to `.` |
| Modify | `webui/app.py` | Add `/api/rag/search` and `/api/rag/ask` endpoints |
| Modify | `webui/static/index.html` | Add search/ask panel + JS |

---

## Task 1: Dependencies, config, package declaration

**Files:**
- Modify: `pyproject.toml`
- Modify: `config.py`
- Modify: `config.yaml`
- Modify: `config.yaml.example`

- [ ] **Step 1: Add dependencies and package declaration to `pyproject.toml`**

Replace the entire file with:

```toml
[project]
name = "tg-downloader"
version = "0.1.0"
description = "Scrape Telegram channels for media files and interactively select which ones to download"
requires-python = ">=3.11"
dependencies = [
    "chromadb>=0.6",
    "httpx>=0.27",
    "inquirerpy>=0.3.4",
    "langdetect>=1.0.9",
    "pymupdf>=1.25",
    "pyyaml>=6.0.3",
    "rich>=15.0.0",
    "sentence-transformers>=3.0",
    "telethon>=1.43.2",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[project.scripts]
tg-downloader = "main:main"
tgdctl = "tgdctl:main"

[tool.uv]
package = true

[tool.setuptools]
py-modules = ["main", "config", "db", "lang_filter", "listener", "ui", "downloader", "utils", "tgdctl"]
packages = ["rag"]
```

- [ ] **Step 2: Run `uv sync` to install new deps and verify it completes**

```bash
uv sync
```

Expected: resolves and installs chromadb, sentence-transformers (+ torch), httpx. Takes several minutes on first run due to PyTorch. No errors.

- [ ] **Step 3: Add `_apply_rag_defaults()` to `config.py`**

Add this function and call it from `_apply_defaults`:

```python
def _apply_defaults(raw: dict) -> None:
    raw["telegram"].setdefault("session_file", "data/tg_session")

    dl = raw.setdefault("download", {})
    dl.setdefault("destination", "data/downloads")
    dl["destination"] = str(Path(dl["destination"]).expanduser())
    dl.setdefault("retention_days", 365)
    dl.setdefault("concurrent_downloads", 1)

    filters = raw.setdefault("filters", {})
    filters.setdefault("extensions", [])
    filters["extensions"] = [e.lower().lstrip(".") for e in filters["extensions"]]
    filters.setdefault("discard_topics", {})
    filters.setdefault("topic_min_matches", 2)
    filters.setdefault("topic_min_keyword_occurrences", 1)

    _apply_rag_defaults(raw)


def _apply_rag_defaults(raw: dict) -> None:
    rag = raw.setdefault("rag", {})
    rag.setdefault("enabled", False)
    rag.setdefault("index_path", "data/rag_index")
    rag.setdefault("embed_model", "all-MiniLM-L6-v2")
    rag.setdefault("ollama_url", "http://host.docker.internal:11434")
    rag.setdefault("ollama_model", "phi3:mini")
    rag.setdefault("top_k", 5)
```

- [ ] **Step 4: Add `rag:` section to `config.yaml`**

After the existing `filters:` section, append:

```yaml
rag:
  enabled: true
  index_path: "data/rag_index"
  embed_model: "all-MiniLM-L6-v2"
  ollama_url: "http://host.docker.internal:11434"   # Pi host from inside Docker container
  ollama_model: "phi3:mini"
  top_k: 5
```

- [ ] **Step 5: Add identical `rag:` section to `config.yaml.example`**

Same block, placed after the `filters:` section with explanatory comments:

```yaml
# Optional: RAG (Retrieval-Augmented Generation) -- semantic search + AI answers.
# Requires Ollama running on the host: https://ollama.com/
# After enabling, run 'tgdctl index' to index existing files.
rag:
  enabled: false                                     # set to true to activate
  index_path: "data/rag_index"                       # ChromaDB storage (inside Docker volume)
  embed_model: "all-MiniLM-L6-v2"                   # local embedding model (~22MB)
  ollama_url: "http://host.docker.internal:11434"   # Pi host from inside Docker container
  ollama_model: "phi3:mini"                          # any model installed in Ollama
  top_k: 5                                           # chunks returned per query
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml config.py config.yaml config.yaml.example
git commit -m "feat(rag): add deps, config section, package declaration"
```

---

## Task 2: Database migration — `indexed_at` column

**Files:**
- Modify: `db.py`
- Create: `tests/test_db_rag.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_rag.py`:

```python
import tempfile
from pathlib import Path
import pytest
from db import Database


def _make_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_channel(db: Database) -> int:
    with db._conn() as conn:
        cur = conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (1, '@test', 'Test')"
        )
        return cur.lastrowid


def _insert_downloaded(db: Database, channel_id: int, msg_id: int = 1) -> int:
    mid = db.save_media_message(
        channel_id=channel_id,
        message_id=msg_id,
        filename=f"file{msg_id}.pdf",
        size=1000,
        mime_type="application/pdf",
        ext="pdf",
        date="2026-01-01T00:00:00",
        caption="",
    )
    db.mark_downloaded(mid, f"/downloads/file{msg_id}.pdf")
    return mid


def test_get_unindexed_downloaded_returns_downloaded_records(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    rows = db.get_unindexed_downloaded()
    assert len(rows) == 1
    assert rows[0]["id"] == mid


def test_mark_indexed_removes_from_unindexed(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    db.mark_indexed(mid)
    assert db.get_unindexed_downloaded() == []


def test_mark_indexed_sets_timestamp(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    mid = _insert_downloaded(db, ch)
    db.mark_indexed(mid)
    with db._conn() as conn:
        row = conn.execute("SELECT indexed_at FROM media_messages WHERE id=?", (mid,)).fetchone()
    assert row["indexed_at"] is not None


def test_get_unindexed_excludes_pending(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    db.save_media_message(
        channel_id=ch, message_id=99, filename="pending.pdf",
        size=500, mime_type="application/pdf", ext="pdf",
        date="2026-01-01T00:00:00", caption="",
    )
    assert db.get_unindexed_downloaded() == []


def test_get_unindexed_returns_multiple(tmp_path):
    db = _make_db(tmp_path)
    ch = _insert_channel(db)
    _insert_downloaded(db, ch, msg_id=1)
    _insert_downloaded(db, ch, msg_id=2)
    assert len(db.get_unindexed_downloaded()) == 2
```

- [ ] **Step 2: Run tests — expect failure (method not found)**

```bash
uv run pytest tests/test_db_rag.py -v
```

Expected: `AttributeError: 'Database' object has no attribute 'get_unindexed_downloaded'`

- [ ] **Step 3: Add migration + methods to `db.py`**

Add `"ALTER TABLE media_messages ADD COLUMN indexed_at TEXT"` to `_MIGRATIONS`:

```python
_MIGRATIONS = [
    "ALTER TABLE media_messages ADD COLUMN downloaded_at TEXT",
    "ALTER TABLE media_messages ADD COLUMN language TEXT",
    "ALTER TABLE media_messages ADD COLUMN file_hash TEXT",
    "ALTER TABLE media_messages ADD COLUMN indexed_at TEXT",
]
```

Add these two methods to the `Database` class (after `mark_expired`):

```python
def get_unindexed_downloaded(self) -> list[dict]:
    """Returns downloaded files with no indexed_at timestamp."""
    with self._conn() as conn:
        rows = conn.execute(
            """SELECT m.*, c.title AS channel_title,
                      c.telegram_id AS channel_telegram_id,
                      c.identifier AS channel_identifier
               FROM media_messages m
               JOIN channels c ON m.channel_id = c.id
               WHERE m.status = 'downloaded' AND m.indexed_at IS NULL
               ORDER BY m.downloaded_at DESC, m.date DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

def mark_indexed(self, media_id: int) -> None:
    with self._conn() as conn:
        conn.execute(
            "UPDATE media_messages SET indexed_at=datetime('now') WHERE id=?",
            (media_id,),
        )
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
uv run pytest tests/test_db_rag.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db_rag.py
git commit -m "feat(rag): add indexed_at column and DB methods"
```

---

## Task 3: `rag/chunker.py`

**Files:**
- Create: `rag/__init__.py`
- Create: `rag/chunker.py`
- Create: `tests/test_chunker.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_chunker.py`:

```python
import zipfile
from pathlib import Path
import fitz
import pytest
from rag.chunker import chunk_file, _split_text, SUPPORTED_EXTS


def _make_pdf(tmp_path: Path, pages: list[str]) -> Path:
    path = tmp_path / "test.pdf"
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 72), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(tmp_path: Path, chapters: list[tuple[str, str]]) -> Path:
    path = tmp_path / "test.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        items_xml = "\n".join(
            f'<item id="ch{i}" href="ch{i}.html" media-type="application/xhtml+xml"/>'
            for i in range(len(chapters))
        )
        itemrefs_xml = "\n".join(
            f'<itemref idref="ch{i}"/>' for i in range(len(chapters))
        )
        opf = (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf">'
            f"<manifest>{items_xml}</manifest>"
            f"<spine>{itemrefs_xml}</spine>"
            "</package>"
        )
        zf.writestr("content.opf", opf)
        for i, (title, text) in enumerate(chapters):
            html = f"<html><body><h1>{title}</h1><p>{text}</p></body></html>"
            zf.writestr(f"ch{i}.html", html)
    return path


def test_unsupported_format_returns_empty(tmp_path):
    p = tmp_path / "file.mobi"
    p.write_bytes(b"fake mobi")
    assert chunk_file(p, "mobi") == []


def test_missing_file_returns_empty(tmp_path):
    assert chunk_file(tmp_path / "nonexistent.pdf", "pdf") == []


def test_pdf_basic_chunks(tmp_path):
    text = "First paragraph.\n\nSecond paragraph with more words to reach the minimum."
    pdf = _make_pdf(tmp_path, [text])
    chunks = chunk_file(pdf, "pdf")
    assert len(chunks) >= 1
    for c in chunks:
        assert "text" in c and len(c["text"]) > 0
        assert c["page"] == 1
        assert c["chapter"] is None
        assert isinstance(c["chunk_idx"], int)


def test_pdf_page_numbers_are_sequential(tmp_path):
    pdf = _make_pdf(tmp_path, ["Page one text here " * 5, "Page two text here " * 5])
    chunks = chunk_file(pdf, "pdf")
    pages = [c["page"] for c in chunks]
    assert 1 in pages
    assert 2 in pages


def test_epub_basic_chunks(tmp_path):
    epub = _make_epub(tmp_path, [
        ("Chapter 1", "The quick brown fox jumped over the lazy dog. " * 15),
        ("Chapter 2", "To be or not to be that is the question here. " * 15),
    ])
    chunks = chunk_file(epub, "epub")
    assert len(chunks) >= 2
    for c in chunks:
        assert c["page"] is None
        assert c["chapter"] is not None


def test_chunk_idx_is_sequential(tmp_path):
    epub = _make_epub(tmp_path, [
        ("Ch 1", "Content " * 40),
        ("Ch 2", "Content " * 40),
    ])
    chunks = chunk_file(epub, "epub")
    for expected_idx, chunk in enumerate(chunks):
        assert chunk["chunk_idx"] == expected_idx


def test_split_text_short_returns_single():
    pieces = _split_text("Short text under the limit.")
    assert len(pieces) == 1


def test_split_text_long_splits_with_overlap():
    text = ("word " * 120 + "\n\n") * 2
    pieces = _split_text(text)
    assert len(pieces) >= 2
    for p in pieces:
        assert len(p) >= 10


def test_supported_exts_constant():
    assert "pdf" in SUPPORTED_EXTS
    assert "epub" in SUPPORTED_EXTS
    assert "mobi" not in SUPPORTED_EXTS
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
uv run pytest tests/test_chunker.py -v
```

Expected: `ModuleNotFoundError: No module named 'rag'`

- [ ] **Step 3: Create `rag/__init__.py`**

Create an empty file at `rag/__init__.py`.

- [ ] **Step 4: Create `rag/chunker.py`**

```python
"""Split PDF and EPUB files into overlapping text chunks with source metadata."""

import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import fitz  # PyMuPDF -- already a project dependency

SUPPORTED_EXTS = {"pdf", "epub"}
_CHUNK_SIZE = 1500   # target characters per chunk
_OVERLAP = 150       # characters of overlap between consecutive chunks
_MIN_CHUNK = 100     # fragments shorter than this are dropped


def chunk_file(file_path: Path, ext: str) -> list[dict]:
    """Return chunks for pdf/epub, or [] for unsupported formats or errors.

    Each chunk: {text: str, page: int|None, chapter: str|None, chunk_idx: int}
    """
    if ext not in SUPPORTED_EXTS or not file_path.exists():
        return []
    try:
        if ext == "pdf":
            return _chunk_pdf(file_path)
        if ext == "epub":
            return _chunk_epub(file_path)
    except Exception:
        return []
    return []


# -- PDF -----------------------------------------------------------------------

def _chunk_pdf(path: Path) -> list[dict]:
    doc = fitz.open(str(path))
    raw: list[dict] = []
    for page_num in range(doc.page_count):
        text = doc[page_num].get_text()
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para:
                raw.append({"text": para, "page": page_num + 1, "chapter": None})
    return _merge_and_split(raw)


# -- EPUB ----------------------------------------------------------------------

def _chunk_epub(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        opf_name = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
        opf_dir = "/".join(opf_name.split("/")[:-1]) if opf_name else ""

        spine_items = _parse_opf_spine(zf, opf_name) if opf_name else []

        if spine_items:
            html_files = []
            label_map: dict[str, str] = {}
            for item in spine_items:
                href = item["href"]
                full = f"{opf_dir}/{href}".lstrip("/") if opf_dir else href
                if full in zf.namelist():
                    html_files.append(full)
                    label_map[full] = item["label"]
        else:
            html_files = sorted(
                n for n in zf.namelist()
                if n.lower().endswith((".html", ".xhtml", ".htm"))
            )
            label_map = {}

        raw: list[dict] = []
        for html_path in html_files:
            chapter = label_map.get(html_path) or html_path.split("/")[-1].rsplit(".", 1)[0]
            try:
                text = _strip_html(zf.read(html_path).decode("utf-8", errors="ignore"))
            except Exception:
                continue
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if para:
                    raw.append({"text": para, "page": None, "chapter": chapter})

        return _merge_and_split(raw)


def _parse_opf_spine(zf: zipfile.ZipFile, opf_name: str) -> list[dict]:
    """Return [{"href": ..., "label": ...}, ...] in spine order."""
    try:
        content = zf.read(opf_name).decode("utf-8", errors="ignore")
        root = ET.fromstring(content)
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {
            item.get("id", ""): item.get("href", "")
            for item in root.findall(".//opf:item", ns)
        }
        spine = []
        for itemref in root.findall(".//opf:itemref", ns):
            idref = itemref.get("idref", "")
            if idref in manifest:
                spine.append({"href": manifest[idref], "label": idref})
        return spine
    except Exception:
        return []


# -- Chunking helpers ----------------------------------------------------------

def _merge_and_split(raw: list[dict]) -> list[dict]:
    """Group paragraphs by source location, join them, split into sized chunks."""
    groups: list[tuple[tuple, list[str]]] = []
    for item in raw:
        key = (item["page"], item["chapter"])
        if groups and groups[-1][0] == key:
            groups[-1][1].append(item["text"])
        else:
            groups.append((key, [item["text"]]))

    result: list[dict] = []
    chunk_idx = 0
    for (page, chapter), texts in groups:
        for piece in _split_text("\n\n".join(texts)):
            result.append({
                "text": piece,
                "page": page,
                "chapter": chapter,
                "chunk_idx": chunk_idx,
            })
            chunk_idx += 1
    return result


def _split_text(text: str) -> list[str]:
    """Split text into _CHUNK_SIZE-character pieces with _OVERLAP overlap."""
    if len(text) < _MIN_CHUNK:
        return []
    if len(text) <= _CHUNK_SIZE:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        if end < len(text):
            bp = text.rfind("\n\n", start + _MIN_CHUNK, end)
            if bp == -1:
                bp = text.rfind(" ", start + _MIN_CHUNK, end)
            if bp != -1:
                end = bp

        piece = text[start:end].strip()
        if len(piece) >= _MIN_CHUNK:
            chunks.append(piece)
        start = end - _OVERLAP if end < len(text) else end

    return chunks


# -- HTML stripping ------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self._parts.append(s)

    def text(self) -> str:
        return "\n".join(self._parts)


def _strip_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.text()
```

- [ ] **Step 5: Run tests — expect all pass**

```bash
uv run pytest tests/test_chunker.py -v
```

Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add rag/__init__.py rag/chunker.py tests/test_chunker.py
git commit -m "feat(rag): add chunker with PDF and EPUB support"
```

---

## Task 4: `rag/indexer.py` and `rag/retriever.py`

**Files:**
- Create: `rag/indexer.py`
- Create: `rag/retriever.py`

No unit tests for these — they require live ChromaDB + model; covered by integration in Task 7.

- [ ] **Step 1: Create `rag/indexer.py`**

```python
"""Embed file chunks and store them in ChromaDB."""

import logging
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from rag.chunker import chunk_file

log = logging.getLogger(__name__)

_COLLECTION = "documents"


class Indexer:
    def __init__(self, config: dict) -> None:
        index_path = config.get("index_path", "data/rag_index")
        embed_model = config.get("embed_model", "all-MiniLM-L6-v2")
        Path(index_path).mkdir(parents=True, exist_ok=True)
        self._model = SentenceTransformer(embed_model)
        self._client = chromadb.PersistentClient(path=str(index_path))
        self._col = self._client.get_or_create_collection(
            name=_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(f"RAG index ready: {index_path} ({self._col.count()} chunks indexed)")

    @property
    def collection(self):
        return self._col

    def embed_query(self, query: str) -> list[float]:
        return self._model.encode([query], show_progress_bar=False)[0].tolist()

    def index_file(self, media_id: int, filepath: Path, meta: dict) -> int:
        """Chunk, embed, and upsert a file. Returns number of chunks stored.

        meta must contain: filename, channel_title, channel_identifier, ext.
        Returns 0 if the file is unsupported or missing.
        """
        if not filepath.exists():
            log.warning(f"RAG index: {filepath} not found -- skipping")
            return 0

        ext = meta.get("ext", "")
        chunks = chunk_file(filepath, ext)
        if not chunks:
            log.debug(f"RAG index: no chunks for {filepath.name} (unsupported or empty)")
            return 0

        self.delete_file(media_id)

        ids = [f"{media_id}_{c['chunk_idx']}" for c in chunks]
        texts = [c["text"] for c in chunks]
        metadatas = [
            {
                "media_id": media_id,
                "filename": meta.get("filename", ""),
                "channel_title": meta.get("channel_title", ""),
                "channel_identifier": meta.get("channel_identifier", ""),
                "ext": ext,
                "page": c["page"] if c["page"] is not None else -1,
                "chapter": c["chapter"] or "",
            }
            for c in chunks
        ]
        embeddings = self._model.encode(texts, show_progress_bar=False).tolist()
        self._col.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
        log.debug(f"RAG index: stored {len(chunks)} chunks for {filepath.name}")
        return len(chunks)

    def delete_file(self, media_id: int) -> None:
        """Remove all chunks for media_id from the collection."""
        try:
            existing = self._col.get(where={"media_id": {"$eq": media_id}})
            if existing["ids"]:
                self._col.delete(ids=existing["ids"])
        except Exception as exc:
            log.debug(f"RAG delete_file({media_id}): {exc}")

    def is_indexed(self, media_id: int) -> bool:
        try:
            result = self._col.get(where={"media_id": {"$eq": media_id}}, limit=1)
            return len(result["ids"]) > 0
        except Exception:
            return False
```

- [ ] **Step 2: Create `rag/retriever.py`**

```python
"""Query the RAG vector store and return ranked chunks."""

import logging
from typing import Any

log = logging.getLogger(__name__)


def retrieve(
    query: str,
    indexer,
    top_k: int = 5,
    channel_identifier: str | None = None,
    media_id: int | None = None,
) -> list[dict]:
    """Embed query and return the top_k most similar chunks.

    Each result dict: {text, score, filename, channel_title, channel_identifier,
                       page, chapter, media_id}
    Returns [] if the index is empty or on error.
    """
    count = indexer.collection.count()
    if count == 0:
        return []

    where: dict[str, Any] | None = None
    if channel_identifier and media_id is not None:
        where = {"$and": [
            {"channel_identifier": {"$eq": channel_identifier}},
            {"media_id": {"$eq": media_id}},
        ]}
    elif channel_identifier:
        where = {"channel_identifier": {"$eq": channel_identifier}}
    elif media_id is not None:
        where = {"media_id": {"$eq": media_id}}

    try:
        q_emb = indexer.embed_query(query)
        kwargs: dict[str, Any] = {
            "query_embeddings": [q_emb],
            "n_results": min(top_k, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        result = indexer.collection.query(**kwargs)
    except Exception as exc:
        log.error(f"RAG retrieve error: {exc}")
        return []

    chunks = []
    for doc, meta, dist in zip(
        result["documents"][0],
        result["metadatas"][0],
        result["distances"][0],
    ):
        page = meta.get("page")
        chunks.append({
            "text": doc,
            "score": round(1.0 - dist, 4),
            "filename": meta.get("filename", ""),
            "channel_title": meta.get("channel_title", ""),
            "channel_identifier": meta.get("channel_identifier", ""),
            "page": page if page != -1 else None,
            "chapter": meta.get("chapter") or None,
            "media_id": meta.get("media_id"),
        })
    return chunks
```

- [ ] **Step 3: Commit**

```bash
git add rag/indexer.py rag/retriever.py
git commit -m "feat(rag): add Indexer and retrieve()"
```

---

## Task 5: `rag/generator.py`

**Files:**
- Create: `rag/generator.py`

- [ ] **Step 1: Create `rag/generator.py`**

```python
"""Generate answers from retrieved chunks using Ollama's /api/chat endpoint."""

import json
import logging
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)


class OllamaUnavailableError(Exception):
    pass


def _build_messages(query: str, chunks: list[dict]) -> list[dict]:
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk.get("filename", "unknown")
        if chunk.get("page"):
            source += f" (p. {chunk['page']})"
        elif chunk.get("chapter"):
            source += f" -- {chunk['chapter']}"
        context_parts.append(f"[{i}] {source}\n{chunk['text']}")

    context = "\n\n---\n\n".join(context_parts)
    return [
        {
            "role": "system",
            "content": (
                "You are a helpful librarian assistant. Answer the user's question "
                "using only the provided book excerpts. Be concise. "
                "Cite sources by their [number] when you reference them."
            ),
        },
        {
            "role": "user",
            "content": f"Excerpts from my library:\n\n{context}\n\nQuestion: {query}",
        },
    ]


async def generate(
    query: str,
    chunks: list[dict],
    ollama_url: str,
    model: str,
) -> AsyncIterator[str]:
    """Yield answer tokens streamed from Ollama.

    Raises OllamaUnavailableError if the server is not reachable.
    """
    payload = {
        "model": model,
        "messages": _build_messages(query, chunks),
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{ollama_url}/api/chat", json=payload
            ) as resp:
                if resp.status_code != 200:
                    raise OllamaUnavailableError(
                        f"Ollama returned HTTP {resp.status_code} from {ollama_url}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if data.get("done"):
                        break
    except httpx.ConnectError:
        raise OllamaUnavailableError(
            f"Ollama not reachable at {ollama_url!r}. "
            "Is it running? Try: ollama serve"
        )
```

- [ ] **Step 2: Commit**

```bash
git add rag/generator.py
git commit -m "feat(rag): add Ollama streaming generator"
```

---

## Task 6: Wire auto-indexing into `downloader.py` and `listener.py`

**Files:**
- Modify: `downloader.py`
- Modify: `listener.py`

- [ ] **Step 1: Update `downloader.py`**

Replace the entire file:

```python
import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient

from db import Database
from lang_filter import DISCARD_LANG, analyze_file
from utils import compute_sha256, human_size, unique_path

log = logging.getLogger(__name__)


async def download_item(
    client: TelegramClient,
    db: Database,
    item: dict,
    dest: Path,
    semaphore: asyncio.Semaphore,
    *,
    message=None,
    topic_keywords: dict | None = None,
    topic_min_matches: int = 2,
    topic_min_occurrences: int = 1,
    indexer=None,
) -> bool:
    """Download one media item to dest. Returns True on success.

    item must contain: id, message_id, filename, and either channel_identifier
    or channel_telegram_id for entity resolution.

    If message is provided (a live Telethon message object), it is used directly
    and no Telegram fetch is needed.

    If indexer is provided (a rag.indexer.Indexer), the file is indexed after
    a successful download via a background asyncio task.
    """
    async with semaphore:
        filepath = unique_path(dest / item["filename"])
        label = item.get("channel_title") or item.get("channel_identifier") or str(item.get("channel_telegram_id", "?"))
        try:
            if message is None:
                identifier = item.get("channel_identifier") or item["channel_telegram_id"]
                entity = await client.get_entity(identifier)
                message = await client.get_messages(entity, ids=item["message_id"])
                if message is None:
                    db.mark_discarded(item["id"])
                    log.warning(
                        f"[{label}] Message {item['message_id']} not found on Telegram "
                        f"(deleted?) -- {item['filename']!r} marked discarded"
                    )
                    return True

            await client.download_media(message, file=str(filepath))

            ext = item.get("ext") or ""

            lang, topic = analyze_file(filepath, ext, topic_keywords, topic_min_matches, topic_min_occurrences)

            if lang == DISCARD_LANG:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (German): {item['filename']}")
                return True

            if topic:
                filepath.unlink(missing_ok=True)
                db.mark_discarded(item["id"])
                log.info(f"[{label}] Auto-discarded (topic: {topic}): {item['filename']}")
                return True

            file_hash = None
            try:
                file_hash = await asyncio.to_thread(compute_sha256, filepath)
            except Exception as exc:
                log.warning(f"[{label}] Hash failed for {item['filename']!r}: {exc}")

            db.mark_downloaded(item["id"], str(filepath), language=lang, file_hash=file_hash)
            size_str = human_size(filepath.stat().st_size) if filepath.exists() else "?"
            lang_tag = f" [{lang}]" if lang else ""
            log.info(f"[{label}] Downloaded: {item['filename']}  ({size_str}){lang_tag}")

            if indexer is not None:
                asyncio.create_task(_index_async(indexer, db, item, filepath))

            return True
        except Exception as exc:
            log.error(f"[{label}] Failed to download {item['filename']!r}: {exc}")
            return False


async def _index_async(indexer, db: Database, item: dict, filepath: Path) -> None:
    """Index a downloaded file in the background. Errors are logged, not raised."""
    try:
        count = await asyncio.to_thread(
            indexer.index_file,
            item["id"],
            filepath,
            {
                "filename": item["filename"],
                "channel_title": item.get("channel_title", ""),
                "channel_identifier": item.get("channel_identifier", ""),
                "ext": item.get("ext", ""),
            },
        )
        if count > 0:
            db.mark_indexed(item["id"])
            log.debug(f"RAG: indexed {item['filename']} ({count} chunks)")
    except Exception as exc:
        log.warning(f"RAG: indexing failed for {item['filename']!r}: {exc}")
```

- [ ] **Step 2: Update `listener.py`**

Replace the entire file:

```python
import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel, PeerChat

from db import Database
from downloader import download_item

log = logging.getLogger(__name__)


async def run_listener(client: TelegramClient, db: Database, config: dict) -> None:
    """Start the real-time listener. Blocks until the client disconnects."""
    destination = Path(config["download"]["destination"])
    allowed = set(config["filters"]["extensions"])
    retention_days = config["download"]["retention_days"]
    concurrent_downloads = config["download"]["concurrent_downloads"]
    topic_keywords = config["filters"].get("discard_topics") or {}
    topic_min_matches = config["filters"].get("topic_min_matches", 2)
    topic_min_occurrences = config["filters"].get("topic_min_keyword_occurrences", 1)
    destination.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrent_downloads)

    rag_config = config.get("rag", {})
    indexer = None
    if rag_config.get("enabled"):
        try:
            from rag.indexer import Indexer
            indexer = Indexer(rag_config)
        except Exception as exc:
            log.error(f"RAG: failed to initialise indexer -- {exc}. Continuing without RAG.")

    await _flush_pending(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
    await _heal_missing(client, db, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
    await _backfill_missed(client, db, allowed, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)

    asyncio.create_task(_cleanup_loop(db, retention_days))

    channels = db.list_channels()
    log.info(f"Listening -- {len(channels)} subscribed channel(s)")
    for c in channels:
        log.info(f"  . {c['title']} ({c['identifier']})")

    @client.on(events.NewMessage)
    async def on_new_message(event):
        try:
            await _handle(event, db, allowed, client, destination, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer)
        except Exception as exc:
            log.error(f"Error handling message {event.message.id}: {exc}", exc_info=True)

    log.info("Waiting for new messages. Use Ctrl+C to stop.")
    await client.run_until_disconnected()


async def _flush_pending(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Download all items that are pending in the DB (e.g. from a previous scrape)."""
    pending = db.get_pending_media()
    if not pending:
        return
    log.info(f"Flushing {len(pending)} pending item(s) from previous session(s)...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        indexer=indexer)
          for item in pending],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Flush complete: {ok}/{len(pending)} succeeded")


async def _heal_missing(
    client, db, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Re-download files marked 'downloaded' in the DB but absent from disk."""
    downloaded = db.get_downloaded_media()
    missing = [
        item for item in downloaded
        if not item.get("local_path") or not Path(item["local_path"]).exists()
    ]
    if not missing:
        return
    log.info(f"Healing {len(missing)} file(s) present in DB but missing from disk...")
    results = await asyncio.gather(
        *[download_item(client, db, item, dest, semaphore,
                        topic_keywords=topic_keywords,
                        topic_min_matches=topic_min_matches,
                        topic_min_occurrences=topic_min_occurrences,
                        indexer=indexer)
          for item in missing],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    log.info(f"Heal complete: {ok}/{len(missing)} restored")


async def _backfill_missed(
    client, db, allowed, dest, semaphore, topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    """Fetch messages that arrived while the service was down and download them."""
    for ch in db.list_channels():
        max_id = db.get_max_message_id(ch["id"])
        if max_id is None:
            log.warning(
                f"Backfill: no prior messages recorded for {ch['title']!r} -- "
                f"run 'scrape --channel {ch['identifier']}' to pull existing history"
            )
            continue

        try:
            entity = await client.get_entity(ch["identifier"])
        except Exception as exc:
            log.warning(f"Backfill: cannot resolve {ch['identifier']!r}: {exc}")
            continue

        tasks = []
        async for message in client.iter_messages(entity, min_id=max_id):
            if not message.media:
                continue
            item_meta = _extract_media(message)
            if item_meta is None:
                continue
            if allowed and item_meta["ext"] not in allowed:
                continue
            db_id = db.save_media_message(
                channel_id=ch["id"],
                message_id=message.id,
                filename=item_meta["filename"],
                size=item_meta["size"],
                mime_type=item_meta["mime_type"],
                ext=item_meta["ext"],
                date=message.date.isoformat(),
                caption=(message.message or "")[:120],
            )
            if db_id:
                tasks.append(download_item(
                    client, db,
                    {
                        "id": db_id,
                        "channel_identifier": ch["identifier"],
                        "channel_telegram_id": ch["telegram_id"],
                        "channel_title": ch["title"],
                        "message_id": message.id,
                        "filename": item_meta["filename"],
                        "size": item_meta["size"],
                        "ext": item_meta["ext"],
                    },
                    dest, semaphore, message=message,
                    topic_keywords=topic_keywords,
                    topic_min_matches=topic_min_matches,
                    topic_min_occurrences=topic_min_occurrences,
                    indexer=indexer,
                ))

        if tasks:
            log.info(f"Backfilling {len(tasks)} missed item(s) from {ch['title']}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            log.info(f"Backfill {ch['title']}: {ok}/{len(tasks)} succeeded")


async def _cleanup_loop(db: Database, retention_days: int) -> None:
    """Run retention cleanup once on startup, then every hour. No-op if retention_days <= 0."""
    if retention_days <= 0:
        return
    while True:
        try:
            _run_cleanup(db, retention_days)
        except Exception as exc:
            log.error(f"Cleanup error: {exc}", exc_info=True)
        await asyncio.sleep(3600)


def _run_cleanup(db: Database, retention_days: int) -> None:
    expired = db.get_expired_files(retention_days)
    if not expired:
        return
    deleted = 0
    for item in expired:
        if item.get("local_path"):
            p = Path(item["local_path"])
            if p.exists():
                p.unlink()
                deleted += 1
        db.mark_expired(item["id"])
    log.info(
        f"Retention cleanup: {deleted} file(s) deleted, "
        f"{len(expired)} record(s) marked expired (>{retention_days}d)"
    )


async def _handle(
    event, db, allowed, client, dest, semaphore,
    topic_keywords, topic_min_matches, topic_min_occurrences, indexer
) -> None:
    if not event.message.media:
        return

    peer = event.message.peer_id
    if isinstance(peer, PeerChannel):
        raw_id = peer.channel_id
    elif isinstance(peer, PeerChat):
        raw_id = peer.chat_id
    else:
        return

    channel = db.get_channel_by_telegram_id(raw_id)
    if channel is None:
        return

    item_meta = _extract_media(event.message)
    if item_meta is None:
        return

    if allowed and item_meta["ext"] not in allowed:
        log.debug(f"Skipping {item_meta['filename']!r}: extension not in filter")
        return

    db_id = db.save_media_message(
        channel_id=channel["id"],
        message_id=event.message.id,
        filename=item_meta["filename"],
        size=item_meta["size"],
        mime_type=item_meta["mime_type"],
        ext=item_meta["ext"],
        date=event.message.date.isoformat(),
        caption=(event.message.message or "")[:120],
    )
    if db_id:
        log.info(f"[{channel['title']}] New media: {item_meta['filename']} ({item_meta['size']} B) -- queuing download")
        asyncio.create_task(download_item(
            client, db,
            {
                "id": db_id,
                "channel_identifier": channel["identifier"],
                "channel_telegram_id": channel["telegram_id"],
                "channel_title": channel["title"],
                "message_id": event.message.id,
                "filename": item_meta["filename"],
                "size": item_meta["size"],
                "ext": item_meta["ext"],
            },
            dest, semaphore, message=event.message,
            topic_keywords=topic_keywords,
            topic_min_matches=topic_min_matches,
            topic_min_occurrences=topic_min_occurrences,
            indexer=indexer,
        ))


def _extract_media(message) -> dict | None:
    if message.document:
        f = message.file
        ext = (f.ext or "").lstrip(".").lower()
        return {
            "filename": f.name or f"document_{message.id}{f.ext or ''}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "application/octet-stream",
            "ext": ext,
        }
    if message.photo:
        f = message.file
        ext = (f.ext or ".jpg").lstrip(".").lower()
        return {
            "filename": f"photo_{message.id}.{ext}",
            "size": f.size or 0,
            "mime_type": f.mime_type or "image/jpeg",
            "ext": ext,
        }
    return None
```

- [ ] **Step 3: Verify the existing DB tests still pass**

```bash
uv run pytest tests/test_db_rag.py -v
```

Expected: 5 passed.

- [ ] **Step 4: Commit**

```bash
git add downloader.py listener.py
git commit -m "feat(rag): wire auto-indexing into download pipeline"
```

---

## Task 7: `main.py` -- `index` and `ask` subcommands

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add `index` and `ask` parsers to `build_parser()`**

In `build_parser()`, after the `scan-hashes` parser line, add:

```python
    sub.add_parser("index", help="Index downloaded files into the RAG vector store")

    p = sub.add_parser("ask", help="Query the RAG index with a natural language question")
    p.add_argument("query", help="Natural language question")
    p.add_argument("--sources-only", action="store_true",
                   help="Show matching sources only, skip AI generation")
    p.add_argument("--top-k", type=int, default=None, metavar="N",
                   help="Number of chunks to retrieve (default: from config)")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None,
                   help="Restrict search to one channel")
```

- [ ] **Step 2: Add dispatch in `run()` -- before the Telegram client block**

In `run()`, add after the `scan-hashes` handler and before `tg = config["telegram"]`:

```python
    if args.command == "index":
        await cmd_index(db, config)
        return
    if args.command == "ask":
        await cmd_ask(db, config, args)
        return
```

- [ ] **Step 3: Add `cmd_index()` function to `main.py`**

Add this async function after `cmd_scan_hashes`:

```python
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
```

- [ ] **Step 4: Add `cmd_ask()` function to `main.py`**

Add this async function after `cmd_index`:

```python
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
            loc = f"p. {chunk['page']}" if chunk.get("page") else (chunk.get("chapter") or "")
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
            loc = f"p. {chunk['page']}" if chunk.get("page") else (chunk.get("chapter") or "")
            console.print(f"  . {fn:<45} {loc}")
```

- [ ] **Step 5: Verify config loads without error**

```bash
uv run python -c "from config import load_config; c = load_config('config.yaml'); print(c.get('rag', {}).get('enabled'))"
```

Expected: `True`

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat(rag): add index and ask subcommands to main.py"
```

---

## Task 8: `tgdctl.py` -- proxy commands

**Files:**
- Modify: `tgdctl.py`

- [ ] **Step 1: Add `index` and `ask` subparser declarations**

In `main()`, after `sub.add_parser("scan-hashes")`, add:

```python
    sub.add_parser("index")

    p = sub.add_parser("ask")
    p.add_argument("query")
    p.add_argument("--sources-only", action="store_true")
    p.add_argument("--top-k", type=int, default=None, metavar="N")
    p.add_argument("--channel", metavar="IDENTIFIER", default=None)
```

- [ ] **Step 2: Add dispatch cases**

In the dispatch block, after the `scan-hashes` handler, add:

```python
    elif args.command == "index":
        sys.exit(app("index"))
    elif args.command == "ask":
        extra = [args.query]
        if args.sources_only:
            extra.append("--sources-only")
        if args.top_k:
            extra += ["--top-k", str(args.top_k)]
        if args.channel:
            extra += ["--channel", args.channel]
        sys.exit(app("ask", *extra))
```

- [ ] **Step 3: Update the epilog**

In the `parser` epilog string, append to the "app commands" section:

```
  index                       Index all downloaded files into the RAG vector store
  ask "<question>"            Query the RAG index; answer generated by local Ollama model
    --sources-only              Show only matching sources, skip generation
    --top-k N                   Chunks to retrieve (default: from config)
    --channel @channel          Restrict search to one channel
```

- [ ] **Step 4: Smoke-test help output**

```bash
uv run tgdctl --help
```

Expected: `index` and `ask` appear in the commands list without error.

- [ ] **Step 5: Commit**

```bash
git add tgdctl.py
git commit -m "feat(rag): add tgdctl index and ask proxy commands"
```

---

## Task 9: Webui -- build context, Dockerfile, RAG endpoints

**Files:**
- Modify: `docker-compose.yml`
- Modify: `webui/Dockerfile`
- Modify: `webui/app.py`

- [ ] **Step 1: Update `docker-compose.yml` webui build stanza**

Change the webui `build: ./webui` line to a context + dockerfile form:

```yaml
  webui:
    build:
      context: .
      dockerfile: webui/Dockerfile
    container_name: tg-webui
    restart: always
    ports:
      - "8090:8080"
    volumes:
      - ./data:/app/data
      - /srv/share/tg-downloads:/srv/share/tg-downloads
```

- [ ] **Step 2: Update `webui/Dockerfile`**

Replace the entire file:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY rag/ ./rag/
COPY webui/app.py ./
COPY webui/static/ ./static/

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 3: Add RAG config constants to `webui/app.py`**

After the `THUMBS_DIR = ...` line, add:

```python
RAG_INDEX_PATH = os.environ.get("RAG_INDEX_PATH", "/app/data/rag_index")
RAG_EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")
RAG_OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://host.docker.internal:11434")
RAG_OLLAMA_MODEL = os.environ.get("RAG_OLLAMA_MODEL", "phi3:mini")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))

_rag_indexer = None


def _get_indexer():
    global _rag_indexer
    if _rag_indexer is None:
        try:
            from rag.indexer import Indexer
            _rag_indexer = Indexer({
                "index_path": RAG_INDEX_PATH,
                "embed_model": RAG_EMBED_MODEL,
            })
        except Exception as exc:
            log.warning(f"RAG indexer unavailable: {exc}")
    return _rag_indexer
```

- [ ] **Step 4: Append RAG endpoints to `webui/app.py`**

At the end of the file, append:

```python
@app.get("/api/rag/search")
def rag_search(q: str, channel: str = "", top_k: int = 0):
    from rag.retriever import retrieve
    indexer = _get_indexer()
    if indexer is None:
        return {"chunks": [], "error": "RAG index not available"}
    k = top_k or RAG_TOP_K
    chunks = retrieve(q, indexer, top_k=k, channel_identifier=channel or None)
    return {"chunks": chunks}


class _AskRequest(BaseModel):
    query: str
    channel: str = ""
    top_k: int = 0


@app.post("/api/rag/ask")
async def rag_ask(req: _AskRequest):
    from rag.retriever import retrieve
    from rag.generator import generate, OllamaUnavailableError
    indexer = _get_indexer()
    if indexer is None:
        raise HTTPException(status_code=503, detail="RAG index not available")
    k = req.top_k or RAG_TOP_K
    chunks = retrieve(req.query, indexer, top_k=k, channel_identifier=req.channel or None)
    if not chunks:
        return {"answer": "No relevant content found in the library.", "chunks": []}
    try:
        tokens = []
        async for token in generate(req.query, chunks, RAG_OLLAMA_URL, RAG_OLLAMA_MODEL):
            tokens.append(token)
        return {"answer": "".join(tokens), "chunks": chunks}
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
```

- [ ] **Step 5: Verify `webui/app.py` parses cleanly**

```bash
uv run python -c "import ast; ast.parse(open('webui/app.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml webui/Dockerfile webui/app.py
git commit -m "feat(rag): add webui RAG endpoints and update Docker build context"
```

---

## Task 10: Web UI -- search/ask panel

**Files:**
- Modify: `webui/static/index.html`

- [ ] **Step 1: Add CSS for the RAG panel**

Inside the `<style>` block, before the closing `</style>` tag, add:

```css
/* RAG search panel */
.rag-panel {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 10px 14px;
}
.rag-bar { display: flex; gap: 8px; align-items: center; }
.rag-input {
  flex: 1; padding: 8px 12px;
  border: 1px solid var(--border); border-radius: 6px;
  font-size: .875rem; font-family: inherit; background: var(--bg);
}
.rag-input:focus { outline: none; border-color: var(--accent); }
.rag-results { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
.rag-chunk {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px;
}
.rag-chunk-source {
  font-size: .73rem; font-weight: 600; color: var(--accent);
  margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rag-chunk-text {
  font-size: .8rem; color: var(--text); line-height: 1.5;
  max-height: 72px; overflow: hidden;
}
.rag-answer {
  background: var(--accent-light); border: 1px solid var(--accent);
  border-radius: 8px; padding: 12px 14px;
}
.rag-answer-label { font-size: .73rem; font-weight: 700; color: var(--accent); margin-bottom: 6px; }
.rag-answer-text { font-size: .85rem; color: var(--text); line-height: 1.6; white-space: pre-wrap; }
```

- [ ] **Step 2: Add the panel HTML**

Immediately after `</header>` and before `<!-- mobile bottom bar -->`, insert:

```html
<div class="rag-panel">
  <div class="rag-bar">
    <input id="rag-input" type="search" class="rag-input"
           placeholder="Search your library..."
           autocomplete="off"
           onkeydown="if(event.key==='Enter')ragSearch()">
    <button class="btn" onclick="ragSearch()">Search</button>
    <button class="btn btn-primary" onclick="ragAsk()">Ask AI</button>
  </div>
  <div id="rag-results" class="rag-results" style="display:none"></div>
</div>
```

- [ ] **Step 3: Add JS functions**

In the `<script>` block, after the `thumbObs` declaration and before `/* -- Boot -- */`, add:

```javascript
/* -- RAG search/ask -- */
function _ragStatus(el, msg) {
  el.style.display = 'block';
  const p = document.createElement('p');
  p.style.cssText = 'color:var(--muted);font-size:.85rem;padding:4px 0';
  p.textContent = msg;
  el.replaceChildren(p);
}

function _ragError(el, msg) {
  el.style.display = 'block';
  const p = document.createElement('p');
  p.style.cssText = 'color:var(--danger);font-size:.85rem';
  p.textContent = msg;
  el.replaceChildren(p);
}

function _renderRagResults(chunks, container, answer) {
  container.replaceChildren();
  container.style.display = 'block';
  if (answer) {
    const box = document.createElement('div');
    box.className = 'rag-answer';
    const lbl = document.createElement('div');
    lbl.className = 'rag-answer-label';
    lbl.textContent = 'Answer';
    const txt = document.createElement('div');
    txt.className = 'rag-answer-text';
    txt.textContent = answer;
    box.appendChild(lbl);
    box.appendChild(txt);
    container.appendChild(box);
  }
  if (!chunks.length) {
    const p = document.createElement('p');
    p.style.cssText = 'color:var(--muted);font-size:.85rem;padding:4px 0';
    p.textContent = 'No matching content found.';
    container.appendChild(p);
    return;
  }
  for (const chunk of chunks) {
    const div = document.createElement('div');
    div.className = 'rag-chunk';
    const src = document.createElement('div');
    src.className = 'rag-chunk-source';
    const loc = chunk.page ? 'p. ' + chunk.page : (chunk.chapter || '');
    src.textContent = chunk.filename + (loc ? ' — ' + loc : '');
    const txt = document.createElement('div');
    txt.className = 'rag-chunk-text';
    txt.textContent = chunk.text;
    div.appendChild(src);
    div.appendChild(txt);
    container.appendChild(div);
  }
}

async function ragSearch() {
  const q = document.getElementById('rag-input').value.trim();
  if (!q) return;
  const el = document.getElementById('rag-results');
  _ragStatus(el, 'Searching...');
  try {
    const ch = encodeURIComponent(S.channel || '');
    const data = await fetch(
      '/api/rag/search?q=' + encodeURIComponent(q) + '&channel=' + ch
    ).then(r => r.json());
    _renderRagResults(data.chunks || [], el, null);
  } catch {
    _ragError(el, 'Search failed. Check server logs.');
  }
}

async function ragAsk() {
  const q = document.getElementById('rag-input').value.trim();
  if (!q) return;
  const el = document.getElementById('rag-results');
  _ragStatus(el, 'Generating answer...');
  try {
    const data = await fetch('/api/rag/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, channel: S.channel || '' }),
    }).then(r => r.json());
    _renderRagResults(data.chunks || [], el, data.answer || null);
  } catch {
    _ragError(el, 'Ask failed. Is Ollama running?');
  }
}
```

- [ ] **Step 4: Verify the HTML is well-formed**

```bash
uv run python -c "
from html.parser import HTMLParser
class V(HTMLParser): pass
V().feed(open('webui/static/index.html').read())
print('HTML parsed OK')
"
```

Expected: `HTML parsed OK`

- [ ] **Step 5: Commit**

```bash
git add webui/static/index.html
git commit -m "feat(rag): add search/ask panel to web UI"
```

---

## Task 11: Final integration check

- [ ] **Step 1: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass (test_chunker.py + test_db_rag.py).

- [ ] **Step 2: Verify tgdctl help shows new commands**

```bash
uv run tgdctl --help
```

Expected: `index` and `ask` appear in the commands list.

- [ ] **Step 3: Verify main.py loads cleanly**

```bash
uv run python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Update CLAUDE.md**

In `CLAUDE.md`:

1. Add `chromadb`, `sentence-transformers`, `httpx` to the **Stack** section under PyMuPDF.

2. Add to the tgdctl usage block:
```
uv run tgdctl index                  # index all downloaded files into the RAG vector store
uv run tgdctl ask "query"            # ask a natural language question about your library
uv run tgdctl ask "query" --sources-only  # show matching sources without AI generation
```

3. Add a **RAG** subsection after **Web UI**:
```
### RAG (Retrieval-Augmented Generation)
Semantic search and AI Q&A over the downloaded library. Disabled by default; enable via `rag.enabled: true` in `config.yaml`.

- Embeddings: `all-MiniLM-L6-v2` (sentence-transformers, local, ~22MB model)
- Vector store: ChromaDB persisted to `data/rag_index/`
- Generation: Ollama running on the Pi host (`http://host.docker.internal:11434`)
- Auto-indexed after each download; re-run `tgdctl index` to index existing files
- Only `pdf` and `epub` are indexed; other formats are skipped
```

- [ ] **Step 5: Final commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for RAG system"
```

---

## Post-implementation: First-run setup

After deploying (rebuild containers with `tgdctl start`):

1. Install Ollama on the Pi host: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull a model: `ollama pull phi3:mini`
3. Index existing library: `tgdctl index` (will take a while for large libraries)
4. Test sources-only first: `tgdctl ask "stoicism and virtue" --sources-only`
5. Test full generation: `tgdctl ask "what is the dichotomy of control?"`
