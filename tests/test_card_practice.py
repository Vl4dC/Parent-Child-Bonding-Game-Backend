"""Saved-card attempts never call a model; both persistence adapters exercise the same API."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.card_practice import CardPractice
from app.config import Settings
from app.main import create_app
from learning_service.flashcards import Flashcard, StoredDeck, start

OWNER = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def cards(repository, monkeypatch):
    import learning_service.flashcards as module
    import learning_service.safety as safety

    def no_ai(*args, **kwargs):
        pytest.fail("Deterministic card practice called an AI provider")

    monkeypatch.setattr(module, "make_cards", no_ai)
    monkeypatch.setattr(module, "_llm", no_ai)
    monkeypatch.setattr(safety, "ensure_safe", no_ai)
    with repository.session(OWNER) as unit:
        unit.put("lessons", "lesson", {"id": "lesson", "status": "approved"})
        deck = StoredDeck(
            id="deck",
            lesson_id="lesson",
            title="Recall",
            cards=[Flashcard(kind="concept", front=f"Q{i}", back=f"A{i}") for i in range(3)],
            progress=start(3),
        )
        unit.put("decks", "deck", deck)
    app = create_app(
        Settings(app_env="test", flashcard_attempts_enabled=True, _env_file=None),
        repository=repository,
    )
    with TestClient(app) as client:
        yield client, repository


def create(client, key="create-attempt-1", which="all"):
    return client.post(
        "/api/flashcards/deck/attempts", json={"which": which}, headers={"Idempotency-Key": key}
    )


def rate(client, attempt, result="know", key=None):
    return client.post(
        f"/api/flashcard-attempts/{attempt['id']}/reviews",
        json={
            "expected_version": attempt["version"],
            "card": attempt["state"]["next"],
            "result": result,
        },
        headers={"Idempotency-Key": key or "review-attempt-" + str(attempt["version"])},
    )


def test_complete_and_restart_without_changing_original_deck(cards):
    client, repo = cards
    response = create(client)
    assert response.status_code == 201, response.text
    attempt = response.json()
    assert attempt["state"] == {"next": 0, "known": 0, "total": 3, "reviews": 0, "hard": []}
    assert create(client).json() == attempt
    assert create(client, which="hard").status_code == 409
    for i in range(3):
        response = rate(client, attempt)
        assert response.status_code == 200, response.text
        attempt = response.json()
    assert attempt["status"] == "completed" and attempt["completed_at"] is not None
    assert client.get("/api/me/progress").json()["flashcard_reviews"] == 3
    frozen = client.get(f"/api/flashcard-attempts/{attempt['id']}/results").json()
    restarted = client.post(
        f"/api/flashcard-attempts/{attempt['id']}/restart",
        json={},
        headers={"Idempotency-Key": "restart-original"},
    )
    assert restarted.status_code == 201, restarted.text
    again = restarted.json()
    assert again["id"] != attempt["id"] and again["source_hash"] == attempt["source_hash"]
    assert again["state"]["known"] == again["state"]["reviews"] == 0
    assert client.get(f"/api/flashcard-attempts/{attempt['id']}/results").json() == frozen
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.get("decks", "deck")["progress"]["reviews"] == 0


def test_again_loop_hard_restart_and_partial_history(cards):
    client, _ = cards
    original = create(client).json()
    attempt = rate(client, original, "again").json()
    assert attempt["state"]["next"] == 1 and attempt["state"]["hard"] == [0]
    restart = client.post(
        f"/api/flashcard-attempts/{attempt['id']}/restart",
        json={"which": "hard"},
        headers={"Idempotency-Key": "hard-restart-key"},
    )
    assert restart.status_code == 201, restart.text
    new = restart.json()
    assert len(new["cards"]) == 1 and new["cards"][0]["index"] == 0
    old = client.get(f"/api/flashcard-attempts/{attempt['id']}/results").json()
    assert old["status"] == "abandoned" and old["state"]["reviews"] == 1
    assert rate(client, attempt).status_code == 409
    assert rate(client, original, "again").json() == attempt  # historical exact receipt
    assert rate(client, new).json()["status"] == "completed"
    first = client.get("/api/flashcards/deck/attempts?limit=1").json()
    second = client.get(
        "/api/flashcards/deck/attempts", params={"after": first["next_cursor"]}
    ).json()
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_versions_next_card_and_strict_inputs(cards):
    client, _ = cards
    attempt = create(client).json()
    path = f"/api/flashcard-attempts/{attempt['id']}/reviews"
    headers = {"Idempotency-Key": "strict-review-test"}
    body = {"expected_version": 1, "card": 2, "result": "know"}
    assert client.post(path, json=body, headers=headers).status_code == 409
    body["card"] = 0
    for update in [
        {"score": 999},
        {"expected_version": True},
        {"card": True},
        {"result": "correct"},
    ]:
        assert client.post(path, json=body | update, headers=headers).status_code == 422
    assert client.post(path, json=body).status_code == 422
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200
    assert client.post(path, json=body, headers=headers).json() == result.json()
    assert client.post(path, json=body | {"result": "again"}, headers=headers).status_code == 409
    assert (
        client.post(path, json=body, headers={"Idempotency-Key": "different-new-key"}).status_code
        == 409
    )


def test_owner_isolation_and_source_deletion(cards):
    client, repo = cards
    attempt = create(client).json()
    headers = {"X-Player-ID": OTHER, "Idempotency-Key": "other-player-key"}
    assert create(client, which="hard").status_code == 409
    assert (
        client.get(f"/api/flashcard-attempts/{attempt['id']}", headers=headers).status_code == 404
    )
    assert client.get("/api/flashcards/deck/attempts", headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/flashcard-attempts/{attempt['id']}/restart", json={}, headers=headers
        ).status_code
        == 404
    )
    with repo.session(OWNER) as unit:
        unit.delete("decks", "deck")
    assert client.get(f"/api/flashcard-attempts/{attempt['id']}").status_code == 404
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.get("card_attempts", attempt["id"]) is None
        assert unit.list("card_attempt_keys") == []


@pytest.mark.parametrize("delete", ["lesson", "all_data"])
def test_source_and_app_data_deletion_erase_attempts(cards, delete):
    client, repo = cards
    attempt = create(client).json()
    if delete == "lesson":
        with repo.session(OWNER) as unit:
            unit.delete("lessons", "lesson")
    else:
        assert client.delete("/api/me/data").status_code == 204
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.get("card_attempts", attempt["id"]) is None
        assert unit.list("card_attempt_keys") == []


def test_concurrent_creation_reviews_and_quota(cards):
    client, repo = cards
    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(lambda _: create(client), range(4)))
    assert all(r.status_code == 201 for r in out)
    assert len({r.json()["id"] for r in out}) == 1
    attempt = out[0].json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(lambda _: rate(client, attempt), range(4)))
    assert all(r.status_code == 200 for r in out)
    assert {r.json()["state"]["reviews"] for r in out} == {1}
    service = CardPractice(repo, attempt_limit=1)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        service.create(OWNER, "deck", "all", "limit-other-key")
    assert error.value.status_code == 429
    assert service.create(OWNER, "deck", "all", "create-attempt-1")["id"] == attempt["id"]


def test_frozen_cards_are_not_edited_by_source_progress_or_source_content(cards):
    client, repo = cards
    attempt = create(client).json()
    with repo.session(OWNER) as unit:
        source = unit.get("decks", "deck", StoredDeck)
        source.cards[0].front = "Changed source"
    assert (
        client.get(f"/api/flashcard-attempts/{attempt['id']}").json()["cards"] == attempt["cards"]
    )
    new = create(client, key="new-source-snapshot").json()
    assert new["source_hash"] != attempt["source_hash"]
    assert new["cards"][0]["front"] == "Changed source"
