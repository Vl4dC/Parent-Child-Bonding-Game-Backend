"""Upload a book, study part of it. Uses real PDFs and pictures; only the model calls are faked."""

import learning_service.materials as materials
from learning_service.lesson import NotCovered
from tests.learning.files import make_pdf, make_png, make_scanned_pdf


def upload(client, *files):
    return client.post("/api/materials", files=[("files", f) for f in files])


def book(client, pdf=None):
    r = upload(client, ("book.pdf", pdf or make_pdf(), "application/pdf"))
    assert r.status_code == 201, r.text
    key = r.json()["id"]
    assert (
        client.post(
            f"/api/materials/{key}/confirm", json={"expected_revision": 1, "confirmed": True}
        ).status_code
        == 200
    )
    return r.json()


# ---------------------------------------------------------------- upload
def test_upload_a_text_pdf(client):
    info = book(client)
    assert info["name"] == "book.pdf" and info["kind"] == "pdf"
    assert (info["page_count"], info["text_pages"], info["vision_pages"], info["blank_pages"]) == (
        6,
        6,
        0,
        0,
    )
    assert [c["title"] for c in info["chapters"]] == [
        "Chapter 1: Plants and light",
        "Chapter 2: Volcanoes and lava",
    ]
    assert info["warnings"] == [] and "vision_calls" not in client.seen  # a text PDF costs nothing
    assert client.get(f"/api/materials/{info['id']}").json() == info


def test_upload_photos_are_read_by_the_vision_model(client):
    r = upload(
        client, ("p1.png", make_png(), "image/png"), ("p2.png", make_png(610, 800), "image/png")
    )
    assert r.status_code == 201
    info = r.json()
    assert info["kind"] == "photos" and info["page_count"] == 2 and info["vision_pages"] == 2
    assert client.seen["vision_calls"] == 2


def test_upload_a_scanned_pdf(client):
    info = book(client, make_scanned_pdf(3))
    assert (
        info["vision_pages"] == 3 and info["text_pages"] == 0 and client.seen["vision_calls"] == 3
    )


def test_bad_uploads_are_refused_with_the_right_status(client):
    assert upload(client, ("notes.txt", b"hello", "text/plain")).status_code == 415
    assert (
        upload(
            client,
            ("a.pdf", make_pdf(), "application/pdf"),
            ("b.pdf", make_pdf(), "application/pdf"),
        ).status_code
        == 422
    )
    assert upload(client, ("empty.pdf", b"", "application/pdf")).status_code == 422
    assert client.post("/api/materials").status_code == 422  # no file at all


def test_oversized_uploads_are_refused_before_and_while_reading(client, monkeypatch):
    monkeypatch.setattr(
        materials, "MAX_FILE_BYTES", 100
    )  # smaller than any PNG header block + data
    r = upload(client, ("big.png", make_png(300, 300), "image/png"))
    assert r.status_code == 413 and "allowed size" in r.json()["detail"]
    monkeypatch.setattr(materials, "MAX_FILE_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(materials, "MAX_TOTAL_BYTES", 100)
    r = client.post(
        "/api/materials",
        files=[("files", ("p.png", make_png(600, 800), "image/png"))],
        headers={"content-length": str(5 * 2**20)},
    )  # a client announcing a 5 MB body
    assert r.status_code == 413 and "too big" in r.json()["detail"]


def test_when_vision_is_down_a_scanned_upload_is_a_502(client, monkeypatch):
    def down():
        def read(_jpeg):
            raise RuntimeError("no network")

        return read

    monkeypatch.setattr(materials, "model_vision_reader", down)
    assert upload(client, ("p.png", make_png(), "image/png")).status_code == 502


def test_persistent_uploads_are_not_silently_evicted(client):
    first, second, third = book(client)["id"], book(client)["id"], book(client)["id"]
    assert client.get(f"/api/materials/{first}").status_code == 200
    assert (
        client.get(f"/api/materials/{second}").status_code == 200
        and client.get(f"/api/materials/{third}").status_code == 200
    )


def test_delete_an_upload(client):
    mid = book(client)["id"]
    assert client.delete(f"/api/materials/{mid}").status_code == 204
    assert client.get(f"/api/materials/{mid}").status_code == 404
    assert client.delete(f"/api/materials/{mid}").status_code == 404


# ---------------------------------------------------------------- previewing the pages a request would use
def test_find_previews_the_pages_without_any_model_call(client):
    mid = book(client)["id"]
    r = client.get(f"/api/materials/{mid}/find", params={"topic": "chapter 2"})
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == [4, 5, 6] and body["how"] == "chapter" and body["truncated"] is False
    assert body["preview"].startswith("[Page 4]\nChapter 2: Volcanoes and lava")
    assert client.get(f"/api/materials/{mid}/find", params={"topic": "photosynthesis"}).json()[
        "pages"
    ] == [2, 3]
    assert (
        client.get(f"/api/materials/{mid}/find", params={"topic": "pages 5-6"}).json()["how"]
        == "pages"
    )
    assert "grounded_request" not in client.seen


def test_find_errors(client):
    mid = book(client)["id"]
    r = client.get(f"/api/materials/{mid}/find", params={"topic": "quantum entanglement"})
    assert r.status_code == 422 and "couldn't find" in r.json()["detail"]
    assert (
        client.get(f"/api/materials/{mid}/find", params={"topic": "pages 40-50"}).status_code == 422
    )
    assert client.get(f"/api/materials/{mid}/find").status_code == 422  # topic is required
    assert client.get("/api/materials/nope/find", params={"topic": "x"}).status_code == 404


# ---------------------------------------------------------------- a lesson from the book
def lesson_from(client, mid, topic="photosynthesis", **extra):
    return client.post(
        "/api/lessons", json={"topic": topic, "age": 13, "material_id": mid, **extra}
    )


def test_lesson_is_built_only_from_the_requested_pages(client):
    mid = book(client)["id"]
    r = lesson_from(client, mid, "chapter 2")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == {
        "material_id": mid,
        "material_revision": 1,
        "title": "book.pdf",
        "pages": [4, 5, 6],
        "label": "Chapter 2: Volcanoes and lava (pages 4-6)",
        "truncated": False,
    }
    assert client.seen["grounded_request"].age == 13
    text = client.seen["pages_text"]
    assert (
        text.startswith("[Page 4]") and "magma" in text and "photosynthesis" not in text
    )  # nothing from other chapters
    assert "lesson_request" not in client.seen  # the general-knowledge writer was not used


def test_everything_after_the_lesson_works_unchanged(client):
    mid = book(client)["id"]
    lesson_id = lesson_from(client, mid).json()["id"]
    assert client.get(f"/api/lessons/{lesson_id}").json()["source"]["pages"] == [2, 3]
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3})
    deck = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 5})
    assert quiz.status_code == 201 and deck.status_code == 201


def test_a_lesson_without_material_id_has_no_source_and_uses_general_knowledge(client):
    body = client.post("/api/lessons", json={"topic": "volcanoes", "age": 10}).json()
    assert body["source"] is None and "lesson_request" in client.seen


def test_asking_for_something_the_book_doesnt_contain_is_a_422_not_an_invented_lesson(client):
    mid = book(client)["id"]
    r = lesson_from(client, mid, "quantum entanglement")
    assert r.status_code == 422 and "couldn't find" in r.json()["detail"]
    assert "grounded_request" not in client.seen  # stopped before spending a model call


def test_pages_that_dont_teach_the_topic_are_a_422_too(client, monkeypatch):
    import learning_service.routes as app_module

    def not_covered(_request, _pages):
        raise NotCovered("The pages only mention photosynthesis in passing.")

    monkeypatch.setattr(app_module, "make_lesson_from_pages", not_covered)
    r = lesson_from(client, book(client)["id"])
    assert (
        r.status_code == 422
        and r.json()["detail"] == "The pages only mention photosynthesis in passing."
    )


def test_lesson_model_failure_is_a_502(client, monkeypatch):
    import learning_service.routes as app_module

    def boom(_request, _pages):
        raise RuntimeError("model down")

    monkeypatch.setattr(app_module, "make_lesson_from_pages", boom)
    r = lesson_from(client, book(client)["id"])
    assert r.status_code == 502 and "model down" not in r.json()["detail"]


def test_unknown_or_deleted_material_is_a_404(client):
    assert lesson_from(client, "nope").status_code == 404
    mid = book(client)["id"]
    lesson_id = lesson_from(client, mid).json()["id"]
    client.delete(f"/api/materials/{mid}")
    assert lesson_from(client, mid).status_code == 404
    assert (
        client.post(f"/api/lessons/{lesson_id}/quiz", json={}).status_code == 404
    )  # deleting source removes derived lesson data


def test_a_long_excerpt_is_reported_as_truncated(client, monkeypatch):
    monkeypatch.setattr(materials, "EXCERPT_CHARS", 200)
    mid = book(client)["id"]
    assert lesson_from(client, mid, "chapter 2").json()["source"]["truncated"] is True
