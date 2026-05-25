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
