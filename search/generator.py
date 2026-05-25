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
