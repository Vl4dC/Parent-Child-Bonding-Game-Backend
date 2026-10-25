"""Duel rewards come from frozen server results, never client scores."""

from datetime import datetime, timezone

import pytest

from app import economy
from learning_service import matches
from tests.learning import test_matches
from tests.learning.test_matches import (
    ALEX,
    CORRECT,
    HEADERS,
    KIM,
    SAM,
    answer,
    profiles,
    setup,
    start,
)
from tests.learning.test_tournament import create

clock = test_matches.clock


def claim(client, tid, **kwargs):
    return client.post("/api/me/coins/claim-duel", json={"tournament_id": tid}, **kwargs)


def quests(client, **kwargs):
    return {q["id"]: q for q in client.get("/api/me/quests", **kwargs).json()["items"]}


def test_same_device_duel_claim_is_once_and_does_not_reward_guest(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    assert claim(client, tid).status_code == 409
    body = {"answers": [[1, 0], [0, 1]]}
    submitted = client.post(f"/api/tournament/{tid}/duel", json=body)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["players"][0]["score"] == 1
    assert submitted.json()["reward_coins"] == 30
    assert client.post(f"/api/tournament/{tid}/duel", json=body).json() == submitted.json()
    assert (
        client.post(f"/api/tournament/{tid}/duel", json={"answers": [[1, 1], [0, 1]]}).status_code
        == 409
    )
    assert claim(client, tid).json() == {"coins": 270, "credited": 30}
    assert claim(client, tid).json() == {"coins": 270, "credited": 0}
    assert claim(client, tid, headers=HEADERS[SAM]).status_code == 404
    assert client.get("/api/me/wallet", headers=HEADERS[SAM]).json()["coins"] == 240
    q = quests(client)["d-duel"]
    assert q["progress"] == 1
    claimed = client.post("/api/me/quests/d-duel/claim", json={"period": q["period"]})
    assert claimed.json() == {"coins": 310, "credited": 40}


@pytest.mark.parametrize(
    "body",
    [
        {"answers": [[1, 0]]},
        {"answers": [[1], [0, 1]]},
        {"answers": [[1, 0], [99, 1]]},
        {"answers": [[1, 0], [0, 1]], "score": 50},
    ],
)
def test_bad_duels_are_atomic_and_do_not_reward(client, lesson_id, body):
    tid = create(client, lesson_id)["id"]
    assert client.post(f"/api/tournament/{tid}/duel", json=body).status_code == 422
    assert claim(client, tid).status_code == 409
    assert quests(client)["d-duel"]["progress"] == 0
    assert (
        client.post(f"/api/tournament/{tid}/duel", json={"answers": [[1, 0], [0, 1]]}).status_code
        == 200
    )


def test_solo_result_cannot_be_reused_as_duel_after_answer_reveal(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 0]}).status_code == 200
    assert (
        client.post(f"/api/tournament/{tid}/duel", json={"answers": [[1, 0], [0, 1]]}).status_code
        == 409
    )
    assert claim(client, tid).status_code == 409


def test_three_distinct_duels_unlock_weekly_reward(client, lesson_id, monkeypatch):
    at = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(economy, "clock", lambda: at)
    for _ in range(3):
        tid = create(client, lesson_id)["id"]
        body = {"answers": [[1, 0], [0, 1]]}
        assert client.post(f"/api/tournament/{tid}/duel", json=body).status_code == 200
        client.post(f"/api/tournament/{tid}/duel", json=body)
    q = quests(client)["w-duels"]
    assert q["progress"] == 3
    response = client.post("/api/me/quests/w-duels/claim", json={"period": q["period"]})
    assert response.json() == {"coins": 490, "credited": 250}
    at = datetime(2026, 10, 25, 23, tzinfo=timezone.utc)
    q = quests(client)["w-duels"]
    assert q["progress"] == 0 and not q["claimed"]
    assert (
        client.post("/api/me/quests/w-duels/claim", json={"period": q["period"]}).status_code == 409
    )


def test_parent_child_match_uses_each_players_score_and_completion_period(
    client, models, clock, monkeypatch
):
    profiles(client.app.state.memory, alex="parent", sam="learner")
    clock.now = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(
        economy, "clock", lambda: datetime.fromtimestamp(clock.now / 1000, timezone.utc)
    )
    mid, _ = setup(client, "mcq", "parent_child", others=[SAM], count=5, seconds=30)
    body = {"match_id": mid}
    url = "/api/me/coins/claim-duel"
    assert client.post(url, json=body).status_code == 409
    play = start(client, mid, clock)
    clock.now = play + 1000
    assert answer(client, mid, 0, CORRECT, ALEX).status_code == 200
    assert answer(client, mid, 0, 0, SAM).status_code == 200
    doc = client.app.state.memory.get_match(ALEX, mid)
    clock.now = matches.mcq_end(doc, 4) + matches.GRACE_MS + 1
    assert client.post(url, json=body, headers=HEADERS[KIM]).status_code == 404
    assert client.post(url, json=body).json() == {"coins": 270, "credited": 30}
    assert client.post(url, json=body).json()["credited"] == 0
    assert quests(client)["d-duel"]["progress"] == 1
    # A later claim earns the balance but cannot turn last week's play into this week's quest progress.
    clock.now = int(datetime(2026, 10, 26, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert client.post(url, json=body, headers=HEADERS[SAM]).json() == {
        "coins": 260,
        "credited": 20,
    }
    assert quests(client, headers=HEADERS[SAM])["d-duel"]["progress"] == 0


def test_unplayed_parent_child_match_and_solo_match_do_not_pay(client, models, clock):
    profiles(client.app.state.memory, alex="parent", sam="learner")
    for mode, others in [("parent_child", [SAM]), ("solo", [])]:
        mid, _ = setup(client, "mcq", mode, others=others, count=5, seconds=30)
        start(client, mid, clock)
        doc = client.app.state.memory.get_match(ALEX, mid)
        clock.now = matches.mcq_end(doc, 4) + matches.GRACE_MS + 1
        assert client.post("/api/me/coins/claim-duel", json={"match_id": mid}).status_code == 409
    assert client.get("/api/me/wallet").json()["coins"] == 240


def test_zero_correct_answers_still_earn_duel_participation_coins(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    response = client.post(f"/api/tournament/{tid}/duel", json={"answers": [[0, 0], [1, 1]]})
    assert response.status_code == 200
    assert response.json()["players"][0]["score"] == 0
    assert response.json()["reward_coins"] == 20
    assert claim(client, tid).json() == {"coins": 260, "credited": 20}
    assert claim(client, tid).json() == {"coins": 260, "credited": 0}


@pytest.mark.parametrize("participation_first", [False, True])
def test_duel_and_participation_claims_cannot_pay_bonus_twice(
    client, lesson_id, participation_first
):
    tid = create(client, lesson_id)["id"]
    assert (
        client.post(f"/api/tournament/{tid}/duel", json={"answers": [[1, 0], [0, 1]]}).status_code
        == 200
    )

    def participation():
        return client.post(
            "/api/me/coins/claim-participation", json={"source": "tournament", "id": tid}
        )

    if participation_first:
        assert participation().json()["credited"] == 20
        assert claim(client, tid).json()["credited"] == 10
    else:
        assert claim(client, tid).json()["credited"] == 30
        assert participation().json()["credited"] == 0
    assert claim(client, tid).json() == {"coins": 270, "credited": 0}
    assert participation().json() == {"coins": 270, "credited": 0}


def test_solo_tournament_participation(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    url = "/api/me/coins/claim-participation"
    body = {"source": "tournament", "id": tid}
    assert client.post(url, json=body).status_code == 409
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [0, 0]}).status_code == 200
    assert client.post(url, json=body).json() == {"coins": 260, "credited": 20}
    assert client.post(url, json=body).json()["credited"] == 0


@pytest.mark.parametrize("game_type", ["mcq", "heads_up", "diagram"])
def test_match_participation_pays_all_supported_games_even_without_a_win(
    client, models, clock, game_type
):
    from tests.learning.test_matches import arrangement, finish, mark, place

    others = [SAM] if game_type == "heads_up" else []
    mid, cid = setup(client, game_type, "friends" if others else "solo", others=others)
    body = {"source": "match", "id": mid}
    url = "/api/me/coins/claim-participation"
    assert client.post(url, json=body).status_code == 409
    clock.now = start(client, mid, clock) + 1000
    if game_type == "mcq":
        assert answer(client, mid, 0, 0).status_code == 200  # wrong
    elif game_type == "heads_up":
        assert mark(client, mid, "skip", 0).status_code == 200  # zero points
    else:
        assert place(client, mid, arrangement(client, cid, wrong=True)).status_code == 200
        assert finish(client, mid).json()["perfect"] is False
    clock.now += 60 * 60 * 1000
    assert client.post(url, json=body, headers=HEADERS[KIM]).status_code == 404
    assert client.post(url, json=body).json() == {"coins": 260, "credited": 20}
    assert client.post(url, json=body).json()["credited"] == 0
    if game_type == "heads_up":
        # The explainer also took part in the turn that advanced a card.
        assert client.post(url, json=body, headers=HEADERS[SAM]).json()["credited"] == 20


def test_idle_match_does_not_earn_participation(client, models, clock):
    mid, _ = setup(client, "mcq", "solo")
    clock.now = start(client, mid, clock) + 60 * 60 * 1000
    response = client.post("/api/me/coins/claim-participation", json={"source": "match", "id": mid})
    assert response.status_code == 409
