"""The home API: routes, status codes, headers and how it fits with the rest of the app."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from learning_service.home_store import HomeStore
from tests.test_quiz_practice import answer, practice  # noqa: F401

ME = "00000000-0000-4000-8000-000000000001"  # the default player
SAM = "00000000-0000-4000-8000-000000000002"
SAM_H = {"X-Player-ID": SAM}


@pytest.fixture
def client():
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as c:
        yield c


@pytest.fixture
def demo_client():
    with TestClient(create_app(Settings(app_env="test", demo_mode=True, _env_file=None))) as c:
        yield c


def set_coins(client, pid, coins):
    store = client.app.state.home
    store.get_home(pid)
    with store._tx() as db:
        db.execute("update wallets set coins = ? where player_id = ?", (coins, pid))


def test_the_app_has_a_home_store_that_is_in_memory_under_test(client):
    assert isinstance(client.app.state.home, HomeStore)
    assert client.app.state.home.path == ":memory:"


def test_an_injected_home_store_is_used_and_not_closed(tmp_path):
    store = HomeStore(str(tmp_path / "h.db"))
    with TestClient(create_app(Settings(app_env="test", _env_file=None), home_store=store)) as c:
        assert c.app.state.home is store
    store.get_home(ME)  # still open after the app shut down
    store.close()


def test_the_home_database_path_comes_from_settings(tmp_path):
    path = str(tmp_path / "x.db")
    app = create_app(Settings(app_env="test", home_db_path=path, _env_file=None))
    with TestClient(app):
        assert app.state.home.path == path


def test_reading_my_home(client):
    response = client.get("/api/me/home")
    assert response.status_code == 200
    body = response.json()
    assert body["coins"] == 240 and body["version"] == 1 and body["visibility"] == "friends"
    assert [r["id"] for r in body["rooms"]] == ["living"]
    assert body["rooms"][0]["items"] == [
        {"id": "tv", "level": 1, "at": None, "rot": 0},
        {"id": "sofa", "level": 1, "at": None, "rot": 0},
    ]
    assert body["owner"] == {"id": ME, "display_name": "Player", "avatar": None}
    assert response.headers["cache-control"] == "no-store" and "x-request-id" in response.headers


def test_two_players_have_two_homes(client):
    client.post("/api/me/home/items/living/table/place", json={})
    assert client.get("/api/me/home").json()["coins"] == 210
    assert client.get("/api/me/home", headers=SAM_H).json()["coins"] == 240


def test_place_upgrade_and_layout_round_trip(client):
    placed = client.post(
        "/api/me/home/items/living/table/place", json={"at": [1.2, 1.4], "rot": 1}
    ).json()
    assert placed["coins"] == 210 and placed["version"] == 2
    upgraded = client.post("/api/me/home/items/living/table/upgrade", json={}).json()
    assert upgraded["coins"] == 170
    table = next(i for i in upgraded["rooms"][0]["items"] if i["id"] == "table")
    assert (table["level"], table["at"], table["rot"]) == (2, [1.2, 1.4], 1)
    moved = client.put(
        "/api/me/home/rooms/living/layout",
        json={"items": {"table": {"at": [2.0, 2.0], "rot": 3}}, "expected_version": 3},
    )
    assert moved.status_code == 200 and moved.json()["coins"] == 170
    table = next(i for i in moved.json()["rooms"][0]["items"] if i["id"] == "table")
    assert (table["at"], table["rot"]) == ([2.0, 2.0], 3)


def test_not_enough_coins_is_a_402_with_the_message_the_app_shows(client):
    set_coins(client, ME, 10)
    response = client.post("/api/me/home/items/living/table/place", json={})
    assert response.status_code == 402 and response.json() == {"detail": "You need 30 coins."}


def test_a_stale_version_is_a_409(client):
    response = client.post("/api/me/home/rooms", json={"expected_version": 7})
    assert response.status_code == 409


def test_a_broken_rule_is_a_422_with_the_servers_message(client):
    response = client.post("/api/me/home/rooms", json={})
    assert response.status_code == 422 and "level 3" in response.json()["detail"]


def test_unknown_rooms_and_items_are_404(client):
    assert client.post("/api/me/home/items/attic/tv/place", json={}).status_code == 404
    assert client.post("/api/me/home/items/living/piano/upgrade", json={}).status_code == 404
    assert client.put("/api/me/home/rooms/attic/layout", json={"items": {}}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"at": [3.0, 1.0]},
        {"at": [-0.5, 1.0]},
        {"at": [1.0]},
        {"at": [1.0, 1.0], "rot": 4},
        {"rot": "left"},
        {"price": 1},
    ],
)
def test_bad_placements_are_rejected_before_anything_is_charged(client, body):
    assert client.post("/api/me/home/items/living/table/place", json=body).status_code == 422
    assert client.get("/api/me/home").json()["coins"] == 240


def test_visibility_and_the_card(client):
    assert client.put("/api/me/home/visibility", json={"visibility": "open"}).status_code == 422
    home = client.put("/api/me/home/visibility", json={"visibility": "nobody"}).json()
    assert home["visibility"] == "nobody" and home["version"] == 2
    card = client.put(
        "/api/me/home/card", json={"display_name": "  Nova ", "avatar": "nova"}
    ).json()
    assert (card["owner"]["display_name"], card["owner"]["avatar"], card["version"]) == (
        "Nova",
        "nova",
        2,
    )
    assert client.put("/api/me/home/card", json={"display_name": "   "}).status_code == 422


def test_deleting_my_data_deletes_the_home_too(client):
    client.post("/api/me/home/items/living/table/place", json={})
    assert client.delete("/api/me/data").status_code == 204
    assert client.get("/api/me/home").json()["coins"] == 240


def befriend(client):
    """ME and SAM become friends through the real request/accept flow."""
    code = client.get("/api/me/friends").json()["code"]
    sent = client.post("/api/me/friends/requests", json={"code": code}, headers=SAM_H)
    assert sent.status_code == 200
    accepted = client.post(f"/api/me/friends/requests/{SAM}/accept")
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"


def test_the_friend_flow(client):
    listing = client.get("/api/me/friends").json()
    assert len(listing["code"]) == 8 and listing["items"] == []
    client.put("/api/me/home/card", json={"display_name": "Sam", "avatar": "sam"}, headers=SAM_H)
    sent = client.post(
        "/api/me/friends/requests", json={"code": listing["code"].lower()}, headers=SAM_H
    )
    assert sent.json()["status"] == "outgoing" and sent.json()["player_id"] == ME
    incoming = client.get("/api/me/friends").json()["items"]
    assert [(f["player_id"], f["status"], f["display_name"]) for f in incoming] == [
        (SAM, "incoming", "Sam")
    ]
    assert client.post(f"/api/me/friends/requests/{SAM}/accept").json()["avatar"] == "sam"
    assert client.delete(f"/api/me/friends/{SAM}").status_code == 204
    assert client.get("/api/me/friends").json()["items"] == []
    assert client.delete(f"/api/me/friends/{SAM}").status_code == 404


def test_friend_requests_that_cannot_work(client):
    own = client.get("/api/me/friends").json()["code"]
    assert client.post("/api/me/friends/requests", json={"code": own}).status_code == 422
    assert client.post("/api/me/friends/requests", json={"code": "NOSUCH00"}).status_code == 404
    assert client.post(f"/api/me/friends/requests/{SAM}/accept").status_code == 404
    assert client.post("/api/me/friends/requests/not-a-uuid/accept").status_code == 422
    assert client.post("/api/me/friends/requests", json={"code": "x"}).status_code == 422


def test_declining_a_request(client):
    code = client.get("/api/me/friends").json()["code"]
    client.post("/api/me/friends/requests", json={"code": code}, headers=SAM_H)
    assert client.post(f"/api/me/friends/requests/{SAM}/decline").status_code == 204
    assert client.get("/api/me/friends").json()["items"] == []


def test_visiting_a_friend_shows_the_home_without_coins(client):
    client.post("/api/me/home/items/living/table/place", json={"at": [1.0, 1.0]}, headers=SAM_H)
    befriend(client)
    home = client.get(f"/api/homes/{SAM}")
    assert home.status_code == 200
    body = home.json()
    assert "coins" not in body and "visibility" not in body and "cheers" not in body
    assert body["owner"]["id"] == SAM and body["summary"]["levels"] == 3


def test_a_stranger_gets_the_same_404_as_a_missing_home(client):
    client.get("/api/me/home", headers=SAM_H)
    missing = "00000000-0000-4000-8000-0000000000ee"
    stranger = client.get(f"/api/homes/{SAM}")
    absent = client.get(f"/api/homes/{missing}")
    assert stranger.status_code == absent.status_code == 404
    assert stranger.json() == absent.json() == {"detail": "We couldn't find that home."}
    assert client.get("/api/homes/not-a-uuid").status_code == 422


@pytest.mark.parametrize("visibility", ["family", "nobody"])
def test_a_private_home_cannot_be_visited_by_a_friend(client, visibility):
    befriend(client)
    client.put("/api/me/home/visibility", json={"visibility": visibility}, headers=SAM_H)
    assert client.get(f"/api/homes/{SAM}").status_code == 404
    assert client.get(f"/api/homes/{ME}", headers=SAM_H).status_code == 200  # your own, always


def test_the_home_list(client):
    befriend(client)
    client.put("/api/me/home/card", json={"display_name": "Sam", "avatar": "sam"}, headers=SAM_H)
    cards = client.get("/api/homes").json()["items"]
    assert [
        (c["owner"]["id"], c["owner"]["display_name"], c["rooms"], c["levels"]) for c in cards
    ] == [(SAM, "Sam", 1, 2)]
    assert client.get("/api/homes?scope=family").json()["items"] == []
    assert client.get("/api/homes?scope=everyone").status_code == 422


def test_cheers(client):
    befriend(client)
    sent = client.post(f"/api/homes/{SAM}/cheers", json={"room_id": "living"})
    assert sent.status_code == 201 and sent.json()["room_id"] == "living"
    assert client.post(f"/api/homes/{SAM}/cheers", json={"room_id": "living"}).status_code == 409
    assert client.post(f"/api/homes/{SAM}/cheers", json={}).status_code == 201  # the whole home
    assert client.post(f"/api/homes/{SAM}/cheers", json={"room_id": "kitchen"}).status_code == 422
    assert client.post(f"/api/homes/{ME}/cheers", json={}).status_code == 422
    assert client.get("/api/me/home", headers=SAM_H).json()["cheers"] == {"home": 1, "living": 1}


def test_a_stranger_cannot_cheer(client):
    client.get("/api/me/home", headers=SAM_H)
    assert client.post(f"/api/homes/{SAM}/cheers", json={}).status_code == 404


@pytest.fixture
def quiz_client(practice):  # noqa: F811
    client, _, quiz = practice
    return client, quiz.id


def test_a_quiz_pays_10_coins_per_first_correct_answer_once(quiz_client):
    client, qid = quiz_client
    claim = {"quiz_id": qid}
    assert client.post("/api/me/coins/claim-quiz", json=claim).json() == {
        "coins": 240,
        "credited": 0,
    }
    assert answer(client, qid, 0, 1, 0).status_code == 200  # correct
    assert client.post("/api/me/coins/claim-quiz", json=claim).json() == {
        "coins": 250,
        "credited": 10,
    }
    assert client.post("/api/me/coins/claim-quiz", json=claim).json()["credited"] == 0
    assert answer(client, qid, 1, 1, 1).status_code == 200  # wrong: the true/false answer is True
    assert answer(client, qid, 2, 2, 2).status_code == 200  # correct
    assert client.post("/api/me/coins/claim-quiz", json=claim).json() == {
        "coins": 260,
        "credited": 10,
    }
    assert client.get("/api/me/home").json()["coins"] == 260


def test_someone_elses_quiz_or_a_missing_one_pays_nothing(quiz_client):
    client, qid = quiz_client
    assert (
        client.post("/api/me/coins/claim-quiz", json={"quiz_id": qid}, headers=SAM_H).status_code
        == 404
    )
    assert client.post("/api/me/coins/claim-quiz", json={"quiz_id": "nope"}).status_code == 404
    assert client.get("/api/me/home", headers=SAM_H).json()["coins"] == 240


def test_the_demo_top_up_exists_only_in_demo_mode(client, demo_client):
    assert client.post("/api/me/coins/demo-topup", json={"amount": 500}).status_code == 404
    assert client.get("/api/me/home").json()["coins"] == 240
    response = demo_client.post("/api/me/coins/demo-topup", json={"amount": 500})
    assert response.status_code == 200 and response.json() == {"coins": 740, "credited": 500}
    assert (
        demo_client.post("/api/me/coins/demo-topup", json={"amount": 500}).json()["coins"] == 1240
    )
    assert demo_client.post("/api/me/coins/demo-topup", json={"amount": 0}).status_code == 422
