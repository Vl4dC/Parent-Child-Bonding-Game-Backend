import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.card_practice import CardPractice
from app.repository import PostgresRepository
from learning_service.flashcards import Flashcard, StoredDeck, start
from tests.test_card_practice import OWNER, create, rate
from tests.test_card_practice import cards as cards
from tests.test_clerk_auth import rsa_pair as rsa_pair
from tests.test_duels import OWNER as CLERK_OWNER
from tests.test_duels import duel as duel


def test_legacy_and_new_reviews_are_counted_once(cards):
    client, _ = cards
    old = client.post(
        "/api/flashcards/deck/review",
        json={"card": 0, "result": "know", "expected_reviews": 0},
        headers={"Idempotency-Key": "legacy-first-review"},
    )
    assert old.status_code == 200, old.text
    attempt = create(client).json()
    result = rate(client, attempt)
    assert result.status_code == 200
    assert rate(client, attempt).json() == result.json()
    assert client.get("/api/me/progress").json()["flashcard_reviews"] == 2


def test_source_hard_cards_and_empty_hard_restart(cards):
    client, _ = cards
    assert (
        client.post("/api/flashcards/deck/review", json={"card": 2, "result": "again"}).status_code
        == 200
    )
    attempt = create(client, which="hard").json()
    assert [c["index"] for c in attempt["cards"]] == [2] and attempt["state"]["next"] == 2
    response = client.post(
        f"/api/flashcard-attempts/{attempt['id']}/restart",
        json={"which": "hard"},
        headers={"Idempotency-Key": "empty-hard-restart"},
    )
    assert response.status_code == 409
    assert client.get(f"/api/flashcard-attempts/{attempt['id']}").json()["status"] == "active"


def test_review_racing_restart_keeps_prior_progress(cards):
    client, _ = cards
    attempt = create(client).json()

    def act(kind):
        if kind == "review":
            return rate(client, attempt)
        return client.post(
            f"/api/flashcard-attempts/{attempt['id']}/restart",
            json={},
            headers={"Idempotency-Key": "restart-race-key"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["review", "restart"]))
    assert out[0].status_code in (200, 409) and out[1].status_code == 201, [r.text for r in out]
    old = client.get(f"/api/flashcard-attempts/{attempt['id']}/results").json()
    assert old["status"] == "abandoned" and old["state"]["reviews"] == (
        1 if out[0].status_code == 200 else 0
    )
    assert out[1].json()["state"]["reviews"] == 0


def test_source_delete_racing_restart_leaves_no_orphan(cards):
    client, repo = cards
    attempt = create(client).json()

    def act(kind):
        if kind == "delete":
            with repo.session(OWNER) as unit:
                unit.delete("decks", "deck")
            return 204
        return client.post(
            f"/api/flashcard-attempts/{attempt['id']}/restart",
            json={},
            headers={"Idempotency-Key": "source-delete-restart"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["delete", "restart"]))
    assert out[0] == 204 and out[1] in (201, 404)
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.list("card_attempts") == [] and unit.list("card_attempt_keys") == []


def test_new_card_api_uses_verified_clerk_owner_not_header(duel):
    client, repo, _, _, tokens, _ = duel
    with repo.session(CLERK_OWNER) as unit:
        unit.put(
            "decks",
            "clerk-deck",
            StoredDeck(
                id="clerk-deck",
                lesson_id="lesson",
                title="Clerk recall",
                cards=[Flashcard(kind="concept", front="Q", back="A")],
                progress=start(1),
            ),
        )
    response = client.post(
        "/api/flashcards/clerk-deck/attempts",
        json={},
        headers=tokens["owner"] | {"Idempotency-Key": "clerk-card-create"},
    )
    assert response.status_code == 201, response.text
    attempt = response.json()
    attacker = tokens["other"] | {
        "X-Player-ID": CLERK_OWNER,
        "Idempotency-Key": "clerk-card-forgery",
    }
    assert (
        client.get(f"/api/flashcard-attempts/{attempt['id']}", headers=attacker).status_code == 404
    )
    assert (
        client.post(
            f"/api/flashcard-attempts/{attempt['id']}/restart", json={}, headers=attacker
        ).status_code
        == 404
    )
    assert client.get(f"/api/flashcard-attempts/{attempt['id']}").status_code == 401


def test_card_attempt_survives_repository_restart(cards):
    _, repo = cards
    if not repo.durable:
        pytest.skip("Repository restart requires PostgreSQL")
    attempt = CardPractice(repo).create(OWNER, "deck", "all", "restart-storage-key")
    fresh = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    try:
        stored = CardPractice(fresh).get(OWNER, attempt["id"])
        assert stored == attempt
    finally:
        fresh.close()


def test_missing_cleanup_trigger_blocks_direct_api_writes(cards):
    client, repo = cards
    if not repo.durable:
        pytest.skip("Trigger enforcement requires PostgreSQL")
    try:
        with repo.control() as c:
            c.execute("alter table api_private.records disable trigger card_attempt_link_insert")
        assert create(client).status_code == 503
        with repo.session(OWNER, serialize=False) as unit:
            assert unit.list("card_attempts") == []
    finally:
        with repo.control() as c:
            c.execute("alter table api_private.records enable trigger card_attempt_link_insert")


def test_oversized_frozen_card_snapshot_is_rejected(cards):
    client, repo = cards
    with repo.session(OWNER) as unit:
        unit.get("decks", "deck", StoredDeck).cards[0].back = "x" * 1_048_577
    assert create(client).status_code == 422
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.list("card_attempts") == []


def test_frozen_attempts_and_creation_receipts_survive_later_source_shape_changes(cards):
    client, repo = cards
    original = create(client).json()
    with repo.session(OWNER) as unit:
        source = unit.get("decks", "deck", StoredDeck)
        source.cards = [
            Flashcard(kind="concept", front=f"Changed {i}", back="Answer") for i in range(101)
        ]
    assert create(client).json()["cards"] == original["cards"]
    assert create(client, key="changed-large-source").status_code == 422
    assert (
        client.get(f"/api/flashcard-attempts/{original['id']}").json()["cards"] == original["cards"]
    )
    response = client.post(
        f"/api/flashcard-attempts/{original['id']}/restart",
        json={},
        headers={"Idempotency-Key": "restart-frozen-shape"},
    )
    assert response.status_code == 201 and response.json()["cards"] == original["cards"]
    assert len(client.get("/api/flashcards/deck/attempts").json()["items"]) == 2
