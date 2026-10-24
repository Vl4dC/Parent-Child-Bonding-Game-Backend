"""Chroma-backed vector store for PDF material retrieval.

Indexes each PDF page as a document embedded with EMBEDDING_MODEL (default
openrouter:google/gemini-embedding-2, using OPENROUTER_API_KEY; or openai:<model> with OPENAI_API_KEY).
When creating a lesson the most relevant pages are retrieved by cosine similarity
instead of the BM25 keyword search. Falls back transparently to BM25 when the
Chroma collection is missing (e.g. after an in-memory restart) or embeddings are
unavailable (no key, unknown provider, provider down): the index is optional, uploads are not.
"""

from __future__ import annotations

import logging
import os

import chromadb

from app.generation import model_settings

log = logging.getLogger("quiz_app")
_client: chromadb.ClientAPI | None = None
_COLLECTION_PREFIX = "mat_"
DEFAULT_EMBEDDING_MODEL = "openrouter:google/gemini-embedding-2"


def _chroma() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.EphemeralClient()
    return _client


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed with EMBEDDING_MODEL. Index and query must use the same model (same vector space)."""
    import langchain_openai

    runtime = model_settings.get()
    model = (
        runtime.embedding_model
        if runtime
        else os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    )
    timeout = (
        runtime.model_timeout_seconds if runtime else int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    )
    provider, _, name = model.partition(":")
    if provider == "openrouter":
        # OpenAI-compatible endpoint. Send text, not tiktoken ids: only OpenAI accepts those.
        client = langchain_openai.OpenAIEmbeddings(
            model=name,
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            check_embedding_ctx_length=False,
            timeout=timeout,
            max_retries=0,
        )
    elif provider == "openai":
        client = langchain_openai.OpenAIEmbeddings(model=name, timeout=timeout, max_retries=0)
    else:
        raise ValueError(f"Unsupported EMBEDDING_MODEL provider: {provider!r}")
    return client.embed_documents(texts)


def index(material_id: str, pages: list) -> None:
    """Embed and store all non-empty pages of a PDF material."""
    records = [(p.number, p.text) for p in pages if p.text.strip()]
    if not records:
        return
    numbers, texts = zip(*records)
    try:
        embeddings = _embed(list(texts))
    except Exception as exc:
        # Type only: provider messages can echo part of the API key.
        log.warning(
            "Embeddings unavailable (%s); material %s uses BM25", type(exc).__name__, material_id
        )
        return
    col = _chroma().get_or_create_collection(
        _COLLECTION_PREFIX + material_id,
        metadata={"hnsw:space": "cosine"},
    )
    col.add(
        ids=[f"{material_id}_{n}" for n in numbers],
        embeddings=embeddings,
        documents=list(texts),
        metadatas=[{"page": int(n)} for n in numbers],
    )


def retrieve(material_id: str, query: str, k: int = 8) -> list[tuple[str, int]]:
    """Return up to k (text, page_number) pairs most relevant to query, or [] if uncached."""
    try:
        col = _chroma().get_collection(_COLLECTION_PREFIX + material_id)
    except Exception:
        return []
    count = col.count()
    if count == 0:
        return []
    try:
        (query_emb,) = _embed([query])
    except Exception as exc:
        log.warning(
            "Embeddings unavailable (%s); material %s uses BM25", type(exc).__name__, material_id
        )
        return []
    results = col.query(
        query_embeddings=[query_emb],
        n_results=min(k, count),
    )
    return [
        (doc, meta["page"]) for doc, meta in zip(results["documents"][0], results["metadatas"][0])
    ]


def drop(material_id: str) -> None:
    """Delete the Chroma collection for a material (called on material deletion)."""
    try:
        _chroma().delete_collection(_COLLECTION_PREFIX + material_id)
    except Exception:
        pass
