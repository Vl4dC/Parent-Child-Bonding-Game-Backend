"""Participation claims use the real diagram race lifecycle and membership rules."""

import pytest

from tests import test_diagram_assembly as diagrams

game = diagrams.game


@pytest.mark.parametrize("correct", [False, True])
def test_diagram_race_participation_rewards_submission_not_idle_waiting(game, correct):
    client, _, _, now, tokens, _ = game
    template, race, _ = diagrams.setup(game)
    url = "/api/me/coins/claim-participation"
    body = {"source": "diagram_race", "id": race["id"]}
    assert client.post(url, json=body, headers=tokens["parent"]).status_code == 409
    timing = diagrams.start(game, race)
    board = diagrams.board(game, race, "parent")
    placements = diagrams.solution(game, template, board)
    if not correct:
        a, b = list(placements)[:2]
        placements[a], placements[b] = placements[b], placements[a]
    assert diagrams.save(game, race, "parent", placements).status_code == 200
    assert diagrams.finish(game, race, "parent").status_code == 200
    now[0] = timing["deadline_at"] + 1
    response = client.post(url, json=body, headers=tokens["parent"])
    assert response.status_code == 200, response.text
    assert response.json() == {"coins": 260, "credited": 20}
    assert client.post(url, json=body, headers=tokens["parent"]).json()["credited"] == 0
    assert client.post(url, json=body, headers=tokens["child"]).status_code == 409
    assert (
        client.post(
            url, json=body, headers={"X-Player-ID": "00000000-0000-4000-8000-000000000009"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("action", ["cancel", "forfeit"])
def test_cancelled_or_forfeited_race_does_not_pay(game, action):
    client, _, _, now, tokens, _ = game
    template, race, _ = diagrams.setup(game)
    timing = diagrams.start(game, race)
    placements = diagrams.solution(game, template, diagrams.board(game, race, "parent"))
    a, b = list(placements)[:2]
    placements[a], placements[b] = placements[b], placements[a]
    assert diagrams.save(game, race, "parent", placements).status_code == 200
    submitted = diagrams.finish(game, race, "parent").json()
    version = timing["version"] if action == "cancel" else submitted["version"]
    assert (
        diagrams.post(
            client, f"races/{race['id']}/{action}", {"expected_version": version}, tokens["parent"]
        ).status_code
        == 200
    )
    now[0] = timing["deadline_at"] + 1
    response = client.post(
        "/api/me/coins/claim-participation",
        json={"source": "diagram_race", "id": race["id"]},
        headers=tokens["parent"],
    )
    assert response.status_code == 409
    assert client.get("/api/me/wallet", headers=tokens["parent"]).json()["coins"] == 240
