from concurrent.futures import ThreadPoolExecutor

import pytest

from app.worker import work_once
from tests.learning.test_app import create_quiz
from tests.learning.test_materials_api import book, lesson_from

PARENT = "00000000-0000-4000-8000-000000000001"


def child_headers(client):
    session = client.post(
        "/auth/login", json={"email": "child@demo.example.com", "password": "DemoChild123!"}
    ).json()
    return {"Authorization": "Bearer " + session["access_token"]}


def approval():
    return {"expected_revision": 1, "confirmed": True}


def test_every_data_route_needs_verified_identity(client):
    client.headers.pop("Authorization")
    for method, url, body in [
        ("get", "/api/me/profile", None),
        ("get", "/api/me/progress", None),
        ("get", "/api/resources/lessons", None),
        ("post", "/api/lessons", {"topic": "x", "age": 10}),
        ("post", "/api/rooms", {"display_name": "A"}),
    ]:
        response = getattr(client, method)(url, **({"json": body} if body else {}))
        assert response.status_code == 401


def test_profile_validation_and_ownership(client):
    assert (
        client.put(
            "/api/me/profile",
            json={"display_name": "  Alex  ", "preferences": {"motion": "reduce"}},
        ).json()["display_name"]
        == "Alex"
    )
    assert (
        client.get("/api/me/profile", headers=child_headers(client)).json()["display_name"]
        == "Learner"
    )
    assert client.put("/api/me/profile", json={"display_name": "  "}).status_code == 422
    assert (
        client.put("/api/me/profile", json={"preferences": {"theme": "invented"}}).status_code
        == 422
    )
    assert client.put("/api/me/profile", json={"account_type": "admin"}).status_code == 422
    assert client.get("/api/me/profile").headers["cache-control"] == "no-store"


def test_other_user_cannot_read_generate_or_delete_owned_resources(client, lesson_id):
    headers = child_headers(client)
    mid = book(client)["id"]
    quiz = create_quiz(client, lesson_id)
    for url in [f"/api/lessons/{lesson_id}", f"/api/materials/{mid}", f"/api/quiz/{quiz['id']}"]:
        assert client.get(url, headers=headers).status_code == 404
    assert client.delete(f"/api/materials/{mid}", headers=headers).status_code == 404
    assert (
        client.post(f"/api/lessons/{lesson_id}/quiz", headers=headers, json={}).status_code == 404
    )
    assert client.get("/api/resources/lessons", headers=headers).json()["items"] == []
    assert client.get(f"/api/lessons/{lesson_id}").status_code == 200


def test_material_confirmation_revision_and_cascade_delete(client):
    mid = book(client)["id"]
    response = client.put(
        f"/api/materials/{mid}/pages",
        json={
            "expected_revision": 1,
            "pages": [
                {
                    "number": 1,
                    "text": "Plants use sunlight to make food. This is photosynthesis.",
                    "via": "text",
                }
            ],
        },
    )
    assert response.json() == {"revision": 2, "confirmed": False}
    assert (
        client.post(
            "/api/lessons", json={"topic": "photosynthesis", "age": 12, "material_id": mid}
        ).status_code
        == 409
    )
    assert client.post(f"/api/materials/{mid}/confirm", json=approval()).status_code == 409
    assert (
        client.post(
            f"/api/materials/{mid}/confirm", json={"expected_revision": 2, "confirmed": True}
        ).status_code
        == 200
    )
    lesson = lesson_from(client, mid, "page 1").json()
    quiz = create_quiz(client, lesson["id"])
    assert client.delete(f"/api/materials/{mid}").status_code == 204
    assert client.get(f"/api/lessons/{lesson['id']}").status_code == 404
    assert client.get(f"/api/quiz/{quiz['id']}").status_code == 404


def test_lesson_revision_preserves_old_content_and_requires_new_approval(client, lesson_id):
    original = client.get(f"/api/lessons/{lesson_id}").json()
    revised = client.post(
        f"/api/lessons/{lesson_id}/revisions",
        json={
            "lesson": {
                "title": "New title",
                "summary": "Changed",
                "sections": [{"heading": "H", "body": "B"}],
                "key_points": ["K"],
            }
        },
    ).json()
    assert revised["id"] != lesson_id and revised["status"] == "draft"
    assert client.get(f"/api/lessons/{lesson_id}").json() == original
    assert client.post("/api/tournament", json={"lesson_id": revised["id"]}).status_code == 409
    assert client.post(f"/api/lessons/{revised['id']}/approve", json=approval()).status_code == 409


def test_attempts_share_immutable_question_set_but_not_answers(client, lesson_id, repository):
    quiz = create_quiz(client, lesson_id)
    other = client.post(f"/api/quiz/{quiz['id']}/attempts").json()
    assert other["id"] != quiz["id"] and other["questions"] == quiz["questions"]
    client.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0})
    assert client.post(f"/api/quiz/{other['id']}/followup", json={}).status_code == 409
    with repository.session(PARENT) as unit:
        assert len(unit.list("question_sets")) == 1
        attempts = unit.list("quizzes")
        assert len(attempts) == 2 and all("questions" not in data for _, data in attempts)


def test_completion_and_progress_are_idempotent(client, lesson_id):
    a = client.post(f"/api/lessons/{lesson_id}/complete").json()
    assert client.post(f"/api/lessons/{lesson_id}/complete").json() == a
    quiz = create_quiz(client, lesson_id)
    client.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 1})
    client.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0})
    stats = client.get("/api/me/progress").json()
    assert (
        stats["lessons_completed"] == 1
        and stats["quiz_answers"] == 1
        and stats["correct_answers"] == 1
    )
    assert client.get("/api/me/progress", headers=child_headers(client)).json()["quiz_answers"] == 0


def test_tournament_review_gate_and_repeated_submission(client, lesson_id):
    tid = client.post("/api/tournament", json={"lesson_id": lesson_id}).json()["id"]
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 0]}).status_code == 409
    assert "questions" in client.get(f"/api/tournament/{tid}/review").json()
    client.post(f"/api/tournament/{tid}/approve", json=approval())
    first = client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 0]})
    assert first.status_code == 200
    assert (
        client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 0]}).json()
        == first.json()
    )
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [0, 0]}).status_code == 409
    assert client.get("/api/me/progress").json()["tournaments_submitted"] == 1


def test_private_room_join_reconnect_versions_host_transfer(client):
    room = client.post("/api/rooms", json={"display_name": "Alex", "capacity": 2}).json()
    headers = child_headers(client)
    assert client.get(f"/api/rooms/{room['id']}", headers=headers).status_code == 404
    joined = client.post(
        "/api/rooms/join", headers=headers, json={"code": room["code"], "display_name": "Sam"}
    ).json()
    assert len(joined["members"]) == 2 and joined["version"] == 2
    assert (
        client.post(
            "/api/rooms/join", headers=headers, json={"code": room["code"], "display_name": "Sam"}
        ).json()
        == joined
    )
    assert client.get(f"/api/rooms/{room['id']}", headers=headers).json() == joined
    assert (
        client.post(
            f"/api/rooms/{room['id']}/commands",
            headers=headers,
            json={"expected_version": 2, "action": "close"},
        ).status_code
        == 403
    )
    ready = client.post(
        f"/api/rooms/{room['id']}/commands",
        headers=headers,
        json={"expected_version": 2, "action": "ready", "ready": True},
    ).json()
    assert ready["version"] == 3 and ready["members"][1]["ready"]
    assert (
        client.post(
            f"/api/rooms/{room['id']}/commands", json={"expected_version": 2, "action": "leave"}
        ).status_code
        == 409
    )
    assert client.post(
        f"/api/rooms/{room['id']}/commands", json={"expected_version": 3, "action": "leave"}
    ).json() == {"left": True}
    snapshot = client.get(f"/api/rooms/{room['id']}", headers=headers).json()
    assert snapshot["host_id"] == joined["members"][1]["user_id"]
    assert client.get(f"/api/rooms/{room['id']}").status_code == 404


def test_concurrent_room_commands_detect_stale_versions(client):
    room = client.post("/api/rooms", json={"display_name": "Alex"}).json()

    def ready(value):
        return client.post(
            f"/api/rooms/{room['id']}/commands",
            json={"expected_version": 1, "action": "ready", "ready": value},
        ).status_code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(ready, [True, False]))
    assert sorted(results) == [200, 409]


def test_jobs_idempotency_validation_and_owner_isolation(client):
    body = {"kind": "lesson", "parameters": {"topic": "networks", "age": 12}}
    headers = {"Idempotency-Key": "same-request-123"}
    first = client.post("/api/jobs", json=body, headers=headers)
    assert first.status_code == 202 and first.json()["status"] == "queued"
    assert "lesson_request" not in client.seen
    assert client.post("/api/jobs", json=body, headers=headers).json() == first.json()
    body["parameters"]["topic"] = "gravity"
    assert client.post("/api/jobs", json=body, headers=headers).status_code == 409
    assert (
        client.get(f"/api/jobs/{first.json()['id']}", headers=child_headers(client)).status_code
        == 404
    )
    assert (
        client.post(
            "/api/jobs",
            json={"kind": "quiz", "resource_id": "other-user-id"},
            headers={"Idempotency-Key": "missing-source"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/jobs", json={"kind": "lesson", "parameters": {"age": 0}}, headers=headers
        ).status_code
        == 422
    )


def test_durable_worker_executes_job_and_survives_new_repository_instance(client, repository):
    if not repository.durable:
        pytest.skip("Worker requires PostgreSQL")
    created = client.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "networks", "age": 12}},
        headers={"Idempotency-Key": "worker-success"},
    ).json()
    assert work_once(repository, client.app.state.settings)
    from app.repository import PostgresRepository

    client.app.state.repository = PostgresRepository(repository.url)
    job = client.get(f"/api/jobs/{created['id']}").json()
    assert job["status"] == "succeeded" and job["attempts"] == 1
    assert client.get(f"/api/lessons/{job['result']['resource_id']}").status_code == 200
    assert not work_once(repository, client.app.state.settings)


def test_generation_disabled_and_attempt_budget_even_on_failure(client, monkeypatch):
    import learning_service.routes as routes

    settings = client.app.state.settings
    settings.generation_daily_limit = 2

    def fail(_request):
        raise RuntimeError("private provider credential")

    monkeypatch.setattr(routes, "make_lesson", fail)
    for _ in range(2):
        response = client.post("/api/lessons", json={"topic": "networks", "age": 12})
        assert response.status_code == 502 and "credential" not in response.text
    assert client.post("/api/lessons", json={"topic": "networks", "age": 12}).status_code == 429
    settings.generation_enabled = False
    assert (
        client.post(
            "/api/jobs",
            json={"kind": "lesson", "parameters": {"topic": "x", "age": 12}},
            headers={"Idempotency-Key": "disabled-job"},
        ).status_code
        == 503
    )


def test_application_data_delete_leaves_rooms_and_preserves_other_account(client, lesson_id):
    client.put("/api/me/profile", json={"display_name": "Alex"})
    headers = child_headers(client)
    client.put("/api/me/profile", headers=headers, json={"display_name": "Sam"})
    room = client.post("/api/rooms", json={"display_name": "Alex"}).json()
    client.post(
        "/api/rooms/join", headers=headers, json={"code": room["code"], "display_name": "Sam"}
    )
    assert client.delete("/api/me/data").status_code == 204
    assert client.get(f"/api/lessons/{lesson_id}").status_code == 404
    assert client.get("/api/me/profile").json()["display_name"] == "Learner"
    assert client.get("/api/me/profile", headers=headers).json()["display_name"] == "Sam"
    snapshot = client.get(f"/api/rooms/{room['id']}", headers=headers).json()
    assert len(snapshot["members"]) == 1 and snapshot["host_id"] != PARENT


def test_request_limit_is_per_account_and_counts_failed_requests(client):
    client.app.state.settings.api_requests_per_minute = 1
    assert client.get("/api/lessons/missing").status_code == 404
    assert client.get("/api/me/profile").status_code == 429
    assert client.get("/api/me/profile", headers=child_headers(client)).status_code == 200


def test_actual_request_body_size_is_bounded(client):
    response = client.put(
        "/api/me/profile",
        content=b"x" * (4 * 2**20 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_worker_retries_are_bounded_and_errors_are_redacted(client, repository, monkeypatch):
    if not repository.durable:
        pytest.skip("Worker requires PostgreSQL")
    from learning_service import routes

    def fail(_body):
        raise RuntimeError("sensitive-provider-error")

    monkeypatch.setattr(routes, "make_lesson", fail)
    job = client.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "networks", "age": 12}},
        headers={"Idempotency-Key": "retry-boundary"},
    ).json()
    settings = client.app.state.settings
    assert work_once(repository, settings)
    assert work_once(repository, settings)
    assert not work_once(repository, settings)
    response = client.get(f"/api/jobs/{job['id']}")
    assert response.json()["status"] == "failed" and response.json()["attempts"] == 2
    assert "sensitive-provider-error" not in response.text


def test_cancelled_job_is_not_executed(client, repository):
    job = client.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "networks", "age": 12}},
        headers={"Idempotency-Key": "cancel-this-job"},
    ).json()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "cancelled"
    if repository.durable:
        assert not work_once(repository, client.app.state.settings)


def test_only_one_worker_can_claim_a_job_and_expired_lease_can_resume(client, repository):
    if not repository.durable:
        pytest.skip("Worker requires PostgreSQL")
    import psycopg

    job = client.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "networks", "age": 12}},
        headers={"Idempotency-Key": "lease-resumption"},
    ).json()
    with psycopg.connect(repository.url) as connection:
        first = connection.execute("select api_private.claim_job('dead-worker',2,2)").fetchone()[0]
        assert first["job"]["attempts"] == 1
        assert (
            connection.execute("select api_private.claim_job('other-worker',2,2)").fetchone()[0]
            is None
        )
        connection.execute(
            "update api_private.records set data=jsonb_set(data,'{lease_until}',to_jsonb((now()-interval '1 second')::text)) where kind='jobs'"
        )
    assert work_once(repository, client.app.state.settings)
    resumed = client.get(f"/api/jobs/{job['id']}").json()
    assert resumed["status"] == "succeeded" and resumed["attempts"] == 2


def test_closed_rooms_reject_readiness_but_can_be_left(client):
    room = client.post("/api/rooms", json={"display_name": "Alex"}).json()
    closed = client.post(
        f"/api/rooms/{room['id']}/commands", json={"action": "close", "expected_version": 1}
    ).json()
    assert closed["closed"] and closed["version"] == 2
    assert (
        client.post(
            "/api/rooms/join",
            headers=child_headers(client),
            json={"code": room["code"], "display_name": "Sam"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/rooms/{room['id']}/commands",
            json={"action": "ready", "expected_version": 2, "ready": True},
        ).status_code
        == 409
    )
    assert client.post(
        f"/api/rooms/{room['id']}/commands", json={"action": "leave", "expected_version": 2}
    ).json() == {"left": True}
    assert client.get(f"/api/rooms/{room['id']}").status_code == 404


def test_api_schema_declares_bearer_auth_for_every_data_operation(client):
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        if path.startswith("/api/"):
            for operation in operations.values():
                assert operation.get("security"), path


def test_ready_reports_adapter_durability(client, repository):
    assert client.get("/ready").json() == {"ready": True, "durable": repository.durable}


def test_flashcard_retries_do_not_duplicate_reviews_with_idempotency_key(client, lesson_id):
    deck = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}).json()
    body = {"card": 0, "result": "again", "expected_reviews": 0}
    header = {"Idempotency-Key": "first-card-review"}
    first = client.post(f"/api/flashcards/{deck['id']}/review", json=body, headers=header)
    assert first.status_code == 200 and first.json()["reviews"] == 1
    assert (
        client.post(f"/api/flashcards/{deck['id']}/review", json=body, headers=header).json()
        == first.json()
    )
    assert client.get(f"/api/flashcards/{deck['id']}").json()["state"]["reviews"] == 1
    assert client.post(f"/api/flashcards/{deck['id']}/review", json=body).status_code == 409
    body["result"] = "know"
    assert (
        client.post(f"/api/flashcards/{deck['id']}/review", json=body, headers=header).status_code
        == 409
    )


def test_material_revisions_keep_old_lesson_provenance(client):
    mid = book(client)["id"]
    lesson = lesson_from(client, mid).json()
    first = client.get(f"/api/materials/{mid}/revisions/1").json()
    assert lesson["source"]["material_revision"] == 1
    changed = {
        "expected_revision": 1,
        "pages": [{"number": 1, "text": "A different source about gravity.", "via": "text"}],
    }
    assert client.put(f"/api/materials/{mid}/pages", json=changed).status_code == 200
    assert client.get(f"/api/materials/{mid}/revisions/1").json() == first
    assert (
        client.get(f"/api/materials/{mid}/revisions/2").json()["pages"][0]["text"]
        == changed["pages"][0]["text"]
    )
    assert client.get(f"/api/lessons/{lesson['id']}").json()["source"]["material_revision"] == 1
    assert (
        client.get(f"/api/materials/{mid}/revisions/1", headers=child_headers(client)).status_code
        == 404
    )


def test_queued_job_rejects_changed_material_before_model_call(client, repository):
    if not repository.durable:
        pytest.skip("Worker requires PostgreSQL")
    mid = book(client)["id"]
    job = client.post(
        "/api/jobs",
        json={
            "kind": "lesson",
            "parameters": {"topic": "photosynthesis", "age": 12, "material_id": mid},
        },
        headers={"Idempotency-Key": "source-revision-job"},
    ).json()
    assert job["source_revision"] == 1
    client.put(
        f"/api/materials/{mid}/pages",
        json={
            "expected_revision": 1,
            "pages": [{"number": 1, "text": "Changed source.", "via": "text"}],
        },
    )
    client.app.state.settings.job_attempt_limit = 1
    assert work_once(repository, client.app.state.settings)
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"
    assert "grounded_request" not in client.seen
    assert client.get("/api/resources/lessons").json()["items"] == []
