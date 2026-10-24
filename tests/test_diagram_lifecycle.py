import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.diagram_store import DiagramStore
from learning_service.store import StoredLesson
from tests.test_diagram_assembly import (
    CODE,
    PARENT,
    board,
    finish,
    post,
    save,
    setup,
    solution,
    start,
)
from tests.test_diagram_assembly import (
    game as game,
)


def completed(env):
    _, _, _, now, _, _ = env
    template, race, room = setup(env)
    start(env, race)
    for role in ("parent", "child"):
        b = board(env, race, role)
        assert save(env, race, role, solution(env, template, b)).status_code == 200
        now[0] += 100
        assert finish(env, race, role).status_code == 200
    return template, race, room


def test_finished_results_survive_cancel_attempt(game):
    client, _, _, _, tokens, _ = game
    _, race, _ = completed(game)
    path = "/api/diagram-assembly/races/" + race["id"]
    before = client.get(path + "/results", headers=tokens["parent"]).json()
    version = client.get(path, headers=tokens["parent"]).json()["version"]
    cancelled = post(
        client,
        "races/" + race["id"] + "/cancel",
        {"expected_version": version},
        tokens["parent"],
        "terminal-cancel",
    )
    assert cancelled.status_code == 409
    assert client.get(path + "/results", headers=tokens["parent"]).json() == before
    assert client.get(path + "/review", headers=tokens["child"]).status_code == 200


@pytest.mark.parametrize("stage", ["waiting", "countdown", "running", "finished", "expired"])
@pytest.mark.parametrize("change", ["unapproved", "changed"])
def test_source_withdrawal_is_monotonic_and_summary_remains_safe(game, stage, change):
    client, repo, _, now, tokens, _ = game
    if stage == "finished":
        _, race, _ = completed(game)
    else:
        _, race, _ = setup(game)
        if stage == "countdown":
            assert (
                post(
                    client,
                    "races/" + race["id"] + "/start",
                    {"expected_version": 1},
                    tokens["parent"],
                    "start-race-key",
                ).status_code
                == 200
            )
        elif stage in ("running", "expired"):
            start(game, race)
    if stage == "expired":
        now[0] = time.time_ns() // 1_000_000
        with DiagramStore(repo).transaction(write=True) as tx:
            r = tx.get("races", race["id"], lock=True)
            r["started_at"] = now[0] - 10000
            r["deadline_at"] = now[0] - 1
            tx.put("races", r["id"], r)
    with repo.session(PARENT) as unit:
        lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
        if change == "unapproved":
            lesson.status = "draft"
        else:
            lesson.visuals[0].code = CODE.replace("Sugar", "Energy store")
    path = "/api/diagram-assembly/races/" + race["id"]
    snapshot = client.get(path, headers=tokens["child"])
    assert snapshot.status_code == 200, snapshot.text
    result = snapshot.json()
    assert result["source"] == {
        "available": False,
        "reason": "source_unapproved" if change == "unapproved" else "source_changed",
    }
    expected = stage if stage in ("finished", "expired") else "cancelled"
    assert result["status"] == expected
    if expected == "cancelled":
        assert result["reason"] == result["source"]["reason"]
        assert (
            post(
                client,
                "races/" + race["id"] + "/cancel",
                {"expected_version": result["version"]},
                tokens["parent"],
                "stop-invalid-source",
            ).status_code
            == 200
        )
    else:
        summary = client.get(path + "/results", headers=tokens["child"])
        assert summary.status_code == 200 and summary.json()["status"] == stage
        assert not summary.json()["source"]["available"]
        assert (
            post(
                client,
                "races/" + race["id"] + "/cancel",
                {"expected_version": result["version"]},
                tokens["parent"],
                "stop-terminal-source",
            ).status_code
            == 409
        )
    assert client.get(path + "/review", headers=tokens["child"]).status_code == 409
    assert client.get(path + "/board", headers=tokens["child"]).status_code == 409
    with repo.session(PARENT) as unit:
        lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
        lesson.status = "approved"
        lesson.visuals[0].code = CODE
    restored = client.get(path, headers=tokens["child"]).json()
    assert restored["status"] == expected and restored["source"]["available"]
    if expected == "cancelled":
        assert (
            post(
                client,
                "races/" + race["id"] + "/start",
                {"expected_version": restored["version"]},
                tokens["parent"],
                "restart-not-allowed",
            ).status_code
            == 409
        )


def test_late_host_has_cancel_only_management_not_content(game):
    client, _, app, _, tokens, _ = game
    _, race, room = setup(game)
    start(game, race)
    original = app.state.verify_identity
    uid = "00000000-0000-4000-8000-000000000003"

    async def identity(request, token):
        if token == "synthetic-late-host":
            return {"id": uid, "email": None, "user_metadata": {}}
        return await original(request, token)

    app.state.verify_identity = identity
    third = {"Authorization": "Bearer synthetic-late-host"}
    current = client.post(
        "/api/rooms/join",
        json={"code": room["code"], "display_name": "Later player"},
        headers=third,
    ).json()
    management = "/api/diagram-assembly/rooms/" + room["id"] + "/management"
    assert client.get(management, headers=tokens["child"]).status_code == 403
    assert client.get(management, headers=third).status_code == 403
    for role in ("parent", "child"):
        assert (
            client.post(
                "/api/rooms/" + room["id"] + "/commands",
                json={"action": "leave", "expected_version": current["version"]},
                headers=tokens[role],
            ).status_code
            == 200
        )
        current = client.get("/api/rooms/" + room["id"], headers=third).json()
    minimal = client.get(management, headers=third)
    assert minimal.status_code == 200, minimal.text
    assert set(minimal.json()) == {"room_id", "race_id", "version", "status", "reason"}
    for suffix in ("board", "study", "results", "review"):
        assert (
            client.get(
                "/api/diagram-assembly/races/" + race["id"] + "/" + suffix, headers=third
            ).status_code
            == 404
        )
    for _ in range(2):
        response = post(
            client,
            "races/" + race["id"] + "/cancel",
            {"expected_version": minimal.json()["version"]},
            third,
            "late-host-cancel",
        )
        assert response.status_code == 200, response.text
        assert response.json()["reason"] == "host_cancelled"


def test_finish_and_cancel_race_have_one_terminal_outcome(game):
    client, repo, _, now, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    for role in ("parent", "child"):
        b = board(game, race, role)
        assert save(game, race, role, solution(game, template, b)).status_code == 200
    now[0] += 100
    assert finish(game, race, "parent").status_code == 200

    def act(kind):
        if kind == "finish":
            return finish(game, race, "child")
        return post(
            client,
            "races/" + race["id"] + "/cancel",
            {"expected_version": 2},
            tokens["parent"],
            "concurrent-cancel",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["finish", "cancel"]))
    assert sorted(r.status_code for r in out) == [200, 409]
    with DiagramStore(repo).transaction() as tx:
        r = tx.get("races", race["id"])
    assert r["status"] in ("finished", "cancelled")
    result = client.get(
        "/api/diagram-assembly/races/" + race["id"] + "/results", headers=tokens["parent"]
    )
    assert result.status_code == (200 if r["status"] == "finished" else 409)


def test_source_change_and_cancel_do_not_deadlock_or_reopen(game):
    client, repo, _, _, tokens, _ = game
    _, race, _ = setup(game)
    start(game, race)

    def act(kind):
        if kind == "source":
            with repo.session(PARENT) as unit:
                lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
                lesson.status = "draft"
            return 200
        return post(
            client,
            "races/" + race["id"] + "/cancel",
            {"expected_version": 2},
            tokens["parent"],
            "source-race-cancel",
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(act, ["source", "cancel"]))
    assert out[0] == 200 and out[1] in (200, 409)
    snapshot = client.get(
        "/api/diagram-assembly/races/" + race["id"], headers=tokens["child"]
    ).json()
    assert snapshot["status"] == "cancelled"
    assert snapshot["reason"] in ("host_cancelled", "source_unapproved")


def test_finished_summary_still_erased_when_source_owner_deletes_data(game):
    client, repo, _, _, tokens, _ = game
    template, race, _ = completed(game)
    assert client.delete("/api/me/data", headers=tokens["parent"]).status_code == 204
    assert (
        client.get(
            "/api/diagram-assembly/races/" + race["id"] + "/results", headers=tokens["child"]
        ).status_code
        == 404
    )
    with DiagramStore(repo).transaction() as tx:
        assert tx.get("templates", template["id"]) is None and tx.get("races", race["id"]) is None
