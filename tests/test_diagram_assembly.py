"""Race tests use saved source and frozen SVG fixtures, never an AI provider."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagram_render import freeze
from app.diagram_store import DiagramStore
from app.main import create_app
from app.repository import PostgresRepository
from learning_service.lesson import Lesson, LessonRequest, LessonSection, LessonSource
from learning_service.materials import Material, Page
from learning_service.store import StoredLesson, StoredVisual

PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
CODE = 'flowchart LR\n A["Sunlight"] --> B["Photosynthesis"]\n B --> C["Sugar"]'
RENDERED = json.loads((Path(__file__).parent / "fixtures" / "diagram_rendered.json").read_text())


@pytest.fixture
def game(repository, monkeypatch):
    import learning_service.safety as safety
    import learning_service.visuals.harness as harness

    def no_ai(*args, **kwargs):
        pytest.fail("Game called an AI producer/checker")

    monkeypatch.setattr(harness, "make_visuals", no_ai)
    monkeypatch.setattr(harness, "_llm", no_ai)
    monkeypatch.setattr(safety, "ensure_safe", no_ai)
    lesson = StoredLesson(
        id="diagram-lesson",
        request=LessonRequest(topic="Photosynthesis", age=12),
        lesson=Lesson(
            title="Photosynthesis",
            summary="Plants make sugars.",
            sections=[
                LessonSection(heading="Inputs and outputs", body="Light powers photosynthesis.")
            ],
            key_points=["Light provides energy"],
        ),
        status="approved",
        visuals=[
            StoredVisual(
                id="saved-flow",
                kind="diagram",
                section_index=0,
                title="Photosynthesis",
                caption="Follow the arrows.",
                alt="Light to sugar.",
                code=CODE,
            )
        ],
    )
    with repository.session(PARENT) as unit:
        unit.put("lessons", lesson.id, lesson)
    now = [time.time_ns() // 1_000_000]
    settings = Settings(
        app_env="test", demo_mode=True, diagram_assembly_enabled=True, _env_file=None
    )
    app = create_app(settings, repository=repository)
    calls = []

    def renderer(code, settings):
        assert code == CODE
        calls.append(code)
        return freeze(copy.deepcopy(RENDERED))

    app.state.diagram_renderer = renderer
    app.state.diagram_clock_ms = lambda: now[0]
    with TestClient(app) as client:
        tokens = {
            "parent": {"X-Player-ID": "00000000-0000-4000-8000-000000000001"},
            "child": {"X-Player-ID": "00000000-0000-4000-8000-000000000002"},
        }
        yield client, repository, app, now, tokens, calls


def post(client, path, body, token, key="synthetic-action-key"):
    return client.post(
        "/api/diagram-assembly/" + path, json=body, headers=token | {"Idempotency-Key": key}
    )


def setup(game, ready=True):
    client, repository, app, now, tokens, calls = game
    t = post(
        client,
        "templates",
        {"lesson_id": "diagram-lesson", "visual_id": "saved-flow"},
        tokens["parent"],
        "template-request-key",
    )
    assert t.status_code == 201, t.text
    room = client.post("/api/rooms", json={"display_name": "Alex"}, headers=tokens["parent"]).json()
    joined = client.post(
        "/api/rooms/join",
        json={"code": room["code"], "display_name": "Sam"},
        headers=tokens["child"],
    ).json()
    if ready:
        for role in ["parent", "child"]:
            joined = client.post(
                f"/api/rooms/{room['id']}/commands",
                json={"expected_version": joined["version"], "action": "ready", "ready": True},
                headers=tokens[role],
            ).json()
    r = post(
        client,
        "races",
        {
            "template_id": t.json()["id"],
            "room_id": room["id"],
            "expected_room_version": joined["version"],
        },
        tokens["parent"],
        "create-race-key",
    )
    assert r.status_code == 201, r.text
    return t.json(), r.json(), joined


def start(game, race):
    client, _, _, now, tokens, _ = game
    result = post(
        client,
        f"races/{race['id']}/start",
        {"expected_version": race["version"]},
        tokens["parent"],
        "start-race-key",
    )
    assert result.status_code == 200, result.text
    now[0] = result.json()["started_at"]
    return result.json()


def board(game, race, role):
    client, _, _, _, tokens, _ = game
    response = client.get(f"/api/diagram-assembly/races/{race['id']}/board", headers=tokens[role])
    assert response.status_code == 200, response.text
    return response.json()


def solution(game, template, board):
    _, repo, _, _, _, _ = game
    with DiagramStore(repo).transaction() as tx:
        t = tx.get("templates", template["id"])
    remaining = board["cards"].copy()
    placements = {}
    for slot in t["snapshot"]["slots"]:
        card = next(c for c in remaining if c["text"] == slot["text"])
        placements[slot["id"]] = card["id"]
        remaining.remove(card)
    return placements


def save(game, race, role, placements, version=1, key="placements-key"):
    client, _, _, _, tokens, _ = game
    return client.put(
        f"/api/diagram-assembly/races/{race['id']}/placements",
        json={"expected_version": version, "placements": placements},
        headers=tokens[role] | {"Idempotency-Key": key},
    )


def finish(game, race, role, version=2, key="finish-player-key"):
    client, _, _, _, tokens, _ = game
    return post(
        client, f"races/{race['id']}/finish", {"expected_version": version}, tokens[role], key
    )


def test_simultaneous_start_independent_boards_and_fastest_correct(game):
    client, _, _, now, tokens, calls = game
    template, race, room = setup(game)
    study = client.get(f"/api/diagram-assembly/races/{race['id']}/study", headers=tokens["child"])
    assert study.status_code == 200 and "Photosynthesis" in study.json()["reference_svg"]
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/board", headers=tokens["child"]
        ).status_code
        == 409
    )
    started = start(game, race)
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/study", headers=tokens["child"]
        ).status_code
        == 409
    )
    a, b = board(game, race, "parent"), board(game, race, "child")
    assert a["started_at"] == b["started_at"] == started["started_at"]
    assert a["puzzle_svg"] == b["puzzle_svg"] and a["slots"] == b["slots"]
    assert {c["id"] for c in a["cards"]}.isdisjoint(c["id"] for c in b["cards"])
    assert [c["text"] for c in a["cards"]] == [c["text"] for c in b["cards"]]
    assert a["question"] == b["question"] == "How does this work?"
    assert all("text" not in slot for slot in a["slots"])
    assert "Sunlight" not in a["puzzle_svg"] and "Photosynthesis" not in a["puzzle_svg"]
    assert "reference_svg" not in a and "solution" not in a
    assert save(game, race, "parent", solution(game, template, a)).status_code == 200
    now[0] += 1000
    first = finish(game, race, "parent")
    assert first.status_code == 200 and first.json()["elapsed_ms"] == 1000
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/review", headers=tokens["parent"]
        ).status_code
        == 409
    )
    assert save(game, race, "child", solution(game, template, b)).status_code == 200
    now[0] += 500
    second = finish(game, race, "child")
    assert second.json()["elapsed_ms"] == 1500
    results = client.get(
        f"/api/diagram-assembly/races/{race['id']}/results", headers=tokens["child"]
    ).json()
    assert [p["rank"] for p in results["players"]] == [1, 2]
    assert len(results["winner_ids"]) == 1
    review = client.get(
        f"/api/diagram-assembly/races/{race['id']}/review", headers=tokens["child"]
    ).json()
    assert "Photosynthesis" in review["reference_svg"] and len(review["solution"]) == 3
    assert calls == [CODE]


def test_template_cache_ownership_and_source_review_gate(game):
    client, repo, _, _, tokens, calls = game
    body = {"lesson_id": "diagram-lesson", "visual_id": "saved-flow"}
    first = post(client, "templates", body, tokens["parent"], "template-one")
    assert first.status_code == 201
    assert post(client, "templates", body, tokens["parent"], "template-one").json() == first.json()
    assert post(client, "templates", body, tokens["parent"], "template-two").json() == first.json()
    assert len(calls) == 1
    assert post(client, "templates", body, tokens["child"], "child-template").status_code == 404
    assert (
        client.get(
            "/api/diagram-assembly/templates/" + first.json()["id"], headers=tokens["child"]
        ).status_code
        == 404
    )
    with repo.session(PARENT) as unit:
        value = unit.get("lessons", "diagram-lesson", StoredLesson)
        value.status = "draft"
    assert post(client, "templates", body, tokens["parent"], "template-new").status_code == 409


def test_only_creator_host_can_start_and_everyone_must_be_ready(game):
    client, _, _, _, tokens, _ = game
    _, race, _ = setup(game, ready=False)
    assert (
        post(
            client, f"races/{race['id']}/start", {"expected_version": 1}, tokens["child"]
        ).status_code
        == 403
    )
    assert (
        post(
            client, f"races/{race['id']}/start", {"expected_version": 1}, tokens["parent"]
        ).status_code
        == 409
    )


def test_wrong_submission_keeps_clock_running_and_retries_are_stable(game):
    client, _, _, now, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    b = board(game, race, "parent")
    mapping = solution(game, template, b)
    keys = list(mapping)
    mapping[keys[0]], mapping[keys[1]] = mapping[keys[1]], mapping[keys[0]]
    assert save(game, race, "parent", mapping).status_code == 200
    now[0] += 100
    wrong = finish(game, race, "parent")
    assert wrong.status_code == 200 and wrong.json()["completed"] is False
    assert wrong.json()["correct_count"] == 1 and wrong.json()["elapsed_ms"] is None
    assert set(wrong.json()["incorrect_slot_ids"]) == set(keys[:2])
    assert finish(game, race, "parent").json() == wrong.json()
    assert finish(game, race, "parent", 3).status_code == 409
    assert (
        save(
            game, race, "parent", solution(game, template, b), version=3, key="correct-placements"
        ).status_code
        == 200
    )
    now[0] += 700
    correct = finish(game, race, "parent", 4, key="correct-finish")
    assert correct.json()["elapsed_ms"] == 800 and correct.json()["submissions"] == 2
    assert finish(game, race, "parent", 4, key="correct-finish").json() == correct.json()
    assert save(game, race, "parent", mapping, 5, key="after-finish-key").status_code == 409


def test_invalid_and_other_player_cards_cannot_be_submitted(game):
    _, _, _, _, _, _ = game
    template, race, _ = setup(game)
    start(game, race)
    a, b = board(game, race, "parent"), board(game, race, "child")
    mapping = solution(game, template, a)
    ids = list(mapping)
    duplicated = mapping | {ids[1]: mapping[ids[0]]}
    assert save(game, race, "parent", duplicated).status_code == 422
    foreign = mapping | {ids[0]: b["cards"][0]["id"]}
    assert save(game, race, "parent", foreign).status_code == 422
    assert save(game, race, "parent", {"unknown": "unknown"}).status_code == 422
    assert finish(game, race, "parent", 1).status_code == 409
    assert board(game, race, "parent")["version"] == 1


def test_ties_and_no_client_time_or_score(game):
    client, _, _, now, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    for role in ("parent", "child"):
        b = board(game, race, role)
        assert save(game, race, role, solution(game, template, b)).status_code == 200
    forbidden = post(
        client,
        f"races/{race['id']}/finish",
        {"expected_version": 2, "elapsed_ms": 1},
        tokens["parent"],
    )
    assert forbidden.status_code == 422
    now[0] += 600
    for role in ("parent", "child"):
        assert finish(game, race, role).status_code == 200
    out = client.get(
        f"/api/diagram-assembly/races/{race['id']}/results", headers=tokens["parent"]
    ).json()
    assert len(out["winner_ids"]) == 2 and [p["rank"] for p in out["players"]] == [1, 1]


def test_forfeit_deadline_and_cancel(game):
    client, _, _, now, tokens, _ = game
    _, race, _ = setup(game)
    start(game, race)
    forfeited = post(
        client, f"races/{race['id']}/forfeit", {"expected_version": 1}, tokens["child"]
    )
    assert forfeited.status_code == 200 and forfeited.json()["forfeited"]
    now[0] += 15 * 60 * 1000
    assert finish(game, race, "parent", 1).status_code == 409
    result = client.get(
        f"/api/diagram-assembly/races/{race['id']}/results", headers=tokens["parent"]
    ).json()
    assert result["status"] == "expired" and result["winner_ids"] == []
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/review", headers=tokens["parent"]
        ).status_code
        == 200
    )
    cancelled = post(
        client,
        f"races/{race['id']}/cancel",
        {"expected_version": 2},
        tokens["parent"],
        "cancel-race-key",
    )
    assert cancelled.status_code == 200
    assert (
        post(
            client,
            f"races/{race['id']}/cancel",
            {"expected_version": 2},
            tokens["parent"],
            "cancel-race-key",
        ).json()
        == cancelled.json()
    )
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/review", headers=tokens["parent"]
        ).status_code
        == 409
    )


def test_concurrent_player_updates_detect_stale_version(game):
    _, _, _, _, _, _ = game
    template, race, _ = setup(game)
    start(game, race)
    b = board(game, race, "parent")
    mapping = solution(game, template, b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda i: save(game, race, "parent", mapping, key=f"concurrent-key-{i}"), range(2)
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_data_deletion_erases_cross_owner_participation_and_source_cascades(game):
    client, repo, _, _, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    assert client.delete("/api/me/data", headers=tokens["child"]).status_code == 204
    with DiagramStore(repo).transaction() as tx:
        assert all(p["user_id"] != CHILD for p in tx.players(race["id"]))
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}/board", headers=tokens["child"]
        ).status_code
        == 404
    )
    assert client.delete("/api/me/data", headers=tokens["parent"]).status_code == 204
    with DiagramStore(repo).transaction() as tx:
        assert tx.get("templates", template["id"]) is None
        assert tx.get("races", race["id"]) is None
        assert tx.players(race["id"]) == []


def test_source_change_during_render_does_not_commit_artifact(game):
    client, repo, app, _, tokens, _ = game

    def changed(code, settings):
        with repo.session(PARENT) as unit:
            lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
            lesson.visuals[0].code = 'flowchart TD\n A["Changed"] --> B["Source"]'
        return freeze(copy.deepcopy(RENDERED))

    app.state.diagram_renderer = changed
    assert (
        post(
            client,
            "templates",
            {"lesson_id": "diagram-lesson", "visual_id": "saved-flow"},
            tokens["parent"],
        ).status_code
        == 409
    )


def test_renderer_holds_no_database_lock_and_delete_cannot_resurrect(game):
    client, repo, app, _, tokens, _ = game

    def deleted(code, settings):
        response = client.delete("/api/me/data", headers=tokens["parent"])
        assert response.status_code == 204
        return freeze(copy.deepcopy(RENDERED))

    app.state.diagram_renderer = deleted
    result = post(
        client,
        "templates",
        {"lesson_id": "diagram-lesson", "visual_id": "saved-flow"},
        tokens["parent"],
    )
    assert result.status_code == 404


def test_raw_database_access_and_restart(game):
    client, repo, _, now, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    if not repo.durable:
        pytest.skip("Raw SQL privileges/restart require PostgreSQL")
    with repo.control() as conn:
        for table in ("diagram_templates", "diagram_races", "diagram_players", "diagram_requests"):
            assert not conn.execute(
                "select has_table_privilege(%s,%s,%s)",
                ("authenticated", "api_private." + table, "SELECT"),
            ).fetchone()[0]
    restarted = PostgresRepository(repo.url)
    try:
        from app.diagram_game import DiagramGame

        restored = DiagramGame(restarted, clock=lambda: now[0])
        assert (
            restored.board(race["id"], CHILD)["puzzle_svg"]
            == board(game, race, "child")["puzzle_svg"]
        )
    finally:
        restarted.close()


def test_feature_defaults_off_without_authentication():
    app = create_app(Settings(app_env="test", demo_mode=True, _env_file=None))
    with TestClient(app) as client:
        assert client.get("/api/diagram-assembly/races/missing").status_code == 503
        assert (
            client.get(
                "/api/diagram-assembly/races/missing", headers={"Authorization": "Bearer bogus"}
            ).status_code
            == 503
        )


def test_room_discovery_countdown_and_repeat_round(game):
    client, _, _, now, tokens, _ = game
    template, race, room = setup(game)
    path = f"/api/diagram-assembly/rooms/{room['id']}/race"
    assert client.get(path, headers=tokens["child"]).json()["id"] == race["id"]
    out = post(
        client,
        f"races/{race['id']}/start",
        {"expected_version": 1},
        tokens["parent"],
        "start-race-key",
    )
    assert out.status_code == 200 and out.json()["status"] == "countdown"
    b = board(game, race, "parent")
    assert save(game, race, "parent", solution(game, template, b)).status_code == 409
    assert out.json()["started_at"] == now[0] + 5000
    now[0] = out.json()["started_at"]
    for role in ("parent", "child"):
        assert (
            post(
                client, f"races/{race['id']}/forfeit", {"expected_version": 1}, tokens[role]
            ).status_code
            == 200
        )
    next_round = post(
        client,
        "races",
        {
            "template_id": template["id"],
            "room_id": room["id"],
            "expected_room_version": room["version"],
        },
        tokens["parent"],
        "repeat-round-key",
    )
    assert next_round.status_code == 201, next_round.text
    assert next_round.json()["id"] != race["id"]
    assert client.get(path, headers=tokens["child"]).json()["id"] == next_round.json()["id"]


def test_current_host_can_cancel_after_creator_leaves(game):
    client, _, _, _, tokens, _ = game
    _, race, room = setup(game)
    assert (
        client.post(
            f"/api/rooms/{room['id']}/commands",
            json={"action": "leave", "expected_version": room["version"]},
            headers=tokens["parent"],
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/diagram-assembly/races/{race['id']}", headers=tokens["parent"]
        ).status_code
        == 404
    )
    assert (
        post(
            client,
            f"races/{race['id']}/cancel",
            {"expected_version": 1},
            tokens["child"],
            "host-cancel-key",
        ).status_code
        == 200
    )


def test_equal_label_cards_are_interchangeable(game):
    client, repo, app, _, _, _ = game
    copied = copy.deepcopy(RENDERED)
    copied["svg"] = copied["svg"].replace("Photosynthesis", "Sunlight")
    copied["labels"][1]["text"] = "Sunlight"
    with repo.session(PARENT) as unit:
        lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
        lesson.visuals[0].code = CODE.replace("Photosynthesis", "Sunlight")
    app.state.diagram_renderer = lambda code, settings: freeze(copy.deepcopy(copied))
    template, race, _ = setup(game)
    start(game, race)
    b = board(game, race, "parent")
    mapping = solution(game, template, b)
    with DiagramStore(repo).transaction() as tx:
        slots = tx.get("templates", template["id"])["snapshot"]["slots"]
    equal = [s["id"] for s in slots if s["text"] == "Sunlight"]
    mapping[equal[0]], mapping[equal[1]] = mapping[equal[1]], mapping[equal[0]]
    assert save(game, race, "parent", mapping).status_code == 200
    result = finish(game, race, "parent")
    assert result.status_code == 200 and result.json()["completed"] is True


def test_late_joiner_is_not_added_to_captured_race(game):
    client, _, app, _, tokens, _ = game
    _, race, room = setup(game)
    third = {"X-Player-ID": "00000000-0000-4000-8000-000000000003"}
    assert (
        client.post(
            "/api/rooms/join", json={"code": room["code"], "display_name": "Third"}, headers=third
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/diagram-assembly/rooms/{room['id']}/race", headers=third).status_code
        == 404
    )
    assert (
        client.get(f"/api/diagram-assembly/races/{race['id']}/study", headers=third).status_code
        == 404
    )


def test_concurrent_account_delete_cannot_resurrect_player_data(game):
    client, repo, _, _, tokens, _ = game
    template, race, _ = setup(game)
    start(game, race)
    b = board(game, race, "child")
    assert save(game, race, "child", solution(game, template, b)).status_code == 200

    def act(which):
        if which == "finish":
            return finish(game, race, "child")
        return client.delete("/api/me/data", headers=tokens["child"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(act, ["finish", "delete"]))
    assert responses[0].status_code in (200, 404)
    assert responses[1].status_code == 204
    with DiagramStore(repo).transaction() as tx:
        assert not any(p["user_id"] == CHILD for p in tx.players(race["id"]))


def test_material_deletion_cascades_frozen_diagram_and_room_match(game):
    client, repo, _, _, tokens, _ = game
    with repo.session(PARENT) as unit:
        unit.put(
            "materials",
            "source-material",
            Material(
                id="source-material",
                name="schoolwork.pdf",
                kind="pdf",
                confirmed=True,
                pages=[Page(number=1, text="Light powers photosynthesis.", via="text")],
            ),
        )
        lesson = unit.get("lessons", "diagram-lesson", StoredLesson)
        lesson.source = LessonSource(
            material_id="source-material", title="Schoolwork", pages=[1], label="page 1"
        )
    template, race, _ = setup(game)
    assert (
        client.delete("/api/materials/source-material", headers=tokens["parent"]).status_code == 204
    )
    with DiagramStore(repo).transaction() as tx:
        assert tx.get("templates", template["id"]) is None
        assert tx.get("races", race["id"]) is None
        assert tx.players(race["id"]) == []
