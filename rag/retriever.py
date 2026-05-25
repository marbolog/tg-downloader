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
