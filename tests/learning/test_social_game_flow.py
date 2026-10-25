"""Real invitations feed the existing challenge, game and reward APIs; generation is faked."""

from datetime import datetime, timezone

import pytest

from app import economy
from learning_service import matches
from tests.learning.test_challenges import create
from tests.test_social import A, B, act, beat, headers, invite, room, seen


@pytest.mark.parametrize(
    "game,mode,capacity",
    [
        ("mcq", "friends", 2),
        ("mcq", "parent_child", 2),
        ("heads_up", "friends", 4),
        ("diagram", "friends", 4),
    ],
)
def test_accepting_an_invite_populates_the_playable_game(
    client, models, monkeypatch, game, mode, capacity
):
    clock = [int(datetime.now(timezone.utc).timestamp() * 1000)]
    monkeypatch.setattr(matches, "clock", lambda: clock[0])
    monkeypatch.setattr(
        economy, "clock", lambda: datetime.fromtimestamp(clock[0] / 1000, timezone.utc)
    )
    beat(client, B)
    assert client.post("/api/me/friend-requests", json={"friend_id": B}).status_code == 200
    assert client.post(f"/api/me/friend-requests/{A}/accept", headers=headers(B)).status_code == 200
    assert seen(client)[0]["status"] == "online"
    client.app.state.memory.set_profile(A, "parent", "Alex")
    client.app.state.memory.set_profile(B, "learner", "Sam")
    lobby = room(client, capacity)
    made = invite(client, lobby, game=game, mode=mode)
    assert made.status_code == 201, made.text
    assert act(client, made.json()["id"], "accept").status_code == 200
    challenge = create(client, game, room_id=lobby["id"])
    response = client.post("/api/matches", json={"challenge_id": challenge["id"], "mode": mode})
    assert response.status_code == 201, response.text
    match = response.json()
    assert {p["player_id"] for p in match["players"]} == {A, B}
    started = client.post(f"/api/matches/{match['id']}/start")
    assert started.status_code == 200, started.text
    clock[0] = started.json()["play_at_ms"] + 1000
    assert client.get(f"/api/matches/{match['id']}", headers=headers(B)).status_code == 200
    if game == "mcq":
        for player in [A, B]:
            assert (
                client.post(
                    f"/api/matches/{match['id']}/answer",
                    headers=headers(player),
                    json={"question_index": 0, "choice": 1},
                ).status_code
                == 200
            )
        clock[0] += 3600 * 1000
        for player in [A, B]:
            paid = client.post(
                "/api/me/coins/claim-participation",
                headers=headers(player),
                json={"source": "match", "id": match["id"]},
            )
            assert paid.status_code == 200 and paid.json()["credited"] == 20
        if mode == "parent_child":
            assert (
                client.post("/api/me/coins/claim-duel", json={"match_id": match["id"]}).json()[
                    "credited"
                ]
                == 10
            )
            quests = client.get("/api/me/quests").json()["items"]
            assert next(q for q in quests if q["id"] == "d-duel")["progress"] == 1
