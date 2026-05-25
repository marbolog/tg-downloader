# Design: Replace RAG Stack with SQLite FTS5 + Claude API

**Date:** 2026-05-25
**Status:** Approved
**Goal:** Eliminate the RAM and CPU pressure that causes the Raspberry Pi to freeze, while preserving full search and AI Q&A functionality.

---

## Problem

The current RAG implementation loads `sentence-transformers` (PyTorch, ~400 MB RAM) and `chromadb` (~100 MB) in the webui container at startup, and relies on Ollama (`phi3:mini`, ~2.2 GB) running on the Pi host for generation. Combined, these consume ~3 GB of RAM and cause the Pi to freeze under load.

Auto-indexing was already disabled in the listener to prevent a segfault on ARM64, but the webui still loads the model at startup unconditionally.

---

## Solution

Replace the entire RAG stack with:

- **SQLite FTS5** for full-text search (built into Python's `sqlite3`, zero RAM overhead when idle)
- **Claude Haiku API** for AI generation (off-Pi, no local model)

The Pi runs no ML models at all. The index lives inside the existing `tg_downloader.db`.

---

## Architecture

```
Download pipeline
  └── text extraction (PyMuPDF / zipfile — already a dependency)
        └── chunk text → INSERT into SQLite FTS5 table (search_fts)

Web UI / tgdctl CLI
  ├── Search: FTS5 MATCH query → BM25-ranked results with snippet excerpts
  └── Ask AI: FTS5 top-K chunks → Claude Haiku API → streamed answer
```

---

## Data Model

New virtual table added to `tg_downloader.db`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    media_id    UNINDEXED,   -- FK to media_messages.id
    chunk_idx   UNINDEXED,   -- chunk position within the file
    page        UNINDEXED,   -- PDF page number, NULL for EPUB
    chapter     UNINDEXED,   -- EPUB chapter title, NULL for PDF
    filename    UNINDEXED,   -- denormalized for result display
    text,                    -- full-text indexed content
    tokenize = 'unicode61 remove_diacritics 1'
);
```

`remove_diacritics 1` means accent-free queries match accented text: `Alentejo` finds `Alêntejo`.

No new database file. No migration of existing data — the table is created on first run and populated retroactively by `tgdctl index`.

### Query examples

```sql
-- keyword search, BM25 ranked
SELECT media_id, filename, page, chapter,
       snippet(search_fts, 5, '<<', '>>', '...', 20) AS excerpt
FROM search_fts WHERE search_fts MATCH 'Alentejo'
ORDER BY rank;

-- phrase search
WHERE search_fts MATCH '"what to do in Alentejo"'

-- boolean
WHERE search_fts MATCH 'Alentejo AND (wine OR vinho)'
```

### Delete on discard

```python
conn.execute("DELETE FROM search_fts WHERE media_id = ?", (str(media_id),))
```

---

## Text Extraction and Chunking

**Strategy:** one chunk per PDF page; one chunk per EPUB chapter section (split at ~600 words for long chapters).

**PDF:** PyMuPDF `page.get_text()` for every page. Already a declared dependency.

**EPUB:** `zipfile` (stdlib) + parse OPF manifest for spine order; extract text from each XHTML content file. Same approach as current `lang_filter.py`, extended to cover all content files.

**Unsupported formats** (MOBI, AZW3, CBR, CBZ, DJVU, FB2): skipped silently, same as current behaviour.

New module: `search/chunker.py`
Returns: `list[dict]` with keys `chunk_idx`, `page`, `chapter`, `text`.

---

## Download Pipeline Integration

Auto-indexing is re-enabled in `listener.py`. It is now safe on ARM64 — no model inference, only text extraction and SQLite writes.

```python
# After download_item() succeeds:
await asyncio.to_thread(index_file, db_path, media_id, filepath, ext, filename)
```

Runs in a thread (same pattern as SHA-256 hashing) to avoid blocking the event loop. Estimated time: 0.1–0.5s per file on the Pi.

**Startup heal:** `listen` checks for `downloaded` files absent from `search_fts` and indexes them in the background. On first run after migration this indexes the entire existing library.

**`tgdctl index`** remains as the explicit manual retroactive indexing command.

---

## Web UI API

Endpoints renamed and reimplemented:

| Old endpoint | New endpoint | Backend |
|---|---|---|
| `GET /api/rag/search?q=` | `GET /api/search?q=&top_k=` | SQLite FTS5 |
| `POST /api/rag/ask` | `POST /api/ask` | Claude Haiku API |

### Search response per result

```json
{
  "media_id": 42,
  "filename": "Alentejo Travel Guide.pdf",
  "page": 17,
  "chapter": null,
  "excerpt": "...the rolling plains of <<Alentejo>> are famous for cork oak and..."
}
```

### Startup change

The `_lifespan` context manager in `webui/app.py` currently loads `SentenceTransformer` at startup. This block is removed entirely. The webui starts instantly.

### API key handling

`ANTHROPIC_API_KEY` is read from the environment. If absent:
- `GET /api/search` — works normally (no key needed)
- `POST /api/ask` — returns HTTP 503 with message `"ANTHROPIC_API_KEY not configured"`

---

## Claude API Generation

New module: `search/generator.py`

```python
import anthropic

async def generate(
    query: str,
    chunks: list[dict],
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> AsyncIterator[str]:
    """Retrieve context from chunks and stream an answer via Claude API."""
    ...
```

System prompt (unchanged from current Ollama generator):
> "You are a helpful librarian assistant. Answer the user's question using only the provided book excerpts. Be concise. Cite sources by their [number] when you reference them."

Model: `claude-haiku-4-5-20251001`. Estimated cost: ~$0.001 per query at personal use volumes.

New dependency: `anthropic` Python package.

---

## Configuration Changes

`config.yaml` section:

```yaml
# Before (rag):
rag:
  enabled: false
  embed_model: "all-MiniLM-L6-v2"
  ollama_url: "http://host.docker-internal:11434"
  ollama_model: "phi3:mini"
  top_k: 5

# After (search):
search:
  enabled: true       # always on — FTS5 has no startup cost
  top_k: 8            # chunks returned per search/ask query
```

`docker-compose.yml` changes:

```yaml
# Remove from both containers:
- CUDA_VISIBLE_DEVICES=
- OMP_NUM_THREADS=1
- OPENBLAS_NUM_THREADS=1
- MKL_NUM_THREADS=1
- TOKENIZERS_PARALLELISM=false

# Add to webui container:
- ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

The `ANTHROPIC_API_KEY` is set in a `.env` file at the project root (not committed).

---

## What Gets Removed

### Dependencies (`pyproject.toml`)
- `chromadb`
- `sentence-transformers`
- PyTorch (transitive via sentence-transformers — removed automatically)

### Codebase
- `rag/` directory (all files) → replaced by `search/`

### Pi host
- Ollama can be stopped and uninstalled: `sudo systemctl stop ollama && sudo apt remove ollama`

---

## New File Layout

```
search/
  __init__.py
  chunker.py      # text extraction + chunking for PDF and EPUB
  indexer.py      # FTS5 insert / delete / query / is_indexed
  generator.py    # Claude API streaming generation
```

Changes to existing files:
- `db.py` — add `create_search_fts_table()` and `search_fts_missing_media_ids()` (returns `list[tuple[media_id, filepath, ext, filename]]` for `downloaded` files not present in `search_fts`)
- `listener.py` — re-enable auto-indexing; add startup heal step
- `webui/app.py` — remove lifespan model loading; replace RAG endpoints
- `webui/static/index.html` — update API endpoint paths (`/api/rag/*` → `/api/*`)
- `main.py` — update `index` and `ask` subcommands to use `search/`
- `tgdctl.py` — no changes needed (proxies commands, endpoint-agnostic)
- `docker-compose.yml` — env var changes above
- `pyproject.toml` — remove chromadb + sentence-transformers; add anthropic
- `config.yaml.example` — replace `rag:` section with `search:` section

---

## Success Criteria

1. Pi no longer freezes under normal operation
2. `tgdctl index` indexes the full library without OOM or segfault
3. Web UI search returns ranked results with context excerpts
4. Web UI Ask AI streams an answer via Claude API
5. New downloads are auto-indexed within 1s of download completion
6. `sentence-transformers`, `chromadb`, and Ollama are fully gone from the system
