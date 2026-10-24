"""Search by meaning over an uploaded book. The embedding provider is faked with a small concept table, so
"vegetation" lands next to "photosynthesis" without any network; everything else (PDFs, storage, routes) is real."""

import pytest

import learning_service.embeddings as emb
import learning_service.materials as mt
from app.config import Settings
from app.generation import model_settings
from app.repository import MemoryRepository
from learning_service.embeddings import Embedder, VectorIndex, chunk_pages
from learning_service.materials import NotInMaterial, Page, select_pages
from tests.learning.fakes import FakeEmbedder
from tests.learning.files import make_pdf, make_png
from tests.learning.test_materials_api import book, lesson_from, upload

real_model_embedder = (
    emb.model_embedder
)  # the conftest replaces the module's name, not this reference


def a_book() -> mt.Material:
    return mt.read_files([("book.pdf", make_pdf())], lambda _jpeg: "")


def vector_keys(repository) -> list[str]:
    if isinstance(repository, MemoryRepository):
        return sorted(key for _, kind, key in repository.data if kind == emb.KIND)
    with repository.control() as connection:
        rows = connection.execute("select id from api_private.records where kind=%s", (emb.KIND,))
        return sorted(row[0] for row in rows)


# ---------------------------------------------------------------- chunks and vectors
def test_chunks_never_cross_a_page_and_skip_blank_and_listed_pages():
    long_page = " ".join(f"word{i}" for i in range(600))  # about 4,000 characters
    pages = [
        Page(number=1, text="Contents\nChapter 1 Plants", via="text"),
        Page(number=2, text=long_page, via="text"),
        Page(number=3, text="   ", via="none"),
        Page(number=4, text="A short page.", via="text"),
    ]
    chunks = chunk_pages(pages, skip={1})
    assert {page for page, _ in chunks} == {2, 4}
    assert [page for page, _ in chunks].count(2) >= 4
    assert all(len(text) <= emb.CHUNK_CHARS for _, text in chunks)
    assert dict(chunks)[4] == "A short page."
    assert " ".join(t for p, t in chunks if p == 2).count("word599") >= 1  # nothing lost at the end


def make_index(vectors, pages=None):
    material = a_book()
    material.id = "m1"
    fake = FakeEmbedder()
    return VectorIndex.build(material, fake, pages or list(range(2, 2 + len(vectors))), vectors)


def test_vectors_are_stored_as_unit_length_and_scored_by_cosine():
    index = make_index([[3.0, 4.0], [0.0, 2.0], [-1.0, 0.0]], pages=[2, 3, 4])
    assert index.dimensions == 2 and len(index.decode()) == 6
    assert index.decode()[:2] == pytest.approx((0.6, 0.8), abs=1e-3)  # normalised
    scores = index.scores([6.0, 8.0])  # the same direction as page 2, whatever its length
    assert scores[2] == pytest.approx(1.0, abs=1e-3)
    assert scores[3] == pytest.approx(0.8, abs=1e-3)
    assert 4 not in scores  # opposite direction: below the similarity floor


def test_a_page_scores_as_its_best_chunk_and_weak_matches_are_dropped():
    index = make_index([[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]], pages=[5, 5, 6])
    assert index.scores([1.0, 0.0]) == {
        5: pytest.approx(1.0, abs=1e-3)
    }  # the weaker chunk and page 6 lose
    assert index.scores([0.0, 1.0]) == {
        5: pytest.approx(0.8, abs=1e-3),
        6: pytest.approx(1.0, abs=1e-3),
    }


def test_a_query_of_the_wrong_size_or_stored_vectors_that_dont_fit_are_not_trusted():
    index = make_index([[1.0, 0.0]], pages=[2])
    assert index.scores([1.0, 0.0, 0.0]) == {}
    broken = index.model_copy(update={"pages": [2, 3]})
    with pytest.raises(ValueError):
        broken.decode()


def test_an_embedder_sends_batches_in_parallel_but_keeps_the_order():
    sizes = []

    class Client:
        def embed_documents(self, texts):
            sizes.append(len(texts))
            return [[float(t)] for t in texts]

    texts = [str(i) for i in range(150)]
    vectors = Embedder(Client(), "x:y", 1).embed(texts)
    assert sorted(sizes) == [22, 64, 64]
    assert [v[0] for v in vectors] == [float(i) for i in range(150)]


# ---------------------------------------------------------------- choosing the provider
def settings(**kwargs):
    return Settings(app_env="test", demo_mode=True, _env_file=None, **kwargs)


def embedder_for(monkeypatch, config: Settings, **env):
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    token = model_settings.set(config)
    try:
        return real_model_embedder()
    finally:
        model_settings.reset(token)


def test_the_default_model_is_used_when_its_key_is_present(monkeypatch):
    made = embedder_for(monkeypatch, settings(), OPENROUTER_API_KEY="test-key")
    assert made.model == "openrouter:openai/text-embedding-3-small" and made.dimensions == 512
    assert made.client.model == "openai/text-embedding-3-small" and made.client.dimensions == 512
    assert (
        embedder_for(
            monkeypatch,
            settings(embedding_model="openai:text-embedding-3-large", embedding_dimensions=256),
            OPENAI_API_KEY="test-key",
        ).dimensions
        == 256
    )


@pytest.mark.parametrize(
    "config, env",
    [
        (settings(), {}),  # no key
        (settings(embedding_model=""), {"OPENROUTER_API_KEY": "k"}),  # switched off
        (settings(embedding_model=None), {"OPENROUTER_API_KEY": "k"}),
        (
            settings(embedding_model="anthropic:claude"),
            {"OPENROUTER_API_KEY": "k"},
        ),  # no such provider
        (
            settings(embedding_model="openai:text-embedding-3-small"),
            {"OPENROUTER_API_KEY": "k"},
        ),  # wrong key
    ],
)
def test_without_a_usable_model_search_by_meaning_is_simply_off(monkeypatch, config, env):
    assert embedder_for(monkeypatch, config, **env) is None


# ---------------------------------------------------------------- merging the two searches
def test_meaning_finds_pages_the_words_miss():
    material = a_book()
    with pytest.raises(NotInMaterial):
        select_pages(material, "vegetation feeding")  # no page contains these words
    picked = select_pages(material, "vegetation feeding", lambda _q: {3: 0.7})
    assert (
        picked.how == "search" and 3 in picked.pages and picked.pages == [2, 3]
    )  # + its chapter-mate


def test_pages_both_searches_agree_on_come_first():
    material = a_book()
    only_words = select_pages(material, "volcano")
    assert only_words.pages == [4, 5, 6]
    both = select_pages(material, "volcano", lambda _q: {5: 0.9})  # meaning prefers page 5
    assert both.pages == [4, 5, 6]
    agreed = select_pages(material, "photosynthesis", lambda _q: {3: 0.9, 2: 0.4})
    assert agreed.pages == [2, 3]


def test_meaning_never_adds_contents_pages_and_is_not_asked_for_ranges_or_chapters():
    material = a_book()
    assert material.contents == [1]
    asked = []

    def meaning(query):
        asked.append(query)
        return {1: 0.99}

    with pytest.raises(NotInMaterial):
        select_pages(material, "vegetation feeding", meaning)  # the only hit is the contents page
    select_pages(material, "pages 2-3", meaning)
    select_pages(material, "chapter 2", meaning)
    assert asked == ["vegetation feeding"]


def test_a_meaning_search_that_finds_nothing_leaves_the_keyword_result_alone():
    material = a_book()
    assert (
        select_pages(material, "volcano", lambda _q: {}).pages
        == select_pages(material, "volcano").pages
    )


# ---------------------------------------------------------------- upload
def test_an_upload_is_embedded_and_says_so(client, embedder):
    info = book(client)
    assert info["indexed_chunks"] == len(embedder.documents) >= 5
    assert info["warnings"] == []
    assert not any(
        "Contents" in text for text in embedder.documents
    )  # the contents page isn't embedded
    assert client.get(f"/api/materials/{info['id']}").json() == info


def test_without_an_embedding_model_nothing_changes(client):
    info = book(client)
    assert info["indexed_chunks"] == 0 and info["warnings"] == []
    mid = info["id"]
    assert lesson_from(client, mid, "photosynthesis").json()["source"]["pages"] == [2, 3]


def test_photos_are_embedded_after_the_vision_model_has_read_them(client, embedder):
    r = upload(client, ("p1.png", make_png(), "image/png"), ("p2.png", make_png(), "image/png"))
    assert r.status_code == 201 and r.json()["indexed_chunks"] == 2
    assert all("volcanoes" in text for text in embedder.documents)  # the fake vision text


def test_a_provider_outage_never_fails_the_upload(client, embedder):
    embedder.fail = True
    r = upload(client, ("book.pdf", make_pdf(), "application/pdf"))
    assert r.status_code == 201
    info = r.json()
    assert info["indexed_chunks"] == 0
    assert any("Search by meaning" in w for w in info["warnings"])
    assert client.get(f"/api/materials/{info['id']}").json()["warnings"] == info["warnings"]


# ---------------------------------------------------------------- lessons and find
def test_a_topic_in_other_words_finds_the_right_pages(client, embedder):
    mid = book(client)["id"]
    found = client.get(f"/api/materials/{mid}/find", params={"topic": "how vegetation feeds"})
    assert found.status_code == 200
    body = found.json()
    assert body["pages"] == [2, 3] and body["how"] == "search" and body["by_meaning"] is True
    lesson = lesson_from(client, mid, "how vegetation feeds")
    assert lesson.status_code == 201, lesson.text
    assert lesson.json()["source"]["pages"] == [2, 3]
    text = client.seen["pages_text"]
    assert "photosynthesis" in text and "magma" not in text


def test_a_topic_the_book_doesnt_cover_is_still_refused(client, embedder):
    mid = book(client)["id"]
    assert (
        client.get(
            f"/api/materials/{mid}/find", params={"topic": "quantum entanglement"}
        ).status_code
        == 422
    )
    r = lesson_from(client, mid, "quantum entanglement")
    assert r.status_code == 422 and "grounded_request" not in client.seen


def test_chapters_and_page_ranges_never_reach_the_provider(client, embedder):
    mid = book(client)["id"]
    for topic in ("chapter 2", "pages 5-6"):
        body = client.get(f"/api/materials/{mid}/find", params={"topic": topic}).json()
        assert body["by_meaning"] is False
        assert lesson_from(client, mid, topic).status_code == 201
    assert embedder.queries == []


def test_a_keyword_topic_reports_that_meaning_took_part_too(client, embedder):
    mid = book(client)["id"]
    body = client.get(f"/api/materials/{mid}/find", params={"topic": "photosynthesis"}).json()
    assert body["pages"] == [2, 3] and body["by_meaning"] is True


def test_an_outage_while_searching_falls_back_to_keywords(client, embedder):
    mid = book(client)["id"]
    embedder.fail = True
    body = client.get(f"/api/materials/{mid}/find", params={"topic": "volcano"}).json()
    assert body["pages"] == [4, 5, 6] and body["by_meaning"] is False
    assert lesson_from(client, mid, "volcano").status_code == 201
    r = client.get(f"/api/materials/{mid}/find", params={"topic": "how vegetation feeds"})
    assert r.status_code == 422  # words alone can't find it, and nothing is invented


def test_find_uses_an_index_but_never_builds_one(client, embedder, monkeypatch):
    # A book uploaded while embeddings were off has no index; searching it must not create one.
    monkeypatch.setattr(emb, "model_embedder", lambda: None)
    plain = book(client)["id"]
    monkeypatch.setattr(emb, "model_embedder", lambda: embedder)
    before = len(embedder.documents)
    r = client.get(f"/api/materials/{plain}/find", params={"topic": "how vegetation feeds"})
    assert r.status_code == 422 and len(embedder.documents) == before
    assert client.get(f"/api/materials/{plain}").json()["indexed_chunks"] == 0


def test_the_first_lesson_embeds_a_book_that_missed_the_upload(client, embedder, monkeypatch):
    monkeypatch.setattr(emb, "model_embedder", lambda: None)
    mid = book(client)["id"]
    monkeypatch.setattr(emb, "model_embedder", lambda: embedder)
    assert client.get(f"/api/materials/{mid}").json()["indexed_chunks"] == 0
    r = lesson_from(client, mid, "how vegetation feeds")
    assert r.status_code == 201 and r.json()["source"]["pages"] == [2, 3]
    assert client.get(f"/api/materials/{mid}").json()["indexed_chunks"] >= 5
    client.get(f"/api/materials/{mid}/find", params={"topic": "how vegetation feeds"})
    documents = len(embedder.documents)
    lesson_from(client, mid, "how vegetation feeds")
    assert len(embedder.documents) == documents  # embedded once, then reused


def test_an_index_made_by_another_model_is_rebuilt(client, embedder):
    mid = book(client)["id"]
    first = len(embedder.documents)
    embedder.model = "fake:newer"
    assert lesson_from(client, mid, "how vegetation feeds").status_code == 201
    assert len(embedder.documents) == 2 * first


# ---------------------------------------------------------------- edits and deletion
def test_correcting_the_text_drops_the_old_vectors_and_the_next_lesson_embeds_the_new_text(
    client, embedder, repository
):
    mid = book(client)["id"]
    assert vector_keys(repository) == [f"{mid}:1"]
    pages = client.get(f"/api/materials/{mid}/pages").json()["pages"]
    pages[1]["text"] = pages[1]["text"] + " Vegetation feeds on light."
    assert (
        client.put(
            f"/api/materials/{mid}/pages", json={"expected_revision": 1, "pages": pages}
        ).status_code
        == 200
    )
    assert vector_keys(repository) == []
    assert client.get(f"/api/materials/{mid}").json()["indexed_chunks"] == 0
    client.post(f"/api/materials/{mid}/confirm", json={"expected_revision": 2, "confirmed": True})
    assert lesson_from(client, mid, "how vegetation feeds").status_code == 201
    assert vector_keys(repository) == [f"{mid}:2"]
    assert client.get(f"/api/materials/{mid}").json()["indexed_chunks"] >= 5
    assert any("Vegetation feeds on light" in text for text in embedder.documents)


def test_deleting_a_book_deletes_its_vectors(client, embedder, repository):
    keep, drop = book(client)["id"], book(client)["id"]
    assert vector_keys(repository) == sorted([f"{keep}:1", f"{drop}:1"])
    assert client.delete(f"/api/materials/{drop}").status_code == 204
    assert vector_keys(repository) == [f"{keep}:1"]


def test_deleting_all_my_data_deletes_the_vectors_too(client, embedder, repository):
    book(client)
    book(client)
    assert len(vector_keys(repository)) == 2
    assert client.delete("/api/me/data").status_code == 204
    assert vector_keys(repository) == []


def test_vectors_never_appear_in_listings_or_material_details(client, embedder):
    mid = book(client)["id"]
    assert "vectors" not in client.get(f"/api/materials/{mid}").text
    listing = client.get("/api/resources/materials").json()["items"]
    assert [item["id"] for item in listing] == [mid]
    assert client.get("/api/resources/material_vectors").status_code == 422
