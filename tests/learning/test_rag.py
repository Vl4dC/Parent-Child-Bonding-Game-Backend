"""PDF retrieval index: an embedding failure must not cost the user their upload or lesson."""

import langchain_openai
import pytest

import learning_service.rag as rag
from learning_service.materials import Page
from tests.learning.files import make_pdf

# The `client` fixture swaps these for no-ops; keep the real ones for the upload test below.
real_index, real_retrieve = rag.index, rag.retrieve


def no_embeddings(_texts):
    raise RuntimeError("Missing credentials")  # e.g. OPENAI_API_KEY not set


class FakeEmbeddings:
    """Records how the embedding client was configured instead of calling a provider."""

    made: list[dict] = []

    def __init__(self, **kwargs):
        FakeEmbeddings.made.append(kwargs)

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def fake_client(monkeypatch):
    FakeEmbeddings.made = []
    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", FakeEmbeddings)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    return FakeEmbeddings.made


def test_default_embeddings_are_gemini_through_openrouter(fake_client, monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert rag._embed(["lava"]) == [[1.0, 0.0]]
    [kwargs] = fake_client
    assert kwargs["model"] == "google/gemini-embedding-2"
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert kwargs["api_key"] == "test-openrouter-key"
    # Otherwise the client sends OpenAI token ids, which other providers reject.
    assert kwargs["check_embedding_ctx_length"] is False


def test_embedding_model_is_configurable(fake_client, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "openai:text-embedding-3-small")
    rag._embed(["lava"])
    [kwargs] = fake_client
    assert kwargs["model"] == "text-embedding-3-small"
    assert "base_url" not in kwargs and "api_key" not in kwargs  # OpenAI directly, OPENAI_API_KEY


def test_embedding_calls_are_bounded(fake_client, monkeypatch):
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "7")
    rag._embed(["lava"])
    [kwargs] = fake_client
    assert kwargs["timeout"] == 7 and kwargs["max_retries"] == 0


def test_unknown_embedding_provider_falls_back(fake_client, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "acme:embed-1")
    rag.index("unknown-provider", [Page(number=1, text="Volcanoes and lava", via="text")])
    assert fake_client == [] and rag.retrieve("unknown-provider", "lava") == []


def test_index_without_embeddings_is_skipped(monkeypatch):
    monkeypatch.setattr(rag, "_embed", no_embeddings)
    rag.index("no-embeddings", [Page(number=1, text="Volcanoes and lava", via="text")])
    assert rag.retrieve("no-embeddings", "lava") == []  # nothing indexed: callers fall back to BM25


def test_retrieve_without_embeddings_falls_back(monkeypatch):
    monkeypatch.setattr(rag, "_embed", lambda texts: [[1.0, 0.0]] * len(texts))
    rag.index("embed-later-fails", [Page(number=1, text="Volcanoes and lava", via="text")])
    monkeypatch.setattr(rag, "_embed", no_embeddings)
    assert rag.retrieve("embed-later-fails", "lava") == []


def test_pdf_upload_succeeds_without_embeddings(client, monkeypatch):
    monkeypatch.setattr(rag, "index", real_index)
    monkeypatch.setattr(rag, "retrieve", real_retrieve)
    monkeypatch.setattr(rag, "_embed", no_embeddings)
    r = client.post(
        "/api/materials", files=[("files", ("book.pdf", make_pdf(), "application/pdf"))]
    )
    assert r.status_code == 201, r.text
    assert r.json()["text_pages"] == 6
