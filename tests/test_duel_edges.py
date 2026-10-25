"""Concurrency, expiry, cross-game exclusion, deletion and durable restart acceptance."""

import copy
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import HTTPException

from app.duel_game import DuelGame
from app.duel_store import DuelStore, DuelTransaction
from app.repository import PostgresRepository
from learning_service.store import StoredLesson, StoredVisual
from tests.test_clerk_auth import rsa_pair as rsa_pair
from tests.test_duels import GOOD, OWNER, THIRD, create, post, start, submit
from tests.test_duels import duel as duel


def test_final_submit_versus_cancel_has_one_terminal_outcome(duel):
    client, _, _, _, tokens, _ = duel
    match, _, _ = create(duel)
    match = start(duel, match)
    a, b = [p["id"] for p in match["players"]]
    assert submit(duel, match, a, GOOD).status_code == 200
    current = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()

    def act(kind):
        if kind == "finish":
            return submit(duel, match, b, GOOD)
        return post(
            duel,
            f"/{match['id']}/cancel",
            {"expected_version": current["version"]},
            key="race-cancel-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["finish", "cancel"]))
    assert sorted(r.status_code for r in out) == [200, 409], [r.text for r in out]
    snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    result = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"])
    assert snapshot["status"] in ("finished", "cancelled")
    assert result.status_code == (200 if snapshot["status"] == "finished" else 409)


@pytest.mark.parametrize("started", [False, True])
def test_expiry_protects_results_and_blocks_new_writes(duel, started):
    client, repo, _, now, tokens, _ = duel
    match, _, _ = create(duel)
    if started:
        match = start(duel, match)
        assert submit(duel, match, match["players"][0]["id"], GOOD).status_code == 200
    with DuelStore(repo).transaction(OWNER, write=True) as tx:
        m = tx.match(match["id"], lock=True)
        m["expires_at"] = now[0] - 1
        tx.put_match(m)
    snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    assert snapshot["status"] == "expired"
    assert (
        post(duel, f"/{match['id']}/cancel", {"expected_version": snapshot["version"]}).status_code
        == 409
    )
    results = client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"])
    assert results.status_code == 200 and results.json()["status"] == "expired"
    if started:
        assert len(results.json()["winner_ids"]) == 1
        assert submit(duel, match, match["players"][1]["id"], GOOD).status_code == 409
    else:
        assert results.json()["winner_ids"] == []


def test_cancel_rechecks_expiry_after_row_lock_wait(duel):
    _, repo, _, now, _, _ = duel
    if not repo.durable:
        pytest.skip("Row-lock timing requires PostgreSQL")
    match, _, _ = create(duel)
    entered = threading.Event()

    def clock():
        value = now[0]
        entered.set()
        return value

    service = DuelGame(repo, clock)

    def cancel():
        try:
            service.start_or_cancel(
                match["id"], OWNER, {"expected_version": 1}, "locked-expiry-key", "cancel"
            )
        except HTTPException as exc:
            return exc.status_code
        return 200

    with ThreadPoolExecutor(max_workers=1) as pool:
        with repo.control() as connection:
            tx = DuelTransaction(repo, connection=connection)
            m = tx.match(match["id"], lock=True)
            m["expires_at"] = now[0] + 100
            tx.put_match(m)
            pending = pool.submit(cancel)
            assert entered.wait(2)
            now[0] += 200
        assert pending.result(timeout=5) == 409


def test_source_update_racing_finish_is_atomic(duel):
    client, repo, _, _, tokens, _ = duel
    match, _, _ = create(duel)
    match = start(duel, match)
    a, b = [p["id"] for p in match["players"]]
    assert submit(duel, match, a, GOOD).status_code == 200

    def act(kind):
        if kind == "source":
            with repo.session(OWNER) as unit:
                unit.get("quizzes", "quiz")["approved"] = False
            return 200
        return submit(duel, match, b, GOOD).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["source", "finish"]))
    assert out[0] == 200 and out[1] in (200, 409)
    snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    assert snapshot["status"] in ("finished", "cancelled") and not snapshot["source"]["available"]
    assert (
        client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"]).status_code == 409
    )


def test_quiz_practice_changes_do_not_invalidate_shared_questions(duel):
    client, _, _, _, tokens, _ = duel
    match, _, _ = create(duel)
    assert (
        client.post(
            "/api/quiz/quiz/check", json={"index": 0, "choice": 1}, headers=tokens["owner"]
        ).status_code
        == 200
    )
    snapshot = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    assert snapshot["status"] == "waiting" and snapshot["source"]["available"]


def test_selected_question_positions_are_frozen_and_reindexed(duel):
    client, _, _, _, tokens, qid = duel
    response = post(
        duel,
        "",
        {
            "source_kind": "quiz",
            "source_id": "quiz",
            "expected_question_set_id": qid,
            "mode": "local",
            "players": ["Alex", "Sam"],
            "question_indices": [2, 0],
        },
    )
    assert response.status_code == 201, response.text
    match = start(duel, response.json())
    questions = client.get(f"/api/duels/{match['id']}/questions", headers=tokens["owner"]).json()[
        "questions"
    ]
    assert [q["index"] for q in questions] == [0, 1]
    assert questions[0]["question"] == "Two plus two?"
    for player in match["players"]:
        assert submit(duel, match, player["id"], [3, 1]).status_code == 200
    assert all(
        p["score"] == 2
        for p in client.get(f"/api/duels/{match['id']}/results", headers=tokens["owner"]).json()[
            "players"
        ]
    )


def test_duplicate_create_and_lifetime_quota(duel):
    client, repo, _, _, tokens, qid = duel
    body = {
        "source_kind": "quiz",
        "source_id": "quiz",
        "expected_question_set_id": qid,
        "mode": "local",
        "players": ["Alex", "Sam"],
        "room_id": None,
        "expected_room_version": None,
        "question_indices": None,
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(lambda _: post(duel, "", body, key="duplicate-create-key"), range(6)))
    assert all(r.status_code == 201 for r in out), [r.text for r in out]
    assert len({r.json()["id"] for r in out}) == 1
    service = DuelGame(repo, match_limit=1)
    assert service.create(OWNER, body, "duplicate-create-key")["id"] == out[0].json()["id"]
    with pytest.raises(HTTPException) as exc:
        service.create(OWNER, body, "different-limit-key")
    assert exc.value.status_code == 429
    assert client.get("/api/duels/history", headers=tokens["owner"]).json()["next_cursor"] is None


def prepare_diagram(env, room):
    from app.diagram_render import freeze
    from tests.test_diagram_assembly import CODE, RENDERED

    client, repo, app, _, tokens, _ = env
    with repo.session(OWNER) as unit:
        lesson = unit.get("lessons", "lesson", StoredLesson)
        lesson.visuals = [
            StoredVisual(
                id="flow",
                kind="diagram",
                section_index=0,
                title="Flow",
                caption="Flow",
                alt="Flow",
                code=CODE,
            )
        ]
    app.state.diagram_renderer = lambda *_: freeze(copy.deepcopy(RENDERED))
    template = client.post(
        "/api/diagram-assembly/templates",
        json={"lesson_id": "lesson", "visual_id": "flow"},
        headers=tokens["owner"] | {"Idempotency-Key": "diagram-source-key"},
    )
    assert template.status_code == 201, template.text
    return {
        "template_id": template.json()["id"],
        "room_id": room["id"],
        "expected_room_version": room["version"],
    }


def test_duel_and_diagram_cannot_both_occupy_one_room(duel):
    client, _, _, _, tokens, qid = duel
    match, room, _ = create(duel, "room")
    diagram_body = prepare_diagram(duel, room)
    response = client.post(
        "/api/diagram-assembly/races",
        json=diagram_body,
        headers=tokens["owner"] | {"Idempotency-Key": "blocked-diagram-key"},
    )
    assert response.status_code == 409, response.text
    assert post(duel, f"/{match['id']}/cancel", {"expected_version": 1}).status_code == 200
    response = client.post(
        "/api/diagram-assembly/races",
        json=diagram_body,
        headers=tokens["owner"] | {"Idempotency-Key": "now-diagram-key"},
    )
    assert response.status_code == 201, response.text
    body = {
        "source_kind": "quiz",
        "source_id": "quiz",
        "expected_question_set_id": qid,
        "mode": "room",
        "room_id": room["id"],
        "expected_room_version": room["version"],
    }
    assert post(duel, "", body, key="blocked-duel-key").status_code == 409


def test_simultaneous_cross_game_create_accepts_one_game(duel):
    client, _, _, _, tokens, qid = duel
    _, room, _ = create(duel, "room")
    old = client.get(f"/api/duels/rooms/{room['id']}/match", headers=tokens["owner"]).json()
    assert post(duel, f"/{old['id']}/cancel", {"expected_version": 1}).status_code == 200
    diagram_body = prepare_diagram(duel, room)
    duel_body = {
        "source_kind": "quiz",
        "source_id": "quiz",
        "expected_question_set_id": qid,
        "mode": "room",
        "room_id": room["id"],
        "expected_room_version": room["version"],
    }

    def act(kind):
        if kind == "duel":
            return post(duel, "", duel_body, key="concurrent-duel-create")
        return client.post(
            "/api/diagram-assembly/races",
            json=diagram_body,
            headers=tokens["owner"] | {"Idempotency-Key": "concurrent-diagram-create"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["duel", "diagram"]))
    assert sorted(r.status_code for r in out) == [201, 409], [r.text for r in out]


def test_disabling_duel_feature_does_not_hide_active_room_occupancy(duel):
    client, _, app, _, tokens, _ = duel
    _, room, _ = create(duel, "room")
    diagram_body = prepare_diagram(duel, room)
    app.state.settings.duels_enabled = False
    response = client.post(
        "/api/diagram-assembly/races",
        json=diagram_body,
        headers=tokens["owner"] | {"Idempotency-Key": "disabled-active-duel"},
    )
    assert response.status_code == 409, response.text


def test_durable_restart_and_raw_role_privileges(duel):
    _, repo, _, _, _, _ = duel
    if not repo.durable:
        pytest.skip("Durable restart and SQL grants require PostgreSQL")
    match, _, _ = create(duel)
    match = start(duel, match)
    for player in match["players"]:
        assert submit(duel, match, player["id"], GOOD).status_code == 200
    fresh = PostgresRepository(os.environ["TEST_DATABASE_URL"])
    try:
        assert DuelGame(fresh).results(match["id"], OWNER)["status"] == "finished"
    finally:
        fresh.close()
    for table in ["duel_matches", "duel_players", "duel_requests", "card_attempt_links"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with repo.session(OWNER) as unit:
                unit.connection.execute("select * from api_private." + table)


def test_participant_erasure_after_finish_removes_personal_sheet(duel):
    client, repo, _, _, tokens, _ = duel
    match, _, _ = create(duel, "room")
    match = start(duel, match)
    for p in match["players"]:
        who = "owner" if p["id"] == match["my_player_id"] else "other"
        assert submit(duel, match, p["id"], GOOD, who).status_code == 200
    assert client.delete("/api/me/data", headers=tokens["other"]).status_code == 204
    result = client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"])
    assert result.status_code == 200 and len(result.json()["players"]) == 1, result.text
    with DuelStore(repo).transaction(OWNER) as tx:
        assert len(tx.players(match["id"])) == 1


def test_review_teaches_the_solution_even_when_both_players_forfeit(duel):
    client, _, _, _, tokens, _ = duel
    match, _, _ = create(duel)
    match = start(duel, match)
    for player in match["players"]:
        response = post(
            duel,
            f"/{match['id']}/players/{player['id']}/forfeit",
            {"expected_version": player["version"]},
            key="forfeit-" + player["id"],
        )
        assert response.status_code == 200, response.text
    response = client.get(f"/api/duels/{match['id']}/review", headers=tokens["owner"])
    assert response.status_code == 200, response.text
    assert [q["answer_index"] for q in response.json()["solution"]] == GOOD
    assert response.json()["winner_ids"] == []
    assert all(a["results"] is None for a in response.json()["attempts"])


def test_room_start_rejects_a_countdown_beyond_the_remaining_lifetime(duel):
    client, repo, _, now, tokens, _ = duel
    match, _, _ = create(duel, "room")
    with DuelStore(repo).transaction(OWNER, write=True) as tx:
        m = tx.match(match["id"], lock=True)
        m["expires_at"] = now[0] + 4999
        tx.put_match(m)
    for who in ["owner", "other"]:
        own = client.get(f"/api/duels/{match['id']}", headers=tokens[who]).json()
        player = next(p for p in own["players"] if p["id"] == own["my_player_id"])
        response = client.put(
            f"/api/duels/{match['id']}/players/{player['id']}/ready",
            json={"expected_version": player["version"], "ready": True},
            headers=tokens[who] | {"Idempotency-Key": "late-ready-" + who},
        )
        assert response.status_code == 200, response.text
    current = client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()
    assert (
        post(duel, f"/{match['id']}/start", {"expected_version": current["version"]}).status_code
        == 409
    )
    assert (
        client.get(f"/api/duels/{match['id']}", headers=tokens["owner"]).json()["status"]
        == "waiting"
    )


def test_erasure_cleans_late_host_receipts_without_a_player_slot(duel):
    client, repo, _, _, tokens, _ = duel
    match, room, _ = create(duel, "room")
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
    assert (
        client.post(
            "/api/rooms/join",
            json={"code": room["code"], "display_name": "Alex"},
            headers=tokens["owner"],
        ).status_code
        == 200
    )
    assert (
        post(
            duel,
            f"/{match['id']}/cancel",
            {"expected_version": 1},
            who="third",
            key="late-host-private-receipt",
        ).status_code
        == 200
    )
    assert client.delete("/api/me/data", headers=tokens["third"]).status_code == 204
    with DuelStore(repo).transaction(OWNER) as tx:
        stored = tx.match(match["id"])
        assert stored is not None and THIRD not in str(stored)
