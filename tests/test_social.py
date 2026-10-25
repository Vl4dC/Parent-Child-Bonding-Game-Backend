"""Presence leases and invitation lifecycle through the real API, memory and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from learning_service import presence
from learning_service.home_store import HomeStore

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
C = "00000000-0000-4000-8000-000000000003"
D = "00000000-0000-4000-8000-000000000004"


def headers(player=A, key=None):
    return {"X-Player-ID": player} | ({"Idempotency-Key": key} if key else {})


@pytest.fixture
def social(repository, monkeypatch):
    clock = [presence.clock()]
    monkeypatch.setattr(presence, "clock", lambda: clock[0])
    settings = Settings(app_env="test", _env_file=None)
    with TestClient(create_app(settings, repository=repository)) as client:
        yield client, repository, clock


def befriend(client, friend=B, actor=A):
    code = client.get("/api/me/friends", headers=headers(actor)).json()["code"]
    assert (
        client.post(
            "/api/me/friends/requests", headers=headers(friend), json={"code": code}
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/me/friends/requests/{friend}/accept", headers=headers(actor)).status_code
        == 200
    )


def room(client, capacity=2):
    r = client.post(
        "/api/rooms", headers=headers(), json={"display_name": "Alex", "capacity": capacity}
    )
    assert r.status_code == 201, r.text
    return r.json()


def invite(client, lobby, friend=B, key="invitation-key-one", **extra):
    return client.post(
        "/api/me/game-invites",
        headers=headers(key=key),
        json={"friend_id": friend, "room_id": lobby["id"], **extra},
    )


def act(client, id, action, actor=B):
    return client.post(f"/api/me/game-invites/{id}/{action}", headers=headers(actor))


def listed(client, actor=B, **params):
    response = client.get("/api/me/game-invites", headers=headers(actor), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def beat(client, actor=B, session=None, status="online"):
    session = session or str(uuid4())
    result = client.put(
        f"/api/me/presence/sessions/{session}", headers=headers(actor), json={"status": status}
    )
    assert result.status_code == 200, result.text
    return result.json()


def seen(client, actor=A):
    response = client.get("/api/me/friends/presence", headers=headers(actor))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["items"]


def test_presence_expires_and_is_only_visible_to_accepted_friends(social):
    client, _, clock = social
    beat(client)
    assert seen(client) == []
    befriend(client)
    assert seen(client)[0]["status"] == "online"
    assert seen(client, C) == []
    clock[0] += 89
    assert seen(client)[0]["status"] == "online"
    clock[0] += 1
    assert seen(client)[0]["status"] == "offline"
    assert seen(client)[0]["last_seen"] is not None
    beat(client, status="away")
    assert seen(client)[0]["status"] == "away"
    assert client.delete(f"/api/me/friends/{B}", headers=headers()).status_code == 204
    assert seen(client) == []


def test_sessions_do_not_disconnect_other_tabs_and_settings_hide_last_seen(social):
    client, _, _ = social
    befriend(client)
    online = beat(client)
    busy = beat(client, status="busy")
    assert seen(client)[0]["status"] == "busy"
    for _ in range(2):
        assert (
            client.delete(
                f"/api/me/presence/sessions/{busy['session_id']}", headers=headers(B)
            ).status_code
            == 204
        )
    assert seen(client)[0]["status"] == "online"
    assert (
        client.put(
            "/api/me/social-settings",
            headers=headers(B),
            json={"share_presence": False, "allow_game_invites": True},
        ).status_code
        == 200
    )
    friend = seen(client)[0]
    assert friend["status"] == "offline" and friend["last_seen"] is None
    client.put("/api/me/social-settings", headers=headers(B), json={"share_presence": True})
    assert seen(client)[0]["status"] == "online"
    client.delete(f"/api/me/presence/sessions/{online['session_id']}", headers=headers(B))
    assert seen(client)[0]["status"] == "offline"


def test_session_cap_renewal_and_expiry_reclaim(social):
    client, _, clock = social
    sessions = [beat(client)["session_id"] for _ in range(8)]
    beat(client, session=sessions[0])
    response = client.put(f"/api/me/presence/sessions/{uuid4()}", headers=headers(B), json={})
    assert response.status_code == 429
    clock[0] += 90
    beat(client)


def test_invitation_accept_joins_lobby_once_and_hides_room_code_until_accept(social):
    client, _, _ = social
    befriend(client)
    lobby = room(client)
    made = invite(client, lobby)
    assert made.status_code == 201, made.text
    value = made.json()
    assert value["status"] == "pending" and "code" not in value
    assert value == invite(client, lobby).json()
    assert invite(client, lobby, game="heads_up").status_code == 409  # same key, different request
    assert invite(client, lobby, key="another-invite-key").status_code == 409
    assert len(listed(client)["items"]) == 1
    assert listed(client, C)["items"] == []
    assert client.get(f"/api/me/game-invites/{value['id']}", headers=headers(C)).status_code == 404
    assert act(client, value["id"], "accept", A).status_code == 403
    assert act(client, value["id"], "cancel", B).status_code == 403
    accepted = act(client, value["id"], "accept")
    assert accepted.status_code == 200, accepted.text
    joined = accepted.json()["room"]
    assert {m["user_id"] for m in joined["members"]} == {A, B}
    assert joined["code"] == lobby["code"]
    again = act(client, value["id"], "accept").json()
    assert again["room"]["version"] == joined["version"]
    assert act(client, value["id"], "decline").status_code == 409
    assert act(client, value["id"], "cancel", A).status_code == 409
    assert (
        client.post(
            f"/api/rooms/{lobby['id']}/commands",
            headers=headers(B),
            json={"expected_version": joined["version"], "action": "leave"},
        ).status_code
        == 200
    )
    assert act(client, value["id"], "accept").json()["room"] is None  # replay never rejoins


@pytest.mark.parametrize(
    "action,actor,state", [("decline", B, "declined"), ("cancel", A, "cancelled")]
)
def test_decline_cancel_and_replays(social, action, actor, state):
    client, _, _ = social
    befriend(client)
    value = invite(client, room(client)).json()
    for _ in range(2):
        r = act(client, value["id"], action, actor)
        assert r.status_code == 200 and r.json()["invite"]["status"] == state
    assert act(client, value["id"], "accept").status_code == 409


def test_expiry_and_new_invite_after_expiry(social):
    client, _, clock = social
    befriend(client)
    lobby = room(client)
    value = invite(client, lobby).json()
    clock[0] += 600
    assert listed(client, status="pending")["items"] == []
    assert listed(client, status="expired")["items"][0]["id"] == value["id"]
    assert act(client, value["id"], "accept").status_code == 409
    assert invite(client, lobby).json()["status"] == "expired"
    assert invite(client, lobby, key="new-after-expiry").status_code == 201


def test_friendship_and_preference_and_room_rules(social):
    client, _, _ = social
    lobby = room(client)
    assert invite(client, lobby).status_code == 404
    assert invite(client, lobby, friend=A).status_code == 422
    befriend(client)
    client.put("/api/me/social-settings", headers=headers(B), json={"allow_game_invites": False})
    assert invite(client, lobby).status_code == 409
    client.put("/api/me/social-settings", headers=headers(B), json={"allow_game_invites": True})
    value = invite(client, lobby).json()
    client.delete(f"/api/me/friends/{B}", headers=headers())
    assert act(client, value["id"], "accept").status_code == 404
    befriend(client)
    assert (
        client.post(
            f"/api/rooms/{lobby['id']}/commands",
            json={"expected_version": lobby["version"], "action": "close"},
        ).status_code
        == 200
    )
    assert act(client, value["id"], "accept").status_code == 409
    assert listed(client)["items"][0]["status"] == "pending"  # failed acceptance changed nothing


def test_invites_to_offline_friends_and_supported_modes(social):
    client, _, _ = social
    befriend(client)
    assert seen(client)[0]["status"] == "offline"
    assert invite(client, room(client), mode="parent_child").status_code == 201
    group = room(client, 4)
    assert invite(client, group, key="bad-parent-room", mode="parent_child").status_code == 422
    assert (
        invite(client, group, key="bad-duo-diagram", mode="duo", game="diagram").status_code == 422
    )
    assert (
        invite(client, group, key="good-duo-room", mode="duo", game="heads_up").status_code == 201
    )
    assert invite(client, room(client), key="bad-solo-mode", mode="solo").status_code == 422


def test_parallel_accepts_for_last_slot_do_not_overbook(social):
    client, _, _ = social
    befriend(client, B)
    befriend(client, C)
    lobby = room(client)
    one = invite(client, lobby, B, key="invite-person-b").json()
    two = invite(client, lobby, C, key="invite-person-c").json()
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda pair: act(client, pair[0]["id"], "accept", pair[1]), [(one, B), (two, C)]
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(client.get(f"/api/rooms/{lobby['id']}").json()["members"]) == 2
    assert sorted(i["status"] for i in listed(client, A)["items"]) == ["accepted", "pending"]


def test_parallel_accept_cancel_has_one_terminal_outcome(social):
    client, _, _ = social
    befriend(client)
    lobby = room(client)
    made = invite(client, lobby).json()
    with ThreadPoolExecutor(2) as pool:
        jobs = [
            pool.submit(act, client, made["id"], "accept", B),
            pool.submit(act, client, made["id"], "cancel", A),
        ]
        results = [j.result() for j in jobs]
    assert sorted(r.status_code for r in results) == [200, 409]
    status = listed(client)["items"][0]["status"]
    count = len(client.get(f"/api/rooms/{lobby['id']}").json()["members"])
    assert (status, count) in [("accepted", 2), ("cancelled", 1)]


def test_pagination_limits_and_player_deletion(social):
    client, _, _ = social
    befriend(client)
    beat(client, A)
    for n in range(3):
        invite(client, room(client), key=f"page-request-{n}")
    first = listed(client, limit=2)
    second = listed(client, limit=2, after=first["next_cursor"])
    assert len(first["items"]) == 2 and len(second["items"]) == 1 and second["next_cursor"] is None
    assert {x["id"] for x in first["items"]}.isdisjoint({x["id"] for x in second["items"]})
    client.app.state.settings.game_invites_per_hour = 3
    assert invite(client, room(client), key="over-hourly-limit").status_code == 429
    assert client.delete("/api/me/data", headers=headers()).status_code == 204
    assert listed(client)["items"] == [] and seen(client, B) == []


def test_presence_settings_and_leases_survive_store_restart(tmp_path, monkeypatch):
    now = [presence.clock()]
    monkeypatch.setattr(presence, "clock", lambda: now[0])
    path = str(tmp_path / "presence.db")
    home = HomeStore(path)
    presence.put_settings(home, A, {"share_presence": True, "allow_game_invites": False})
    presence.heartbeat(home, A, str(uuid4()), "online", 90)
    home.close()
    home = HomeStore(path)
    assert presence.settings(home, A)["allow_game_invites"] is False
    assert presence.read(home, A, 90)["status"] == "online"
    now[0] += 90
    assert presence.read(home, A, 90)["status"] == "offline"
    home.forget_player(A)
    assert presence.read(home, A, 90)["last_seen"] is None
    home.close()


def test_invitations_cannot_survive_host_transfer_as_join_permissions(social):
    client, _, _ = social
    befriend(client)
    lobby = room(client, 4)
    made = invite(client, lobby).json()
    joined = client.post(
        "/api/rooms/join", headers=headers(C), json={"code": lobby["code"], "display_name": "Kim"}
    ).json()
    assert (
        client.post(
            f"/api/rooms/{lobby['id']}/commands",
            headers=headers(A),
            json={"expected_version": joined["version"], "action": "leave"},
        ).status_code
        == 200
    )
    assert act(client, made["id"], "accept").status_code == 409


def test_pending_requests_are_not_friends_and_bad_payloads_are_rejected(social):
    client, _, _ = social
    code = client.get("/api/me/friends").json()["code"]
    client.post("/api/me/friends/requests", headers=headers(B), json={"code": code})
    beat(client)
    assert seen(client) == []
    lobby = room(client)
    assert invite(client, lobby).status_code == 404
    assert (
        client.put(
            f"/api/me/presence/sessions/{uuid4()}",
            json={"status": "online", "last_seen": 9999999999},
        ).status_code
        == 422
    )
    assert client.put("/api/me/social-settings", json={"other_player": B}).status_code == 422
    assert (
        client.post(
            "/api/me/game-invites",
            headers=headers(key="malformed-key"),
            json={"friend_id": B, "room_id": lobby["id"], "sender_id": C},
        ).status_code
        == 422
    )


def test_acceptance_room_join_rolls_back_if_saving_invite_fails(social, monkeypatch):
    from fastapi import HTTPException

    from app.game_invites import Store

    client, _, _ = social
    befriend(client)
    lobby = room(client)
    made = invite(client, lobby).json()
    original = Store.update

    def fail(self, invitation, status):
        if status == "accepted":
            raise HTTPException(503, "Synthetic storage failure")
        return original(self, invitation, status)

    monkeypatch.setattr(Store, "update", fail)
    assert act(client, made["id"], "accept").status_code == 503
    assert client.get(f"/api/rooms/{lobby['id']}").json()["members"] == lobby["members"]
    assert listed(client)["items"][0]["status"] == "pending"
    monkeypatch.setattr(Store, "update", original)
    assert act(client, made["id"], "accept").status_code == 200


def test_postgres_invitation_visibility_and_missing_migration_readiness(social):
    import psycopg

    from tests.test_database import as_user

    client, repo, _ = social
    if not repo.durable:
        pytest.skip("PostgreSQL policies and schema readiness")
    befriend(client)
    made = invite(client, room(client)).json()
    with psycopg.connect(repo.url) as connection:
        as_user(connection, C)
        assert connection.execute("select * from api_private.game_invites").fetchall() == []
        assert (
            connection.execute("select api_private.game_invite_room(%s)", (made["id"],)).fetchone()[
                0
            ]
            is None
        )
    assert repo.ready()
    with psycopg.connect(repo.url, autocommit=True) as connection:
        connection.execute(
            "alter function api_private.game_invite_room(uuid) rename to hidden_game_invite_room"
        )
        try:
            assert not repo.ready()
        finally:
            connection.execute(
                "alter function api_private.hidden_game_invite_room(uuid) rename to game_invite_room"
            )
    assert repo.ready()


def test_friend_request_by_id_unlocks_presence_and_invites_only_after_acceptance(social):
    client, _, _ = social
    beat(client, B)
    lobby = room(client)
    sent = client.post("/api/me/friend-requests", json={"friend_id": B})
    assert sent.status_code == 200 and sent.json()["status"] == "outgoing"
    assert client.post("/api/me/friend-requests", json={"friend_id": B}).json() == sent.json()
    incoming = client.get("/api/me/friend-requests", headers=headers(B)).json()
    outgoing = client.get("/api/me/friend-requests").json()
    assert len(incoming["incoming"]) == len(outgoing["outgoing"]) == 1
    assert incoming["incoming"][0]["player_id"] == A
    assert incoming["incoming"][0]["requested_at"]
    assert client.get("/api/me/friend-requests", headers=headers(C)).json()["incoming"] == []
    assert seen(client) == [] and invite(client, lobby).status_code == 404
    assert client.post(f"/api/me/friend-requests/{B}/accept").status_code == 403
    for _ in range(2):
        accepted = client.post(f"/api/me/friend-requests/{A}/accept", headers=headers(B))
        assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    assert client.get("/api/me/friend-requests").json()["outgoing"] == []
    assert seen(client)[0]["status"] == "online"
    assert invite(client, lobby).status_code == 201
    assert client.delete(f"/api/me/friend-requests/{B}").status_code == 409
    assert (
        client.post(f"/api/me/friend-requests/{A}/decline", headers=headers(B)).status_code == 409
    )
    assert len(seen(client)) == 1


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_friend_code_requests_decline_cancel_permissions_and_replays(social, action):
    client, _, _ = social
    code = client.get("/api/me/friends", headers=headers(B)).json()["code"]
    sent = client.post("/api/me/friend-requests", json={"code": code.lower()})
    assert sent.status_code == 200 and sent.json()["status"] == "outgoing"
    assert client.delete(f"/api/me/friend-requests/{A}", headers=headers(B)).status_code == 403
    assert client.post(f"/api/me/friend-requests/{B}/decline").status_code == 403
    for _ in range(2):
        response = (
            client.post(f"/api/me/friend-requests/{A}/decline", headers=headers(B))
            if action == "decline"
            else client.delete(f"/api/me/friend-requests/{B}")
        )
        assert response.status_code == 204
    assert client.get("/api/me/friend-requests", headers=headers(B)).json()["incoming"] == []
    assert client.post(f"/api/me/friend-requests/{A}/accept", headers=headers(B)).status_code == 404


def test_invalid_friend_request_targets_and_mutual_requests(social):
    client, _, _ = social
    for player in [A, B]:
        client.get("/api/me/friends", headers=headers(player))
    for body in [
        {},
        {"friend_id": A},
        {"friend_id": B, "code": "ABCD2345"},
        {"friend_id": "user_not_a_uuid"},
        {"code": "bad"},
    ]:
        assert client.post("/api/me/friend-requests", json=body).status_code == 422
    assert client.post("/api/me/friend-requests", json={"friend_id": C}).status_code == 404
    with ThreadPoolExecutor(2) as pool:
        jobs = [
            pool.submit(
                client.post,
                "/api/me/friend-requests",
                headers=headers(actor),
                json={"friend_id": other},
            )
            for actor, other in [(A, B), (B, A)]
        ]
        responses = [j.result() for j in jobs]
    assert sorted(r.json()["status"] for r in responses) == ["accepted", "outgoing"]
    for player in [A, B]:
        assert (
            client.get("/api/me/friend-requests", headers=headers(player)).json()["incoming"] == []
        )
    assert len(seen(client)) == 1


def test_invitation_expiry_is_rechecked_after_waiting_for_lobby_lock(social, monkeypatch):
    from app.game_invites import Store

    client, _, clock = social
    befriend(client)
    lobby = room(client)
    made = invite(client, lobby).json()
    original = Store.room

    def after_lock_wait(self, invitation):
        result = original(self, invitation)
        clock[0] += 600
        return result

    monkeypatch.setattr(Store, "room", after_lock_wait)
    response = act(client, made["id"], "accept")
    assert response.status_code == 409 and "expired" in response.json()["detail"]
    assert client.get(f"/api/rooms/{lobby['id']}").json()["members"] == lobby["members"]
    assert listed(client)["items"][0]["status"] == "expired"
