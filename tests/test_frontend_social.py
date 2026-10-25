"""Exact frontend wire contract, real RSA JWT verification and fake model outputs."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository import current_session
from learning_service import live_social, store
from learning_service.lesson import Lesson, LessonRequest, LessonSection
from learning_service.store import StoredLesson, StoredQuiz
from learning_service.visual_recall import IllustratedQuestion

ISSUER = "https://clerk.example.test"
ORIGIN = "http://localhost:3000"
PLAYERS = {
    name: f"00000000-0000-4000-8000-00000000000{index}"
    for index, name in enumerate(["alice", "bob", "carol"], 1)
}


@pytest.fixture(scope="module")
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {
        "kid": "key-one",
        "alg": "RS256",
        "use": "sig",
    }
    return key, jwk


@pytest.fixture
def api(repository, monkeypatch, signing):
    private, jwk = signing
    now = [int(time.time() * 1000)]
    monkeypatch.setattr(live_social, "clock", lambda: now[0])
    from app import economy

    monkeypatch.setattr(
        economy, "clock", lambda: datetime.fromtimestamp(now[0] / 1000, live_social.timezone.utc)
    )
    settings = Settings(
        app_env="test", clerk_issuer=ISSUER, generation_enabled=True, _env_file=None
    )
    application = create_app(settings, repository=repository)
    jwks_calls = []

    def fetch_jwks():
        jwks_calls.append(True)
        data = {"keys": [jwk]}
        application.state.clerk_social.jwks.jwk_set_cache.put(data)
        return data

    monkeypatch.setattr(application.state.clerk_social.jwks, "fetch_data", fetch_jwks)
    for player in PLAYERS.values():
        lesson = StoredLesson(
            id="lesson",
            request=LessonRequest(topic="Plants", age=12),
            lesson=Lesson(
                title="Plants",
                summary="Plant cells",
                sections=[LessonSection(heading="Cells", body="Plants have cells.")],
                key_points=["Cells"],
            ),
            status="draft",
        )
        with repository.session(player) as unit:
            unit.put("lessons", lesson.id, lesson)
    generated = []

    def generate(lesson, n):
        generated.append((current_session.get().owner, n))
        return StoredQuiz(
            id="synthetic-private-quiz",
            lesson_id=lesson.id,
            title="Plant quiz",
            dropped=0,
            questions=[
                IllustratedQuestion(
                    index=i,
                    type="multiple_choice",
                    question=f"Question {i}",
                    options=["wrong", "right", "also wrong", "still wrong"],
                    answer="right",
                    explanation="Private explanation",
                )
                for i in range(n)
            ],
        )

    monkeypatch.setattr(store, "quiz_from_lesson", generate)

    def token(name="alice", **changes):
        stamp = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "user_" + name,
            "sid": "sess_" + name,
            "player_id": PLAYERS[name],
            "username": name,
            "iat": stamp - 1,
            "nbf": stamp - 1,
            "exp": stamp + 600,
            "azp": ORIGIN,
        }
        claims.update(changes)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "key-one"})

    with TestClient(application) as client:

        def call(method, path, who="alice", **kwargs):
            headers = {"Authorization": "Bearer " + token(who), **kwargs.pop("headers", {})}
            return client.request(method, "/api/social" + path, headers=headers, **kwargs)

        def profile(name="alice", **kwargs):
            return call(
                "PUT",
                "/me",
                name,
                json={"display_name": name.title(), "avatar": "nova", "role": "learner"},
                **kwargs,
            )

        result = SimpleNamespace(
            client=client,
            app=application,
            repo=repository,
            now=now,
            token=token,
            call=call,
            profile=profile,
            generated=generated,
            generate=generate,
            jwks_calls=jwks_calls,
            private=private,
        )
        yield result


def connect(api):
    for name in PLAYERS:
        assert api.profile(name).status_code == 200
    request = api.call("POST", "/friend-requests", json={"username": "bob"})
    assert request.status_code == 201, request.text
    rid = request.json()["request"]["id"]
    accepted = api.call("POST", f"/friend-requests/{rid}/accept", "bob")
    assert accepted.status_code == 200, accepted.text
    assert api.call("POST", "/presence", "bob", json={"state": "online"}).status_code == 200


def duel(api):
    response = api.call(
        "POST",
        "/duels",
        json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
    )
    assert response.status_code == 201, response.text
    return response.json()


def millis(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def wallet(api, name):
    return api.client.get("/api/me/wallet", headers={"X-Player-ID": PLAYERS[name]}).json()["coins"]


def test_profile_jwt_mapping_and_legacy_routes_stay_separate(api):
    assert api.call("GET", "/friends").status_code == 409
    me = api.profile().json()
    assert me == {
        "id": "user_alice",
        "username": "alice",
        "display_name": "Alice",
        "avatar": "nova",
        "role": "learner",
    }
    assert api.call("GET", "/friends", headers={"X-Player-ID": PLAYERS["bob"]}).status_code == 403
    assert api.call("GET", "/friends", headers={"X-Player-ID": PLAYERS["alice"]}).status_code == 200
    forged = api.token("alice", sub="user_intruder")
    assert api.profile(headers={"Authorization": "Bearer " + forged}).status_code == 403
    remapped = api.token("alice", player_id=PLAYERS["bob"])
    assert api.profile(headers={"Authorization": "Bearer " + remapped}).status_code == 403
    assert api.client.get("/api/me/profile").status_code == 200
    assert (
        api.client.get("/api/me/profile", headers={"Authorization": "Bearer invalid"}).status_code
        == 200
    )
    assert len(api.jwks_calls) == 1  # public keys were cached; signatures were still checked


@pytest.mark.parametrize(
    "change",
    [
        {"exp": 1},
        {"nbf": 9999999999},
        {"iss": "https://wrong.example"},
        {"azp": "https://wrong.example"},
        {"player_id": None},
        {"player_id": "not-a-uuid"},
        {"sid": None},
        {"sts": "pending"},
    ],
)
def test_invalid_claims_fail_closed_without_leaking_token(api, change):
    token = api.token(**change)
    r = api.client.get("/api/social/friends", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 401 and token not in r.text
    assert r.headers["www-authenticate"] == "Bearer"


def test_missing_tampered_and_wrong_algorithm_tokens(api):
    assert api.client.get("/api/social/friends").status_code == 401
    token = api.token()
    payload = jwt.decode(token, options={"verify_signature": False})
    payload["sub"] = "user_bob"
    bad = jwt.encode(
        payload,
        "synthetic-untrusted-hmac-key-for-tests-only",
        algorithm="HS256",
        headers={"kid": "key-one"},
    )
    assert (
        api.client.get(
            "/api/social/friends", headers={"Authorization": "Bearer " + bad}
        ).status_code
        == 401
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "key-one"})
    assert (
        api.client.get(
            "/api/social/friends", headers={"Authorization": "Bearer " + bad}
        ).status_code
        == 401
    )
    assert (
        api.client.get(
            "/api/social/friends", headers={"Authorization": "Basic anything"}
        ).status_code
        == 401
    )


def test_username_is_signed_search_is_private_and_prefix_is_literal(api):
    for name in PLAYERS:
        assert api.profile(name).status_code == 200
    assert (
        api.call("PUT", "/me", json={"display_name": "Alice", "username": "forged"}).status_code
        == 422
    )
    missing = api.token(username=None)
    r = api.profile(headers={"Authorization": "Bearer " + missing})
    assert r.status_code == 422 and r.json()["code"] == "no_username"
    assert api.call("GET", "/users/search?q=b").status_code == 422
    result = api.call("GET", "/users/search?q=BO").json()["users"]
    assert len(result) == 1 and result[0]["id"] == "user_bob"
    assert (
        result[0]["online"] is None
        and result[0]["busy"] is None
        and result[0]["relationship"] == "none"
    )
    assert api.call("GET", "/users/search?q=b_").json()["users"] == []
    assert api.call("GET", "/users/search?q=al").json()["users"] == []
    assert api.call("GET", "/users/search?q=bo&limit=21").status_code == 422


def test_friend_request_contract_ids_permissions_and_shared_graph(api):
    for name in PLAYERS:
        api.profile(name)
    r = api.call("POST", "/friend-requests", json={"username": "@BOB"})
    assert r.status_code == 201, r.text
    req = r.json()["request"]
    assert set(req) == {"id", "from", "to", "created_at"} and req["from"]["id"] == "user_alice"
    assert api.call("POST", "/friend-requests", json={"username": "bob"}).status_code == 409
    assert api.call("GET", "/friends", "bob").json()["incoming"] == [req]
    assert api.call("POST", f"/friend-requests/{req['id']}/accept").status_code == 404
    assert api.call("POST", f"/friend-requests/{req['id']}/accept", "carol").status_code == 404
    assert api.call("POST", f"/friend-requests/{req['id']}/accept", "bob").status_code == 200
    assert api.call("POST", f"/friend-requests/{req['id']}/accept", "bob").status_code == 404
    legacy = api.client.get("/api/me/friends", headers={"X-Player-ID": PLAYERS["alice"]}).json()[
        "items"
    ]
    assert legacy[0]["player_id"] == PLAYERS["bob"] and legacy[0]["status"] == "accepted"
    assert api.call("DELETE", "/friends/user_bob").status_code == 204
    new = api.call("POST", "/friend-requests", json={"username": "bob"}).json()["request"]
    assert new["id"] != req["id"]
    assert api.call("DELETE", f"/friend-requests/{req['id']}").status_code == 404
    assert api.call("POST", f"/friend-requests/{new['id']}/decline", "bob").status_code == 204


def test_mutual_requests_and_presence_expiry(api):
    for name in PLAYERS:
        api.profile(name)
    api.call("POST", "/friend-requests", json={"username": "bob"})
    mutual = api.call("POST", "/friend-requests", "bob", json={"username": "alice"})
    assert mutual.status_code == 200 and mutual.json()["status"] == "accepted"
    p = api.call("POST", "/presence", "bob", json={"state": "online"}).json()
    assert p == {"heartbeat_s": 20, "online_window_s": 45, "invites": []}
    assert api.call("GET", "/friends?online=true").json()["friends"][0]["online"] is True
    api.now[0] += 45_000
    assert api.call("GET", "/friends?online=true").json()["friends"] == []
    api.call("POST", "/presence", "bob", json={"state": "online"})
    api.call("POST", "/presence", "bob", json={"state": "offline"})
    assert api.call("GET", "/friends").json()["friends"][0]["online"] is False


def test_invite_countdown_answers_results_rewards_and_quest_contract(api):
    connect(api)
    d = duel(api)
    id = d["id"]
    assert d["status"] == "invited" and d["questions"] is None and d["results"] is None
    assert d["progress"] == {"me": 0, "opponent": 0} and d["my_role"] == "challenger"
    assert millis(d["invite_expires_at"]) - millis(d["created_at"]) == 30_000
    assert (
        api.call(
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        ).json()["code"]
        == "busy"
    )
    assert len(api.generated) == 1
    heartbeat = api.call("POST", "/presence", "bob", json={"state": "online"}).json()
    assert heartbeat["invites"][0]["id"] == id and heartbeat["invites"][0]["my_role"] == "opponent"
    assert api.call("GET", f"/duels/{id}", "carol").status_code == 404
    assert api.call("POST", f"/duels/{id}/accept").status_code == 403
    started = api.call("POST", f"/duels/{id}/accept", "bob").json()
    assert started["status"] == "active" and started["questions"] is None
    assert millis(started["start_at"]) - api.now[0] == 5000
    assert millis(started["ends_at"]) - millis(started["start_at"]) == 60_000
    assert (
        api.call("POST", f"/duels/{id}/answers", json={"index": 0, "choice": 1}).status_code == 409
    )
    api.now[0] = millis(started["start_at"])
    playing = api.call("GET", f"/duels/{id}").json()
    assert len(playing["questions"]) == 3
    assert "answer_index" not in json.dumps(playing) and "explanation" not in json.dumps(playing)
    for index in range(3):
        r = api.call("POST", f"/duels/{id}/answers", json={"index": index, "choice": 1})
        assert r.status_code == 200 and r.json() == {"answered": index + 1}
    assert wallet(api, "alice") == 240
    assert (
        api.call("POST", f"/duels/{id}/answers", json={"index": 0, "choice": 0}).status_code == 409
    )
    for index in range(3):
        assert (
            api.call(
                "POST", f"/duels/{id}/answers", "bob", json={"index": index, "choice": 0}
            ).status_code
            == 200
        )
    for _ in range(2):
        final = api.call("GET", f"/duels/{id}").json()
        assert final["status"] == "finished" and final["results"]["winner"] == "me"
        assert final["results"]["me"]["score"] == 3 and final["results"]["opponent"]["score"] == 0
        assert wallet(api, "alice") == 290 and wallet(api, "bob") == 260
    assert api.call("POST", f"/duels/{id}/answers", json={"index": 0, "choice": 1}).json() == {
        "answered": 1
    }
    assert api.call("GET", "/duels/current").status_code == 404
    for name in ("alice", "bob"):
        quests = api.client.get("/api/me/quests", headers={"X-Player-ID": PLAYERS[name]}).json()[
            "items"
        ]
        q = next(q for q in quests if q["id"] == "d-duel")
        assert q["progress"] == 1
        assert (
            api.client.post(
                "/api/me/quests/d-duel/claim",
                headers={"X-Player-ID": PLAYERS[name]},
                json={"period": q["period"]},
            ).json()["credited"]
            == 40
        )
    with api.repo.session(PLAYERS["alice"], serialize=False) as unit:
        assert unit.get("lessons", "lesson")["status"] == "draft"  # no silent approval mutation


@pytest.mark.parametrize(
    "action,who,terminal",
    [
        ("cancel", "alice", "cancelled"),
        ("decline", "bob", "declined"),
        ("expire", "bob", "expired"),
    ],
)
def test_invitation_terminal_states_do_not_pay(api, action, who, terminal):
    connect(api)
    d = duel(api)
    if action == "expire":
        api.now[0] += 30_000
    else:
        assert api.call("POST", f"/duels/{d['id']}/{action}", who).status_code == 200
    state = api.call("GET", f"/duels/{d['id']}").json()
    assert state["status"] == terminal and state["questions"] is None and state["results"] is None
    assert api.call("POST", f"/duels/{d['id']}/accept", "bob").status_code == 409
    assert wallet(api, "alice") == wallet(api, "bob") == 240


def test_timeout_scores_unanswered_as_wrong_and_idle_player_gets_no_coins(api):
    connect(api)
    d = duel(api)
    active = api.call("POST", f"/duels/{d['id']}/accept", "bob").json()
    api.now[0] = millis(active["start_at"])
    assert (
        api.call("POST", f"/duels/{d['id']}/answers", json={"index": 0, "choice": 1}).status_code
        == 200
    )
    api.now[0] = millis(active["ends_at"])
    assert (
        api.call(
            "POST", f"/duels/{d['id']}/answers", "bob", json={"index": 0, "choice": 1}
        ).status_code
        == 409
    )
    result = api.call("GET", f"/duels/{d['id']}").json()
    assert result["status"] == "finished"
    assert result["results"]["opponent"]["results"][0]["choice"] is None
    assert wallet(api, "alice") == 270 and wallet(api, "bob") == 240


def test_generation_rechecks_online_and_releases_reservations(api, monkeypatch):
    connect(api)

    def slow(lesson, n):
        result = api.generate(lesson, n)
        api.now[0] += 45_000
        return result

    monkeypatch.setattr(store, "quiz_from_lesson", slow)
    r = api.call(
        "POST",
        "/duels",
        json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
    )
    assert r.status_code == 409 and r.json()["code"] == "offline"
    assert api.app.state.home._query("select * from live_social_reservations") == []
    assert api.app.state.home._query("select * from live_social_duels") == []
    monkeypatch.setattr(store, "quiz_from_lesson", api.generate)
    api.call("POST", "/presence", "bob", json={"state": "online"})
    duel(api)


def test_concurrent_creators_cannot_invite_the_same_busy_opponent(api, monkeypatch):
    connect(api)
    rid = api.call("POST", "/friend-requests", "carol", json={"username": "bob"}).json()["request"][
        "id"
    ]
    api.call("POST", f"/friend-requests/{rid}/accept", "bob")
    started, release = Event(), Event()

    def slow(lesson, n):
        started.set()
        assert release.wait(8)
        return api.generate(lesson, n)

    monkeypatch.setattr(store, "quiz_from_lesson", slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            api.call,
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        )
        assert started.wait(5)
        try:
            second = api.call(
                "POST",
                "/duels",
                "carol",
                json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
            )
            assert second.status_code == 409 and second.json()["code"] == "busy"
            assert api.call("POST", "/presence", "bob", json={"state": "online"}).status_code == 200
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 201
    assert len(api.generated) == 1


def test_swagger_has_every_frontend_operation_and_bearer_authorization(api):
    assert api.client.get("/docs").status_code == 200
    spec = api.client.get("/openapi.json").json()
    paths = {path: ops for path, ops in spec["paths"].items() if path.startswith("/api/social/")}
    assert sum(len(ops) for ops in paths.values()) == 16
    for path, ops in paths.items():
        for operation in ops.values():
            assert operation["security"] == [{"ClerkSession": []}]
            assert "401" in operation["responses"]
            if "requestBody" in operation:
                assert operation["requestBody"]["content"]["application/json"]["examples"], path
    assert not spec["paths"]["/api/me/wallet"]["get"].get("security")


def test_jwks_failure_and_unconfigured_social_fail_closed_but_legacy_stays_available(
    api, monkeypatch
):
    def unavailable(*args, **kwargs):
        raise jwt.PyJWKClientConnectionError("synthetic private upstream detail")

    monkeypatch.setattr(api.app.state.clerk_social.jwks, "get_signing_key_from_jwt", unavailable)
    r = api.call("GET", "/friends")
    assert r.status_code == 503 and "upstream detail" not in r.text
    assert api.client.get("/api/me/profile").status_code == 200
    api.app.state.clerk_social.jwks = None
    assert api.call("GET", "/friends").status_code == 503
    assert api.client.get("/api/social/friends").status_code == 401


def test_generation_failure_and_disabled_generation_release_player_reservations(api, monkeypatch):
    from fastapi import HTTPException

    connect(api)
    api.app.state.settings.generation_enabled = False
    assert (
        api.call(
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        ).status_code
        == 503
    )
    assert api.app.state.home._query("select * from live_social_reservations") == []
    api.app.state.settings.generation_enabled = True

    def fail(*args):
        raise HTTPException(502, "Synthetic model failure")

    monkeypatch.setattr(store, "quiz_from_lesson", fail)
    assert (
        api.call(
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        ).status_code
        == 502
    )
    assert api.app.state.home._query("select * from live_social_reservations") == []


def test_finish_and_wallet_rewards_roll_back_together(api, monkeypatch):
    connect(api)
    d = duel(api)
    active = api.call("POST", f"/duels/{d['id']}/accept", "bob").json()
    api.now[0] = millis(active["start_at"])
    for i in range(3):
        api.call("POST", f"/duels/{d['id']}/answers", json={"index": i, "choice": 1})
    for i in range(2):
        api.call("POST", f"/duels/{d['id']}/answers", "bob", json={"index": i, "choice": 0})
    original = api.app.state.home._ledger

    def fail(db, pid, amount, reason, ref, **kwargs):
        if reason == "duel":
            raise RuntimeError("synthetic ledger failure")
        return original(db, pid, amount, reason, ref, **kwargs)

    monkeypatch.setattr(api.app.state.home, "_ledger", fail)
    with pytest.raises(RuntimeError, match="synthetic ledger"):
        api.call("POST", f"/duels/{d['id']}/answers", "bob", json={"index": 2, "choice": 0})
    assert wallet(api, "alice") == wallet(api, "bob") == 240
    progress = api.call("GET", f"/duels/{d['id']}").json()
    assert progress["status"] == "active" and progress["progress"]["opponent"] == 2
    monkeypatch.setattr(api.app.state.home, "_ledger", original)
    assert (
        api.call(
            "POST", f"/duels/{d['id']}/answers", "bob", json={"index": 2, "choice": 0}
        ).status_code
        == 200
    )
    assert wallet(api, "alice") == 290 and wallet(api, "bob") == 260


def test_current_duel_timeout_commits_rewards_even_when_it_returns_404(api):
    connect(api)
    d = duel(api)
    active = api.call("POST", f"/duels/{d['id']}/accept", "bob").json()
    api.now[0] = millis(active["start_at"])
    api.call("POST", f"/duels/{d['id']}/answers", json={"index": 0, "choice": 1})
    api.now[0] = millis(active["ends_at"])
    assert api.call("GET", "/duels/current").status_code == 404
    assert wallet(api, "alice") == 270


def test_new_api_respects_presence_privacy_and_invitation_preferences(api):
    connect(api)
    api.client.put(
        "/api/me/social-settings",
        headers={"X-Player-ID": PLAYERS["bob"]},
        json={"share_presence": False, "allow_game_invites": True},
    )
    friend = api.call("GET", "/friends").json()["friends"][0]
    assert friend["online"] is False and friend["busy"] is False
    assert (
        api.call(
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        ).json()["code"]
        == "offline"
    )
    api.client.put(
        "/api/me/social-settings",
        headers={"X-Player-ID": PLAYERS["bob"]},
        json={"share_presence": True, "allow_game_invites": False},
    )
    assert (
        api.call(
            "POST",
            "/duels",
            json={"opponent_id": "user_bob", "lesson_id": "lesson", "num_questions": 3},
        ).json()["code"]
        == "invites_disabled"
    )
    assert api.generated == []


def test_material_and_player_deletion_clear_shared_duel_copies(api):
    from learning_service.lesson import LessonSource
    from learning_service.materials import Material, Page

    connect(api)
    with api.repo.session(PLAYERS["alice"]) as unit:
        unit.put(
            "materials",
            "upload",
            Material(
                id="upload",
                name="plants.pdf",
                kind="pdf",
                pages=[Page(number=1, text="Plants", via="text")],
            ),
        )
        lesson = unit.get("lessons", "lesson", StoredLesson)
        lesson.source = LessonSource(
            material_id="upload", title="plants.pdf", pages=[1], label="Plants"
        )
    d = duel(api)
    assert (
        api.client.delete(
            "/api/materials/upload", headers={"X-Player-ID": PLAYERS["alice"]}
        ).status_code
        == 204
    )
    assert api.call("GET", f"/duels/{d['id']}", "bob").status_code == 404
    assert (
        api.client.delete("/api/me/data", headers={"X-Player-ID": PLAYERS["alice"]}).status_code
        == 204
    )
    assert api.call("GET", "/users/search?q=al", "bob").json()["users"] == []
    assert api.call("GET", "/friends", "bob").json()["friends"] == []


def test_live_duels_and_mapping_survive_sqlite_restart(api, tmp_path):
    import sqlite3

    from app.clerk_social import Identity
    from learning_service.home_store import HomeStore

    connect(api)
    d = duel(api)
    destination = str(tmp_path / "persisted-home.db")
    with sqlite3.connect(destination) as db:
        api.app.state.home._db.backup(db)
    home = HomeStore(destination)
    try:
        live = live_social.LiveSocial(home)
        identity = Identity("user_alice", PLAYERS["alice"], "sess_alice", "alice", ISSUER)
        loaded = live.get_duel(identity, d["id"])
        assert loaded["status"] == "invited" and loaded["opponent"]["id"] == "user_bob"
        assert live.friends(identity)["friends"][0]["username"] == "bob"
    finally:
        home.close()


def test_remove_friend_does_not_cancel_a_pending_request(api):
    api.profile("alice")
    api.profile("bob")
    sent = api.call("POST", "/friend-requests", json={"username": "bob"}).json()["request"]
    assert api.call("DELETE", "/friends/user_bob").status_code == 204
    assert api.call("GET", "/friends", "bob").json()["incoming"][0]["id"] == sent["id"]
    assert api.call("DELETE", f"/friend-requests/{sent['id']}").status_code == 204
