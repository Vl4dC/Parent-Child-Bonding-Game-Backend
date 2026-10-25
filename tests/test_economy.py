"""Wallet integration with committed learning activity; no AI or network calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app import economy
from app.repository import current_session
from learning_service.flashcards import Flashcard, StoredDeck, start
from learning_service.home_store import HomeError, HomeStore
from tests import test_quiz_practice
from tests.test_quiz_practice import PARENT, answer

practice = test_quiz_practice.practice

OTHER = {"X-Player-ID": "00000000-0000-4000-8000-000000000002"}


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        at = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)

    fake = Clock()
    monkeypatch.setattr(economy, "clock", lambda: fake.at)
    return fake


def listing(client):
    response = client.get("/api/me/quests")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return {q["id"]: q for q in response.json()["items"]}


def claim(client, id, period=None):
    return client.post(
        f"/api/me/quests/{id}/claim", json={"period": period or listing(client)[id]["period"]}
    )


def test_lesson_and_upload_quests_record_completion_not_generation(practice, clock):
    client, _, quiz = practice
    assert listing(client)["d-lesson"]["progress"] == 0
    assert claim(client, "d-lesson").status_code == 409
    assert client.post(f"/api/lessons/{quiz.lesson_id}/complete").status_code == 200
    assert client.post(f"/api/lessons/{quiz.lesson_id}/complete").status_code == 200
    quests = listing(client)
    assert quests["d-lesson"]["progress"] == quests["w-lessons"]["progress"] == 1
    assert quests["w-upload"]["progress"] == 1
    assert claim(client, "d-lesson").json() == {"coins": 300, "credited": 60}
    assert claim(client, "d-lesson").json() == {"coins": 300, "credited": 0}
    assert listing(client)["d-lesson"]["claimed"]
    assert client.get("/api/me/quests", headers=OTHER).json()["items"][0]["progress"] == 0
    assert client.get("/api/me/wallet", headers=OTHER).json()["coins"] == 240


def test_daily_and_monday_resets_do_not_replay_old_activity(practice, clock):
    client, repo, quiz = practice
    client.post(f"/api/lessons/{quiz.lesson_id}/complete")
    old = listing(client)["d-lesson"]["period"]
    claim(client, "d-lesson")
    clock.at = datetime(2026, 10, 25, 23, tzinfo=timezone.utc)  # Monday midnight in Berlin
    assert listing(client)["d-lesson"]["progress"] == 0
    assert listing(client)["w-lessons"]["progress"] == 0
    assert claim(client, "d-lesson", old).status_code == 409
    client.post(f"/api/lessons/{quiz.lesson_id}/complete")
    assert listing(client)["d-lesson"]["progress"] == 0
    with repo.session(PARENT) as unit:
        fresh = unit.get("lessons", quiz.lesson_id).copy()
        fresh["id"] = "new-lesson"
        unit.put("lessons", fresh["id"], fresh)
    assert client.post("/api/lessons/new-lesson/complete").status_code == 200
    assert claim(client, "d-lesson").json() == {"coins": 360, "credited": 60}
    assert listing(client)["w-upload"]["progress"] == 0  # same upload was learned last week


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_reset_boundaries_use_local_calendar_across_dst(day, hours):
    at = datetime.fromisoformat(day).replace(tzinfo=economy.ZONE)
    key, start_at, end = economy.period("daily", at)
    assert key == day
    assert (
        end.astimezone(timezone.utc) - start_at.astimezone(timezone.utc)
    ).total_seconds() == hours * 3600
    assert end.hour == 0


def test_iso_year_week_boundary():
    key, _, end = economy.period("weekly", datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert key == "2026-12-28" and end.date().isoformat() == "2027-01-04"


def test_both_quiz_answer_apis_count_first_answers_only(practice, clock):
    client, _, quiz = practice
    assert answer(client, quiz.id, 0, 1, 0).status_code == 200
    assert answer(client, quiz.id, 0, 1, 0).status_code == 200
    assert (
        client.post(f"/api/quiz/{quiz.id}/check", json={"index": 0, "choice": 0}).status_code == 200
    )
    assert (
        client.post(f"/api/quiz/{quiz.id}/check", json={"index": 1, "choice": 0}).status_code == 200
    )
    assert listing(client)["d-quiz"]["progress"] == 2
    assert (
        client.post(f"/api/quiz/{quiz.id}/check", json={"index": 2, "choice": 99}).status_code
        == 422
    )
    assert listing(client)["d-quiz"]["progress"] == 2


def test_flashcard_review_replays_and_weak_deck_completion(practice, clock):
    client, repo, quiz = practice
    deck = StoredDeck(
        id="cards",
        lesson_id=quiz.lesson_id,
        title="Mistakes",
        weak_practice=True,
        cards=[Flashcard(index=i, kind="concept", front=f"Q{i}", back="A") for i in range(8)],
        progress=start(8),
    )
    with repo.session(PARENT) as unit:
        unit.put("decks", deck.id, deck)
    for i in range(8):
        body = {"card": i, "result": "know", "expected_reviews": i}
        headers = {"Idempotency-Key": f"card-review-{i}"}
        result = client.post("/api/flashcards/cards/review", json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert (
            client.post("/api/flashcards/cards/review", json=body, headers=headers).status_code
            == 200
        )
        if i < 7:
            assert listing(client)["w-weak"]["progress"] == 0
    assert listing(client)["d-cards"]["progress"] == 8
    assert listing(client)["w-weak"]["progress"] == 1
    assert claim(client, "d-cards").json() == {"coins": 280, "credited": 40}


def test_failed_learning_transaction_cannot_advance_quests(practice, clock):
    client, repo, _ = practice
    with pytest.raises(RuntimeError), repo.session(PARENT) as unit:
        token = current_session.set(unit)
        try:
            economy.record("lesson", "rolled-back")
            raise RuntimeError("transaction failed")
        finally:
            current_session.reset(token)
    assert listing(client)["d-lesson"]["progress"] == 0


def test_shop_validates_price_ownership_slots_and_uses_home_wallet(practice):
    client, _, _ = practice
    catalog = client.get("/api/shop/catalog").json()
    clothing = [x for x in catalog if x["asset_pack"] == "atelier-r6"]
    assert len(clothing) == 120 and {x["price"] for x in clothing} == {100}
    assert client.get("/api/me/inventory").json() == {
        "owned": ["cat", "nova"],
        "equipped": {"look": "nova", "pet": "cat"},
    }
    assert client.put("/api/me/equipment/top", json={"item_id": "top.navy"}).status_code == 409
    assert (
        client.post("/api/me/shop/purchases", json={"item_id": "top.navy", "price": 1}).status_code
        == 422
    )
    bought = client.post("/api/me/shop/purchases", json={"item_id": "top.navy"})
    assert bought.status_code == 200, bought.text
    assert bought.json()["coins"] == 140 and bought.json()["charged"] == 100
    assert (
        client.post("/api/me/shop/purchases", json={"item_id": "top.navy"}).json()["charged"] == 0
    )
    assert client.post("/api/me/shop/purchases", json={"item_id": "mia"}).status_code == 402
    assert client.post("/api/me/shop/purchases", json={"item_id": "missing"}).status_code == 404
    assert client.put("/api/me/equipment/shoes", json={"item_id": "top.navy"}).status_code == 422
    assert (
        client.put("/api/me/equipment/top", json={"item_id": "top.navy"}).json()["equipped"]["top"]
        == "top.navy"
    )
    assert (
        client.put("/api/me/equipment/top", json={"item_id": None}).json()["equipped"]["top"]
        is None
    )
    assert client.put("/api/me/equipment/pet", json={"item_id": None}).status_code == 200
    assert client.get("/api/me/inventory").json()["equipped"]["pet"] is None
    assert client.post("/api/me/home/items/living/table/place", json={}).json()["coins"] == 110
    wallet = client.get("/api/me/wallet").json()
    assert sum(x["delta"] for x in wallet["transactions"]) == wallet["coins"] == 110
    first = client.get("/api/me/wallet?limit=1").json()
    second = client.get(f"/api/me/wallet?after={first['next_cursor']}").json()
    assert len(second["transactions"]) == 2
    assert client.get("/api/me/inventory", headers=OTHER).json()["owned"] == ["cat", "nova"]
    assert client.delete("/api/me/data").status_code == 204
    assert client.get("/api/me/inventory").json()["owned"] == ["cat", "nova"]
    assert client.get("/api/me/wallet").json()["coins"] == 240


def test_delete_removes_quest_activity_and_claims(practice, clock):
    client, _, quiz = practice
    client.post(f"/api/lessons/{quiz.lesson_id}/complete")
    claim(client, "d-lesson")
    assert client.delete("/api/me/data").status_code == 204
    assert listing(client)["d-lesson"]["progress"] == 0
    assert not listing(client)["d-lesson"]["claimed"]


def test_shop_and_reward_transactions_survive_restart_and_concurrency(tmp_path):
    path = str(tmp_path / "wallet.db")
    stores = [HomeStore(path), HomeStore(path)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: stores[i % 2].purchase(PARENT, "top.navy"), range(12)))
        assert sum(r["charged"] for r in results) == 100
        paid = list(
            pool.map(
                lambda i: stores[i % 2].credit(PARENT, "quest", [("d-lesson/2026-10-25", 60)]),
                range(12),
            )
        )
        assert sum(r[1] for r in paid) == 60
    stores[0].equip(PARENT, "top", "top.navy")
    for store in stores:
        store.close()
    store = HomeStore(path)
    assert store.inventory(PARENT)["equipped"]["top"] == "top.navy"
    assert store.wallet(PARENT, 0, 100)["coins"] == 200
    assert "d-lesson/2026-10-25" in store.claimed_quests(PARENT)
    store.close()


def test_concurrent_distinct_purchases_cannot_overdraw(tmp_path):
    path = str(tmp_path / "spend.db")
    a, b = HomeStore(path), HomeStore(path)

    def buy(item):
        try:
            return a.purchase(PARENT, item) if item == "top.navy" else b.purchase(PARENT, item)
        except HomeError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(buy, ["top.navy", "top.white_tee", "top.hoodie"]))
    assert results.count(402) == 1
    assert a.wallet(PARENT, 0, 100)["coins"] == 40
    a.close()
    b.close()


def test_weekly_lesson_upload_targets_and_repeat_claims(practice, clock):
    client, repo, quiz = practice
    for i in range(5):
        with repo.session(PARENT) as unit:
            lesson = unit.get("lessons", quiz.lesson_id).copy()
            lesson["id"] = f"lesson-{i}"
            lesson["source"] = {**lesson["source"], "material_id": f"upload-{i}"}
            unit.put("lessons", lesson["id"], lesson)
        assert client.post(f"/api/lessons/lesson-{i}/complete").status_code == 200
        if i < 2:
            assert claim(client, "w-upload").status_code == 409
        if i < 4:
            assert claim(client, "w-lessons").status_code == 409
    assert claim(client, "w-upload").json() == {"coins": 360, "credited": 120}
    assert claim(client, "w-lessons").json() == {"coins": 560, "credited": 200}
    assert claim(client, "w-lessons").json()["credited"] == 0
    assert listing(client)["w-upload"]["progress"] == 3


def test_completed_missed_retries_earn_weak_and_answer_quests(practice, clock):
    client, _, quiz = practice
    for i, choice in enumerate([0, 1, 0]):
        assert answer(client, quiz.id, i, choice, i).status_code == 200
    for n in range(2):
        made = client.post(
            f"/api/quiz/{quiz.id}/retry",
            json={"which": "missed"},
            headers={"Idempotency-Key": f"weak-retry-{n}"},
        )
        assert made.status_code == 201, made.text
        qid = made.json()["id"]
        assert listing(client)["w-weak"]["progress"] == n
        for i, choice in enumerate([1, 0, 2]):
            assert answer(client, qid, i, choice, i).status_code == 200
    assert claim(client, "w-weak").json() == {"coins": 390, "credited": 150}
    assert claim(client, "d-quiz").json() == {"coins": 440, "credited": 50}
    assert claim(client, "d-quiz").json()["credited"] == 0
    assert (
        client.post("/api/me/quests/not-real/claim", json={"period": "2026-10-25"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/me/quests/d-quiz/claim", json={"period": "2026-10-25", "reward": 9000}
        ).status_code
        == 422
    )


def test_quiz_participation_requires_all_answers_but_not_correctness(practice):
    client, _, quiz = practice
    url = "/api/me/coins/claim-participation"
    body = {"source": "quiz", "id": quiz.id}
    assert client.post(url, json=body).status_code == 409
    for i, choice in enumerate([0, 1, 0]):
        assert answer(client, quiz.id, i, choice, i).status_code == 200
    assert client.post(url, json=body, headers=OTHER).status_code == 404
    assert (
        client.post("/api/me/coins/claim-quiz", json={"quiz_id": quiz.id}).json()["credited"] == 0
    )
    assert client.post(url, json=body).json() == {"coins": 260, "credited": 20}
    assert client.post(url, json=body).json() == {"coins": 260, "credited": 0}
    assert client.post(url, json=body | {"amount": 1000}).status_code == 422


def test_concurrent_duel_and_participation_claims_share_one_bonus(tmp_path):
    stores = [HomeStore(str(tmp_path / "shared.db")) for _ in range(2)]

    def pay(i):
        entries = [("participation", "tournament/round-one", 20)]
        if i % 2:
            entries.append(("duel", "tournament/round-one", 30))
        return stores[i % 2].credit_rewards(PARENT, entries)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pay, range(12)))
    assert sum(r[1] for r in results) == 50
    assert stores[0].wallet(PARENT, 0, 100)["coins"] == 290
    for store in stores:
        store.close()
