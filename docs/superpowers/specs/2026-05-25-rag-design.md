# RAG System Design

**Date:** 2026-05-25
**Status:** Approved

## Overview

Add a Retrieval-Augmented Generation (RAG) system to tg-downloader so that the local library of downloaded PDFs and ebooks can be searched semantically and queried with natural language. Embeddings and retrieval run entirely on the Raspberry Pi 5. Generation is handled by a locally-running Ollama model (no cloud dependency).

---

## Architecture

### New module: `rag/`

```
rag/
├── __init__.py
├── chunker.py      # file path → list of text chunks with source metadata
├── indexer.py      # Indexer class: embed chunks, write/update ChromaDB
├── retriever.py    # query string → top-k matching chunks with scores
└── generator.py   # chunks + query → Ollama → answer + citations
```

### Storage

| Path | Contents |
|------|----------|
| `data/rag_index/` | ChromaDB persistent directory (HNSW index + metadata), inside existing Docker volume |
| `~/.cache/huggingface/` | sentence-transformers model cache (downloaded once) |

### Data flow — indexing

```
download_item() marks file as downloaded
    → if RAG enabled: asyncio.create_task(_index_async(indexer, db, item, filepath))
        → chunker.chunk_file(path, ext) → [{text, page, chapter, chunk_idx}, ...]
        → indexer embeds all chunks in one batch (sentence-transformers)
        → ChromaDB upsert: doc_id = f"{media_id}_{chunk_idx}"
          metadata = {media_id, filename, channel_title, channel_identifier, ext, page, chapter}
        → db.mark_indexed(media_id)
```

### Data flow — querying

```
tgdctl ask "query"
    → retriever.retrieve(query, top_k, filters)
        → embed query with same model
        → ChromaDB similarity search → top-k chunks
    → if --sources-only: print citations and exit
    → generator.generate(query, chunks, ollama_url, model)
        → HTTP POST to Ollama /api/chat (streaming)
        → yield tokens to terminal
    → print answer + source list
```

---

## Components

### `rag/chunker.py`

- Accepts `(file_path: Path, ext: str)` — returns `list[dict]` or `[]` for unsupported formats
- Supported: `pdf`, `epub`
- Unsupported (silently skipped): `mobi`, `azw3`, `fb2`, `djvu`, `djv`, `cbr`, `cbz`
- PDF: extract text page-by-page via PyMuPDF; split each page at double-newlines; store `page` number per chunk
- EPUB: unzip, parse OPF manifest, extract content files in spine order; split at double-newlines; store `chapter` title per chunk (from spine item label if present, else content filename without extension)
- Chunk size: ~1500 characters; overlap: ~150 characters (character-based, no tokenizer needed)
- Minimum chunk length: 100 characters — shorter fragments are merged into the previous chunk
- Each chunk: `{text: str, page: int|None, chapter: str|None, chunk_idx: int}`

### `rag/indexer.py`

- `Indexer(config: dict)` — loads embedding model on init (`all-MiniLM-L6-v2`, 384-dim vectors)
- `index_file(media_id, filepath, meta) → int` — chunks file, batch-embeds, upserts to ChromaDB; returns chunk count
- `delete_file(media_id)` — removes all chunks for a given `media_id` from ChromaDB (used if file is discarded)
- `is_indexed(media_id) → bool` — checks ChromaDB for any doc with that media_id
- ChromaDB collection name: `"documents"`; distance: cosine
- Idempotent: deletes existing entries for `media_id` before inserting, so re-indexing a file is safe
- Embedding step runs via `asyncio.to_thread` to avoid blocking the event loop

### `rag/retriever.py`

- `retrieve(query, indexer, top_k=5, channel_identifier=None, media_id=None) → list[dict]`
- Returns: `[{text, score, filename, channel_title, channel_identifier, page, chapter, media_id}, ...]`
- Optional filters passed as ChromaDB `where` clause: `channel_identifier`, `media_id`

### `rag/generator.py`

- `generate(query, chunks, ollama_url, model) → AsyncIterator[str]` — async generator, yields tokens
- Prompt structure: system message (librarian role) + context blocks (one per chunk, with source label) + user question
- If Ollama is unreachable: raises `OllamaUnavailableError` with a clear message
- Calls `/api/chat` endpoint (Ollama >= 0.1.14)

---

## Database changes (`db.py`)

- Migration: `ALTER TABLE media_messages ADD COLUMN indexed_at TEXT`
- `get_unindexed_downloaded() → list[dict]` — `status='downloaded'` AND `indexed_at IS NULL`
- `mark_indexed(media_id: int) → None` — sets `indexed_at = datetime('now')`

---

## Config (`config.yaml` / `config.yaml.example`)

```yaml
rag:
  enabled: true
  index_path: "data/rag_index"
  embed_model: "all-MiniLM-L6-v2"
  ollama_url: "http://host.docker.internal:11434"   # Pi host from inside Docker container
  ollama_model: "phi3:mini"
  top_k: 5
```

`config.py` adds `_apply_rag_defaults()` — sets all keys if `rag:` section is absent or `enabled` is missing. If `rag.enabled` is false or the section is absent entirely, RAG is disabled: `ask`/`index` commands print "RAG not enabled in config.yaml" and exit cleanly; `download_item` skips the indexing step.

---

## Dependencies (`pyproject.toml`)

```
chromadb              # embedded vector store, no server process
sentence-transformers # local embedding model (requires PyTorch — large first install ~300-500MB)
httpx                 # async HTTP client for Ollama API
```

**Note:** `sentence-transformers` pulls PyTorch as a transitive dependency. Docker image build time and size will increase significantly on the first rebuild after this change. Subsequent builds use the layer cache.

---

## CLI commands (`tgdctl.py`)

### `tgdctl index`

Indexes all downloaded files not yet in the vector store.

```
$ tgdctl index
Indexing 847 unindexed file(s)...
[████████████████] 847/847  The Meditations.pdf ✓
Done. 847 indexed, 12 skipped (unsupported format), 0 errors.
```

- Iterates `db.get_unindexed_downloaded()`
- Skips unsupported formats; logs them as "skipped"
- Shows Rich progress bar
- Idempotent and safe to re-run

### `tgdctl ask "<query>"`

Searches the index and optionally generates an answer.

```
$ tgdctl ask "what is the dichotomy of control?"
$ tgdctl ask "machine learning architectures" --sources-only
$ tgdctl ask "stoicism" --top-k 10 --channel @philosophy_books
```

Flags:
- `--sources-only` — skip Ollama, print matching chunks and citations only
- `--top-k N` — override `config.rag.top_k`
- `--channel IDENTIFIER` — restrict search to one channel

Output:
```
Answer:
[streamed text from Ollama]

Sources:
  · The Enchiridion - Epictetus.pdf          p. 4
  · Meditations - Marcus Aurelius.epub       Ch. 5 — The Inner Citadel
  · A Guide to the Good Life.pdf             p. 112
```

---

## Web UI changes

### New API endpoints (`webui/app.py`)

- `GET /api/rag/search?q=<query>&channel=<optional>&top_k=<optional>` — returns matching chunks as JSON
- `POST /api/rag/ask` body `{query, channel?, top_k?}` — returns `{answer, sources}` (non-streaming for simplicity)

The webui service initialises its own `Indexer` instance (read-only: only calls `retrieve`, never `index_file`).

### UI panel (`webui/static/index.html`)

A collapsible panel above the existing file grid:

```
┌─────────────────────────────────────────────────────┐
│ 🔍  Search your library...              [Ask AI ▼]  │
└─────────────────────────────────────────────────────┘
```

- **Search mode** (default): results appear as cards — filename, page/chapter, excerpt. Clicking a result triggers the file download (same as the existing download button).
- **Ask AI mode**: shows search results first, then a "Generating answer…" state while waiting for `/api/rag/ask`, then the answer text below the sources.
- Channel filter in the existing dropdown applies to RAG queries too.
- If RAG is not enabled or the index is empty, the panel shows a one-line status message rather than hiding entirely.

---

## Integration points in existing files

| File | Change |
|------|--------|
| `downloader.py` | Accept optional `indexer: Indexer | None` kwarg; after `mark_downloaded` succeeds, if `indexer` is set: `asyncio.create_task(_index_async(indexer, db, item, filepath))` |
| `listener.py` | After config load: `indexer = Indexer(config["rag"]) if config["rag"]["enabled"] else None`; pass to `download_item` in all three call sites (`_flush_pending`, `_heal_missing`, `_backfill_missed`, `_handle`) |
| `config.py` | Add `_apply_rag_defaults(raw)` called from `_apply_defaults` |
| `db.py` | Migration + two new methods |

---

## Error handling

| Scenario | Behaviour |
|----------|-----------|
| File deleted before indexing | `index_file` receives a missing path → logs warning, no DB entry written |
| ChromaDB write fails | Logs error; download succeeds regardless (indexing is best-effort) |
| Ollama not running | `ask` command prints clear error: "Ollama not reachable at `<url>`. Is it running? Try: `ollama serve`" |
| Unsupported format | Chunker returns `[]`; indexer logs "skipped (no text extractor)" |
| Re-indexing an already-indexed file | Idempotent upsert — safe, replaces existing chunks |

---

## Out of scope

- Re-indexing files when topic/language filters change (files already discarded won't be re-indexed)
- Indexing `expired` records (those files are gone from disk)
- Multi-turn conversation (each `ask` is a single-shot query)
- Semantic deduplication (hash-based dedup already exists; RAG operates on all non-discarded files)
