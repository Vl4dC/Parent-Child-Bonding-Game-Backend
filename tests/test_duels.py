"""Duel lifecycle and privacy over real Clerk-signed test tokens, memory and PostgreSQL."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.clerk_auth import player_uuid
from app.config import Settings
from app.duel_store import DuelStore
from app.main import create_app
from learning_service.lesson import Lesson, LessonRequest, LessonSection
from learning_service.store import StoredLesson, StoredQuiz
from learning_service.visual_recall import IllustratedQuestion
from tests.test_clerk_auth import ISSUER, ORIGIN, signed
from tests.test_clerk_auth import rsa_pair as rsa_pair

OWNER = player_uuid(ISSUER, "user_Alice123")
OTHER = player_uuid(ISSUER, "user_Bob123")
THIRD = player_uuid(ISSUER, "user_Casey123")
GOOD = [1, 1, 3]
WRONG = [0, 0, 0]


@pytest.fixture
def duel(repository, rsa_pair, monkeypatch):
    import learning_service.flashcards as cards
    import learning_service.safety as safety
    import learning_service.store as source_module

    def no_ai(*args, **kwargs):
        pytest.fail("Saved-question duel called an AI producer/checker")

    monkeypatch.setattr(source_module, "quiz_from_lesson", no_ai)
    monkeypatch.setattr(cards, "make_cards", no_ai)
    monkeypatch.setattr(safety, "ensure_safe", no_ai)
    lesson = StoredLesson(
        id="lesson",
        request=LessonRequest(topic="Numbers", age=12),
        status="approved",
        lesson=Lesson(
            title="Numbers",
            summary="Count and compare.",
            sections=[LessonSection(heading="Numbers", body="Two is more than one.")],
            key_points=["Count carefully"],
        ),
    )
    questions = [
        IllustratedQuestion(
            index=0,
            type="multiple_choice",
            question="One plus one?",
            options=["One", "Two", "Three", "Four"],
            answer="Two",
            explanation="PRIVATE EXPLANATION ONE",
        ),
        IllustratedQuestion(
            index=1,
            type="true_false",
            question="One is greater than two.",
            options=["True", "False"],
            answer="False",
            explanation="PRIVATE EXPLANATION TWO",
        ),
        IllustratedQuestion(
            index=2,
            type="multiple_choice",
            question="Two plus two?",
            options=["One", "Two", "Three", "Four"],
            answer="Four",
            explanation="PRIVATE EXPLANATION THREE",
        ),
    ]
    with repository.session(OWNER) as unit:
        unit.put("lessons", "lesson", lesson)
        quiz = StoredQuiz(
            id="quiz",
            lesson_id="lesson",
            title="Count together",
            dropped=0,
            questions=questions,
            approved=True,
        )
        unit.put("quizzes", "quiz", quiz)
        tournament = quiz.model_copy(
            deep=True, update={"id": "tournament", "question_set_id": "tournament-question-set"}
        )
        unit.put("tournaments", "tournament", tournament)
    private, public = rsa_pair
    app = create_app(
        Settings(
            app_env="test",
            clerk_issuer=ISSUER,
            clerk_jwt_key=public,
            clerk_authorized_parties=[ORIGIN],
            duels_enabled=True,
            flashcard_attempts_enabled=True,
            diagram_assembly_enabled=True,
            api_requests_per_minute=10000,
            _env_file=None,
        ),
        repository=repository,
    )
    now = [time.time_ns() // 1_000_000]
    app.state.duel_clock_ms = lambda: now[0]
    app.state.diagram_clock_ms = lambda: now[0]
    tokens = {
        "owner": {"Authorization": signed(private)},
        "other": {"Authorization": signed(private, sub="user_Bob123")},
        "third": {"Authorization": signed(private, sub="user_Casey123")},
    }
    with TestClient(app) as client:
        yield client, repository, app, now, tokens, quiz.question_set_id


def post(env, path, body, who="owner", key="duel-action-key"):
    return env[0].post(
        "/api/duels" + path, json=body, headers=env[4][who] | {"Idempotency-Key": key}
    )


def create(env, mode="local", key="duel-create-key", source="quiz"):
    client, _, _, _, tokens, qid = env
    body = {
        "source_kind": source,
        "source_id": source,
        "expected_question_set_id": qid if source == "quiz" else "tournament-question-set",
        "mode": mode,
    }
    if mode == "local":
        body["players"] = ["Alex", "Sam"]
        room = None
    else:
        room = client.post(
            "/api/rooms", json={"display_name": "Alex", "capacity": 4}, headers=tokens["owner"]
        ).json()
        room = client.post(
            "/api/rooms/join",
            json={"code": room["code"], "display_name": "Sam"},
            headers=tokens["other"],
        ).json()
        body |= {"room_id": room["id"], "expected_room_version": room["version"]}
    response = post(env, "", body, key=key)
    assert response.status_code == 201, response.text
    return response.json(), room, body


def start(env, match):
    client, _, _, now, tokens, _ = env
    if match["mode"] == "room":
        for who in ["owner", "other"]:
            own = client.get("/api/duels/" + match["id"], headers=tokens[who]).json()
            player = next(p for p in own["players"] if p["id"] == own["my_player_id"])
            response = client.put(
                f"/api/duels/{match['id']}/players/{player['id']}/ready",
                json={"expected_version": player["version"], "ready": True},
                headers=tokens[who] | {"Idempotency-Key": "ready-" + who},
            )
            assert response.status_code == 200, response.text
        match = client.get("/api/duels/" + match["id"], headers=tokens["owner"]).json()
    response = post(
        env,
        "/" + match["id"] + "/start",
        {"expected_version": match["version"]},
        key="duel-start-key",
    )
    assert response.status_code == 200, response.text
    now[0] = response.json()["started_at"]
    return client.get("/api/duels/" + match["id"], headers=tokens["owner"]).json()


def submit(env, match, slot, answers, who="owner", key=None):
    own = env[0].get(f"/api/duels/{match['id']}/players/{slot}/attempt", headers=env[4][who]).json()
    return post(
        env,
        f"/{match['id']}/players/{slot}/submit",
        {"expected_version": own["version"], "answers": answers},
        who,
        key or "submit-" + slot,
    )


def test_local_duel_independent_sheets_hidden_until_both_finish(duel):
    client, repo, _, _, tokens, _ = duel
    source = client.get("/api/duels/sources/quiz/quiz", headers=tokens["owner"])
    assert source.status_code == 200 and source.json()["approved"]
    assert "PRIVATE EXPLANATION" not in source.text
    match, _, body = create(duel)
    assert post(duel, "", body, key="duel-create-key").json()["id"] == match["id"]
    assert (
        client.get(f"/api/duels/{match['id']}/questions", headers=tokens["owner"]).status_code
        == 409
    )
    match = start(duel, match)
    q = client.get(f"/api/duels/{match['id']}/questions", headers=tokens["owner"])
    assert q.status_code == 200 and len(q.json()["questions"]) == 3
    assert "PRIVATE EXPLANATION" not in q.text and "answer_index" not in q.text
    a, b = [p["id"] for p in match["players"]]
    first = submit(duel, match, a, GOOD)
    assert first.status_code == 200, first.text
    assert not any(key in first.json() for key in ["score", "results", "correct", "answer_index"])
    assert (
        client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).status_code == 409
    )
    assert (
        client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"]).status_code == 409
    )
    second = submit(duel, match, b, WRONG)
    assert second.status_code == 200 and second.json()["status"] == "finished", second.text
    result = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).json()
    assert {p["id"]: p["score"] for p in result["players"]} == {a: 3, b: 0}
    assert result["winner_ids"] == [a]
    review = client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"])
    assert review.status_code == 200 and "PRIVATE EXPLANATION" in review.text
    assert client.get(f"/api/duels/{match['id']}", headers=tokens["other"]).status_code == 404
    with repo.session(OWNER, serialize=False) as unit:
        assert unit.get("quizzes", "quiz")["attempts"] == {}
        assert unit.get("quizzes", "quiz")["submission"] is None
    assert (
        client.get("/api/duels/history", headers=tokens["owner"]).json()["items"][0]["id"]
        == match["id"]
    )


def test_room_duel_private_attempts_fresh_readiness_and_countdown(duel):
    client, _, _, now, tokens, _ = duel
    match, room, _ = create(duel, "room")
    assert post(duel, f"/{match['id']}/start", {"expected_version": 1}).status_code == 409
    own, other = {}, {}
    for who, target in [("owner", own), ("other", other)]:
        snap = client.get(f"/api/duels/{match['id']}", headers=tokens[who]).json()
        target.update(next(p for p in snap["players"] if p["id"] == snap["my_player_id"]))
    path = f"/api/duels/{match['id']}/players/{own['id']}"
    assert client.get(path + "/attempt", headers=tokens["other"]).status_code == 404
    assert (
        client.put(
            path + "/ready",
            json={"expected_version": 1, "ready": True},
            headers=tokens["other"] | {"Idempotency-Key": "wrong-ready-key"},
        ).status_code
        == 404
    )
    match = start(duel, match)
    now[0] -= 1
    assert (
        client.get(f"/api/duels/{match['id']}/questions", headers=tokens["other"]).status_code
        == 200
    )
    assert submit(duel, match, own["id"], GOOD).status_code == 409
    now[0] += 1
    assert submit(duel, match, own["id"], GOOD).status_code == 200
    assert client.get(path + "/attempt", headers=tokens["other"]).status_code == 404
    assert submit(duel, match, other["id"], GOOD, "other").status_code == 200
    result = client.get(f"/api/duels/{match['id']}/results", headers=tokens["other"]).json()
    assert len(result["winner_ids"]) == 2 and all(p["rank"] == 1 for p in result["players"])
    current = client.get(f"/api/duels/rooms/{room['id']}/match", headers=tokens["other"])
    assert current.status_code == 200 and current.json()["status"] == "finished"
    latest_room = client.get("/api/rooms/" + room["id"], headers=tokens["owner"]).json()
    again = post(
        duel,
        f"/{match['id']}/rematch",
        {"expected_room_version": latest_room["version"]},
        key="room-rematch-key",
    )
    assert again.status_code == 201, again.text
    assert all(not p["ready"] and not p["submitted"] for p in again.json()["players"])


def test_terminal_protection_rematch_history_and_wrong_player(duel):
    client, _, _, _, tokens, _ = duel
    match, _, _ = create(duel, source="tournament")
    match = start(duel, match)
    for p in match["players"]:
        assert submit(duel, match, p["id"], GOOD).status_code == 200
    before = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).json()
    current = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    assert (
        post(duel, f"/{match['id']}/cancel", {"expected_version": current["version"]}).status_code
        == 409
    )
    assert client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).json() == before
    rematch = post(duel, f"/{match['id']}/rematch", {}, key="local-rematch-key")
    assert rematch.status_code == 201, rematch.text
    assert rematch.json()["id"] != match["id"] and rematch.json()["status"] == "waiting"
    assert (
        post(duel, f"/{match['id']}/rematch", {}, key="local-rematch-key").json()["id"]
        == rematch.json()["id"]
    )
    assert post(duel, f"/{match['id']}/rematch", {}, who="other").status_code == 404
    history = client.get("/api/duels/history?limit=1", headers=tokens["owner"]).json()
    next_page = client.get(
        "/api/duels/history", params={"after": history["next_cursor"]}, headers=tokens["owner"]
    ).json()
    assert len(history["items"]) == len(next_page["items"]) == 1


@pytest.mark.parametrize("mode", ["local", "room"])
def test_concurrent_different_sheets_and_exact_replays(duel, mode):
    client, _, _, _, tokens, _ = duel
    match, _, _ = create(duel, mode)
    match = start(duel, match)
    requests = []
    for i, p in enumerate(match["players"]):
        who = "owner" if mode == "local" or p["id"] == match["my_player_id"] else "other"
        requests.append((p["id"], GOOD if i == 0 else WRONG, who, p["version"]))

    def send(item):
        slot, answers, who, version = item
        return post(
            duel,
            f"/{match['id']}/players/{slot}/submit",
            {"expected_version": version, "answers": answers},
            who,
            key="parallel-" + slot,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(send, requests))
    assert all(r.status_code == 200 for r in out), [r.text for r in out]
    for item, response in zip(requests, out):
        assert send(item).json() == response.json()
    results = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"])
    assert results.status_code == 200 and sorted(p["score"] for p in results.json()["players"]) == [
        0,
        3,
    ]


def test_conflicting_first_submissions_one_winner(duel):
    match, _, _ = create(duel)
    match = start(duel, match)
    slot = match["players"][0]["id"]

    def send(answers):
        return post(
            duel,
            f"/{match['id']}/players/{slot}/submit",
            {"expected_version": 1, "answers": answers},
            key="conflict-" + str(answers[0]),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(send, [GOOD, WRONG]))
    assert sorted(r.status_code for r in out) == [200, 409]


@pytest.mark.parametrize("change", ["lesson", "question_approval", "question_snapshot"])
@pytest.mark.parametrize("stage", ["waiting", "running", "finished"])
def test_source_withdrawal_cancels_active_preserves_finished_summary(duel, change, stage):
    client, repo, _, _, tokens, _ = duel
    match, _, _ = create(duel)
    if stage != "waiting":
        match = start(duel, match)
    if stage == "finished":
        for p in match["players"]:
            assert submit(duel, match, p["id"], GOOD).status_code == 200
    with repo.session(OWNER) as unit:
        if change == "lesson":
            unit.get("lessons", "lesson")["status"] = "draft"
        elif change == "question_approval":
            unit.get("quizzes", "quiz")["approved"] = False
        else:
            unit.get("quizzes", "quiz")["title"] = "Changed reviewed source"
    snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"])
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["status"] == ("finished" if stage == "finished" else "cancelled")
    assert not snapshot.json()["source"]["available"]
    assert (
        client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"]).status_code == 409
    )
    if stage == "finished":
        assert (
            client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).status_code
            == 200
        )
    with repo.session(OWNER) as unit:
        unit.get("lessons", "lesson")["status"] = "approved"
        unit.get("quizzes", "quiz")["approved"] = True
        unit.get("quizzes", "quiz")["title"] = "Count together"
    assert (
        client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()["status"]
        == snapshot.json()["status"]
    )


@pytest.mark.parametrize("delete", ["source", "lesson", "owner_data", "participant_data"])
def test_deletion_does_not_resurrect_games_or_players(duel, delete):
    client, repo, _, _, tokens, _ = duel
    match, _, _ = create(duel, "room")
    match = start(duel, match)
    if delete in ("source", "lesson"):
        with repo.session(OWNER) as unit:
            unit.delete(
                "quizzes" if delete == "source" else "lessons",
                "quiz" if delete == "source" else "lesson",
            )
    else:
        assert (
            client.delete(
                "/api/me/data", headers=tokens["owner" if delete == "owner_data" else "other"]
            ).status_code
            == 204
        )
    if delete == "participant_data":
        assert client.get(f"/api/duels/{match['id']}", headers=tokens["other"]).status_code == 404
        snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"])
        assert snapshot.status_code == 200 and snapshot.json()["status"] == "cancelled", (
            snapshot.text
        )
        assert len(snapshot.json()["players"]) == 1
    else:
        assert client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).status_code == 404
        with DuelStore(repo).transaction(OWNER) as tx:
            assert tx.match(match["id"]) is None


def test_late_host_cancel_only_and_current_membership(duel):
    client, _, _, _, tokens, _ = duel
    match, room, _ = create(duel, "room")
    match = start(duel, match)
    current = client.post(
        "/api/rooms/join",
        json={"code": room["code"], "display_name": "Casey"},
        headers=tokens["third"],
    ).json()
    for who in ["owner", "other"]:
        assert (
            client.post(
                f"/api/rooms/{room['id']}/commands",
                json={"action": "leave", "expected_version": current["version"]},
                headers=tokens[who],
            ).status_code
            == 200
        )
        current = client.get("/api/rooms/" + room["id"], headers=tokens["third"]).json()
    management = client.get(f"/api/duels/rooms/{room['id']}/management", headers=tokens["third"])
    assert management.status_code == 200, management.text
    assert set(management.json()) == {"room_id", "match_id", "status", "version", "reason"}
    for suffix in ["", "/questions", "/results", "/review"]:
        assert (
            client.get(f"/api/duels/{match['id']}" + suffix, headers=tokens["third"]).status_code
            == 404
        )
    response = post(
        duel,
        f"/{match['id']}/cancel",
        {"expected_version": management.json()["version"]},
        who="third",
        key="late-host-cancel",
    )
    assert response.status_code == 200, response.text


def test_stale_versions_strict_bodies_and_forfeit(duel):
    client, _, _, _, tokens, qid = duel
    base = {
        "source_kind": "quiz",
        "source_id": "quiz",
        "expected_question_set_id": qid,
        "mode": "local",
        "players": ["Alex", "Sam"],
    }
    for update, status in [
        ({"score": 100}, 422),
        ({"players": ["Alex", "Alex"]}, 422),
        ({"mode": "room"}, 422),
        ({"expected_question_set_id": "old"}, 409),
        ({"question_indices": [0, 0]}, 422),
        ({"question_indices": [10]}, 422),
    ]:
        assert post(duel, "", base | update, key="strict-create-test").status_code == status
    match, _, _ = create(duel)
    match = start(duel, match)
    slot = match["players"][0]["id"]
    for answers, status in [([True, 1, 3], 422), ([1], 422), ([1, 3, 3], 422), ([-1, 1, 3], 422)]:
        assert (
            post(
                duel,
                f"/{match['id']}/players/{slot}/submit",
                {"expected_version": 1, "answers": answers},
                key="invalid-submit-key",
            ).status_code
            == status
        )
    assert (
        post(duel, f"/{match['id']}/players/{slot}/forfeit", {"expected_version": 1}).status_code
        == 200
    )
    assert submit(duel, match, slot, GOOD).status_code == 409
    other = match["players"][1]["id"]
    assert submit(duel, match, other, GOOD).status_code == 200
    results = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).json()
    assert results["winner_ids"] == [other]
