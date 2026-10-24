"""
Meaning-based search over an uploaded book: the retrieval half of a retrieval-augmented lesson.

  chunk_pages()      page text -> small overlapping pieces, each remembering its page
  Embedder           text -> vector, through the configured provider (OpenRouter or OpenAI)
  VectorIndex        one unit vector per chunk, stored compactly beside the owned material
  index_material()   at upload: chunk, embed, store. Best effort: if it fails, keyword search still works
  SemanticSearch     query -> {page: similarity}, handed to materials.select_pages next to BM25

The index is per book, so a plain scan of a few thousand vectors is the whole "vector search": no
vector database is needed, and the same code runs in memory (demo) and in PostgreSQL. Only extracted page
text is embedded and only the vectors are kept; the text itself still lives in the material record.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import struct
from operator import mul
from typing import Sequence

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel

from app.generation import ContextPool, model_settings
from app.repository import current_session
from learning_service.materials import Material, Page

log = logging.getLogger("embeddings")

KIND = "material_vectors"  # owned record kind; the key is "<material id>:<revision>"
DEFAULT_MODEL = "openrouter:openai/text-embedding-3-small"
DEFAULT_DIMENSIONS = 512
OPENROUTER_URL = "https://openrouter.ai/api/v1"

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
MAX_CHUNKS = 3000  # a 600-page book is about 1,500; this only stops a runaway
BATCH = 64  # texts per embedding request
WORKERS = 4
# Cosine similarity. Measured with text-embedding-3-small: related text 0.44-0.66, unrelated under 0.15.
MIN_SIMILARITY = 0.25
MAX_MEANING_PAGES = 12  # pages one query can bring in, best first
MAX_MEANING_CHUNKS = 12  # chunks one chat question can bring in, best first


class Embedder:
    """Turns text into vectors with the configured provider. Batches are sent in parallel."""

    def __init__(self, client, model: str, dimensions: int):
        self.client, self.model, self.dimensions = client, model, dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        batches = [texts[i : i + BATCH] for i in range(0, len(texts), BATCH)]
        with ContextPool(WORKERS) as pool:
            parts = list(pool.map(self.client.embed_documents, batches))
        return [vector for part in parts for vector in part]

    def embed_query(self, text: str) -> list[float]:
        return self.client.embed_query(text)


def model_embedder() -> Embedder | None:
    """The configured embedding model, or None when meaning-based search is off (no model named, an
    unsupported provider, or no API key for it). Same `provider:model` format as QUIZ_MODEL."""
    runtime = model_settings.get()
    if runtime:
        spec, dimensions = runtime.embedding_model, runtime.embedding_dimensions
        timeout = runtime.model_timeout_seconds
    else:
        spec = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", DEFAULT_DIMENSIONS))
        timeout = int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    if not spec:
        return None
    provider, _, name = spec.partition(":")
    if provider == "openrouter":
        base_url, key = OPENROUTER_URL, os.getenv("OPENROUTER_API_KEY")
    elif provider == "openai":
        base_url, key = None, os.getenv("OPENAI_API_KEY")
    else:
        log.warning("embedding provider %r is not supported; using keyword search only", provider)
        return None
    if not key or not name:
        return None
    from langchain_openai import OpenAIEmbeddings

    client = OpenAIEmbeddings(
        model=name,
        base_url=base_url,
        api_key=key,
        dimensions=dimensions,
        timeout=timeout,
        max_retries=1,
        check_embedding_ctx_length=False,  # send the text as is; OpenRouter does not take token ids
    )
    return Embedder(client, spec, dimensions)


# --------------------------------------------------------------------------
# Chunks and vectors
# --------------------------------------------------------------------------
def chunk_pages(pages: list[Page], skip: set[int] = frozenset()) -> list[tuple[int, str]]:
    """(page number, text) pieces. A piece never crosses a page, so every hit names its page.
    Pages in `skip` (contents and index pages: they list every topic without teaching it) and blank pages
    are left out."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP)
    return [
        (page.number, piece)
        for page in pages
        if page.number not in skip and page.text.strip()
        for piece in splitter.split_text(page.text)
    ]


def unit(vector: Sequence[float]) -> list[float]:
    length = math.sqrt(sum(x * x for x in vector))
    return [x / length for x in vector] if length else [0.0] * len(vector)


class VectorIndex(BaseModel):
    material_id: str
    revision: int
    # the embedding model it was made with; an index from another model is not comparable
    model: str
    requested_dimensions: int
    dimensions: int
    pages: list[int]  # the page of each chunk, in order
    vectors: str  # base64 float16, one unit-length vector per chunk, same order as `pages`

    @classmethod
    def build(
        cls,
        material: Material,
        embedder: Embedder,
        pages: list[int],
        vectors: list[list[float]],
    ) -> VectorIndex:
        if len(vectors) != len(pages) or len({len(v) for v in vectors}) != 1:
            raise ValueError("The embedding provider returned an unexpected number of vectors")
        flat = [x for vector in vectors for x in unit(vector)]
        return cls(
            material_id=material.id,
            revision=material.revision,
            model=embedder.model,
            requested_dimensions=embedder.dimensions,
            dimensions=len(vectors[0]),
            pages=pages,
            vectors=base64.b64encode(struct.pack(f"<{len(flat)}e", *flat)).decode("ascii"),
        )

    def decode(self) -> tuple[float, ...]:
        raw = base64.b64decode(self.vectors)
        count = len(self.pages) * self.dimensions
        if len(raw) != count * 2:
            raise ValueError("Stored vectors do not match the chunk list")
        return struct.unpack(f"<{count}e", raw)

    def similarities(self, query: Sequence[float]) -> list[float]:
        """Cosine similarity of the query to every chunk, in chunk order ([] if the sizes don't fit)."""
        q = unit(query)
        if len(q) != self.dimensions:
            return []
        flat, size = self.decode(), self.dimensions
        return [sum(map(mul, q, flat[i * size : (i + 1) * size])) for i in range(len(self.pages))]

    def best_chunks(self, query: Sequence[float], limit: int) -> list[tuple[int, float]]:
        """(chunk index, similarity) of the chunks at or above MIN_SIMILARITY, best first, at most `limit`."""
        found = [(i, s) for i, s in enumerate(self.similarities(query)) if s >= MIN_SIMILARITY]
        return sorted(found, key=lambda item: -item[1])[:limit]

    def scores(self, query: Sequence[float]) -> dict[int, float]:
        """page -> its best chunk's cosine similarity to the query, for pages at or above MIN_SIMILARITY."""
        best: dict[int, float] = {}
        for i, similarity in self.best_chunks(query, len(self.pages)):
            best.setdefault(self.pages[i], similarity)  # best chunks come first
        return dict(list(best.items())[:MAX_MEANING_PAGES])


def key(material: Material) -> str:
    return f"{material.id}:{material.revision}"


def load_index(material: Material) -> VectorIndex | None:
    """The stored index for the material's current text, or None if there is none (or it is unreadable)."""
    try:
        index = current_session.get().get(KIND, key(material), VectorIndex)
        if index is not None:
            index.decode()
        return index
    except Exception:
        log.warning("stored vectors of a material are unreadable; they will be rebuilt")
        return None


def build_index(material: Material, embedder: Embedder) -> VectorIndex:
    """Chunk, embed and store the material's current text; remembers on the material that it is searchable."""
    chunks = chunk_pages(material.pages, set(material.contents))
    if not chunks:
        raise ValueError("No text to index")
    if len(chunks) > MAX_CHUNKS:
        raise ValueError(f"{len(chunks)} chunks is more than the {MAX_CHUNKS} allowed")
    vectors = embedder.embed([text for _, text in chunks])
    index = VectorIndex.build(material, embedder, [page for page, _ in chunks], vectors)
    current_session.get().put(KIND, key(material), index)
    material.index_revision, material.index_chunks = material.revision, len(chunks)
    return index


def index_material(material: Material, embedder: Embedder | None = None) -> str:
    """Make a material searchable by meaning. Returns "indexed", "off" (no embedding model configured)
    or "failed". Never raises: a failure leaves keyword search, which needs no index."""
    embedder = embedder or model_embedder()
    if embedder is None:
        return "off"
    try:
        build_index(material, embedder)
    except Exception:
        log.warning("could not index a material for meaning-based search")
        return "failed"
    return "indexed"


class SemanticSearch:
    """query -> {page: similarity}. The index is read (or, with `build`, made) on the first query, so page
    ranges and chapters never reach the embedding provider. Every failure becomes "no meaning-based hits"."""

    def __init__(self, material: Material, embedder: Embedder, build: bool):
        self.material, self.embedder, self.build = material, embedder, build
        self.used = False  # did meaning-based search take part in the answer?

    def index(self, pages: list[int] | None = None) -> VectorIndex | None:
        """The stored index, made first if allowed and missing or outdated. With `pages` (the page of each chunk
        the caller has), an index that doesn't line up with them counts as outdated."""
        index = load_index(self.material)
        if (
            index is not None
            and index.model == self.embedder.model
            and index.requested_dimensions == self.embedder.dimensions
            and (pages is None or index.pages == pages)
        ):
            return index
        return build_index(self.material, self.embedder) if self.build else None

    def __call__(self, query: str) -> dict[int, float]:
        try:
            index = self.index()
            if index is None:
                return {}
            found = index.scores(self.embedder.embed_query(query))
        except Exception:
            log.warning("meaning-based search failed; using keyword search only")
            return {}
        self.used = True
        return found

    def chunks(self, query: str, pieces: list[tuple[int, str]]) -> dict[int, float]:
        """chunk index -> similarity for the best chunks of `pieces` (the material's chunk_pages), best first."""
        try:
            index = self.index([page for page, _ in pieces])
            if index is None:
                return {}
            found = dict(index.best_chunks(self.embedder.embed_query(query), MAX_MEANING_CHUNKS))
        except Exception:
            log.warning("meaning-based search failed; using keyword search only")
            return {}
        self.used = True
        return found


def semantic_search(material: Material, build: bool = True) -> SemanticSearch | None:
    """A search over the material's meaning, or None when no embedding model is configured. With
    build=False it only uses an index that already exists (never pays to make one)."""
    embedder = model_embedder()
    return SemanticSearch(material, embedder, build) if embedder else None
