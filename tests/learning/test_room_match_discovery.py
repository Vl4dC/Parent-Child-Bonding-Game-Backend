from tests.learning.test_challenges import OTHER, THIRD, create, make_room


def test_joined_players_discover_the_match_but_late_members_do_not(client, models):
    room = make_room(client, (OTHER, "Sam"))
    challenge = create(client, room_id=room)
    made = client.post("/api/matches", json={"challenge_id": challenge["id"], "mode": "friends"})
    assert made.status_code == 201, made.text
    mid = made.json()["id"]
    for headers in [{}, OTHER]:
        response = client.get(f"/api/rooms/{room}/match", headers=headers)
        assert response.json() == {"id": mid, "game": "mcq", "phase": "waiting"}
    assert client.get(f"/api/rooms/{room}/match", headers=THIRD).status_code == 404
    code = client.get(f"/api/rooms/{room}").json()["code"]
    client.post("/api/rooms/join", headers=THIRD, json={"code": code, "display_name": "Late"})
    assert client.get(f"/api/rooms/{room}/match", headers=THIRD).status_code == 404


def test_masked_diagram_view_is_sandboxed_and_participant_only(client, models):
    room = make_room(client, (OTHER, "Sam"))
    challenge = create(client, game="diagram", room_id=room)
    made = client.post(
        "/api/matches", json={"challenge_id": challenge["id"], "mode": "friends"}
    ).json()
    view = client.get(f"/api/matches/{made['id']}/diagram/view")
    assert view.status_code == 200, view.text
    assert view.headers["content-security-policy"] == "sandbox allow-scripts"
    assert view.headers["content-type"].startswith("text/html")
    assert "answer_key" not in view.text
    assert client.get(f"/api/matches/{made['id']}/diagram/view", headers=THIRD).status_code == 404
