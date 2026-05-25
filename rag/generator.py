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
