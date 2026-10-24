"""Durable generation jobs: validation, idempotency, queue limits, cancellation, and every kind through the worker."""

import re

import pytest

from app.worker import work_once
from tests.e2e.helpers import (
    approval,
    bug_on_postgres,
    child_headers,
    create_quiz,
    keys_anywhere,
    upload,
)
from tests.learning.files import make_pdf


def post_job(api, kind, key, resource_id=None, parameters=None, headers=None, **extra):
    body = {
        "kind": kind,
        **({"resource_id": resource_id} if resource_id else {}),
        "parameters": parameters or {},
        **extra,
    }
    return api.post("/api/jobs", json=body, headers={"Idempotency-Key": key, **(headers or {})})


def lesson_job(api, key="lesson-job-0001", topic="networks", **params):
    return post_job(api, "lesson", key, parameters={"topic": topic, "age": 12, **params})


# ---------------------------------------------------------------- what a job accepts
@pytest.mark.parametrize(
    "build,status",
    [
        (lambda api, L: lesson_job(api), 202),
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-1", L, {"num_questions": 15}), 202),
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-2", L, {"num_questions": 16}), 422),
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-3", L, {"num_questions": 0}), 422),
        (lambda api, L: post_job(api, "flashcards", "job-key-cards-1", L, {"num_cards": 20}), 202),
        (lambda api, L: post_job(api, "flashcards", "job-key-cards-2", L, {"num_cards": 2}), 422),
        (lambda api, L: post_job(api, "visuals", "job-key-vis-1", L, {"ask": "x" * 300}), 202),
        (lambda api, L: post_job(api, "visuals", "job-key-vis-2", L, {"ask": "x" * 301}), 422),
        (
            lambda api, L: post_job(api, "followup", "job-key-fol-1", L),
            404,
        ),  # a lesson id is not a quiz id
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-4"), 404),  # source is required
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-5", "missing"), 404),
        (lambda api, L: post_job(api, "quiz", "job-key-quiz-6", "x" * 129, {}), 422),
        (
            lambda api, L: post_job(api, "lesson", "job-key-less-1", L, {"topic": "x", "age": 12}),
            422,
        ),  # resource_id forbidden
        (lambda api, L: lesson_job(api, "job-key-less-2", age=4), 422),
        (lambda api, L: lesson_job(api, "job-key-less-3", topic=""), 422),
        (lambda api, L: lesson_job(api, "job-key-less-4", model="expensive-model"), 422),
        (lambda api, L: lesson_job(api, "job-key-less-5", material_id="nope"), 404),
        (lambda api, L: post_job(api, "bogus", "job-key-bogus-1"), 422),
        (
            lambda api, L: api.post(
                "/api/jobs",
                json={"kind": "lesson", "parameters": ["x"]},
                headers={"Idempotency-Key": "job-key-less-6"},
            ),
            422,
        ),
        (
            lambda api, L: post_job(
                api, "lesson", "job-key-less-7", parameters={"topic": "x", "age": 12}, priority=1
            ),
            422,
        ),
        (
            lambda api, L: post_job(
                api,
                "lesson",
                "job-key-less-8",
                parameters={"topic": "x", "age": 12},
                status="succeeded",
            ),
            422,
        ),
    ],
    ids=[
        "lesson",
        "quiz-15",
        "quiz-16",
        "quiz-0",
        "cards-20",
        "cards-2",
        "visuals-300",
        "visuals-301",
        "followup-with-lesson-id",
        "quiz-no-source",
        "quiz-missing-source",
        "resource-id-129",
        "lesson-with-resource-id",
        "age-4",
        "empty-topic",
        "unknown-parameter",
        "missing-material",
        "bogus-kind",
        "parameters-list",
        "extra-body-field",
        "forged-status",
    ],
)
def test_job_validation_matrix(api, lesson_id, build, status):
    response = build(api, lesson_id)
    assert response.status_code == status, response.text
    jobs = api.get("/api/resources/jobs").json()["items"]
    assert len(jobs) == (1 if status == 202 else 0)


@pytest.mark.parametrize(
    "key,ok",
    [
        ("12345678", True),
        ("k" * 128, True),
        ("1234567", False),
        ("k" * 129, False),
        ("", False),
        (None, False),
    ],
)
def test_idempotency_key_length_is_enforced(api, key, ok):
    headers = {"Idempotency-Key": key} if key is not None else {}
    r = api.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "x", "age": 12}},
        headers=headers,
    )
    assert (r.status_code == 202) is ok, r.text


def test_submitting_never_calls_a_model_or_reads_the_source_text(api, lesson_id):
    before = {
        key: list(value) if isinstance(value, list) else value for key, value in api.seen.items()
    }
    post_job(api, "quiz", "no-model-call-1", lesson_id, {"num_questions": 3})
    lesson_job(api, "no-model-call-2")
    assert set(api.seen) == set(before)  # no quiz/cards/visual/lesson request appeared
    assert (
        api.seen["safety_checks"] == before["safety_checks"]
        and api.seen["lesson_request"] == before["lesson_request"]
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG-08: a job for an unconfirmed material is accepted and then burns generation attempts",
)
def test_a_job_for_unconfirmed_material_is_refused_up_front(api):
    mid = upload(api, ("b.pdf", make_pdf(), "application/pdf")).json()["id"]
    assert lesson_job(api, "unconfirmed-0001", material_id=mid).status_code == 409


# ---------------------------------------------------------------- idempotency
def test_a_retry_returns_the_same_job_and_a_changed_request_is_a_conflict(api):
    first = lesson_job(api, "retry-key-0001").json()
    assert lesson_job(api, "retry-key-0001").json() == first
    assert lesson_job(api, "retry-key-0001", topic="something else").status_code == 409
    assert lesson_job(api, "retry-key-0001", age=13).status_code == 409
    assert len(api.get("/api/resources/jobs").json()["items"]) == 1


def test_equivalent_requests_share_one_job(api):
    a = lesson_job(api, "equivalent-key-1", topic="  networks  ").json()
    b = lesson_job(api, "equivalent-key-1", topic="networks", prefers_visuals=False).json()
    assert a == b


def test_keys_are_case_sensitive_and_never_shared_between_accounts(api):
    parent = lesson_job(api, "Shared-Key-0001").json()
    assert lesson_job(api, "shared-key-0001").json()["id"] != parent["id"]
    child = post_job(
        api,
        "lesson",
        "Shared-Key-0001",
        parameters={"topic": "networks", "age": 12},
        headers=child_headers(api),
    ).json()
    assert child["id"] != parent["id"]
    assert api.get(f"/api/jobs/{parent['id']}", headers=child_headers(api)).status_code == 404
    assert api.get(f"/api/jobs/{child['id']}").status_code == 404


def test_job_ids_are_opaque_hashes_that_do_not_reveal_the_key(api):
    job = lesson_job(api, "secret-key-value-1").json()
    assert re.fullmatch(r"[0-9a-f]{64}", job["id"]) and "secret-key-value" not in job["id"]


def test_concurrent_identical_submissions_create_one_job(api):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: lesson_job(api, "race-key-job-001").status_code, range(6)))
    assert 202 in codes and set(codes) <= {202, 409}
    assert len(api.get("/api/resources/jobs").json()["items"]) == 1


# ---------------------------------------------------------------- queue limits and cancellation
def test_ten_pending_jobs_is_the_ceiling_and_cancelling_frees_a_slot(api):
    ids = [lesson_job(api, f"pending-key-{n:04d}", topic=f"t{n}").json()["id"] for n in range(10)]
    assert lesson_job(api, "pending-key-0010", topic="t10").status_code == 429
    assert (
        lesson_job(api, "pending-key-0003", topic="t3").status_code == 202
    )  # replaying an existing job is free
    assert lesson_job(api, "pending-key-0003", topic="changed").status_code == 409
    assert (
        post_job(
            api,
            "lesson",
            "other-account-0001",
            parameters={"topic": "x", "age": 12},
            headers=child_headers(api),
        ).status_code
        == 202
    )
    assert api.delete(f"/api/jobs/{ids[0]}").status_code == 204
    assert lesson_job(api, "pending-key-0011", topic="t11").status_code == 202


def test_cancellation_rules(api):
    job = lesson_job(api, "cancel-key-0001").json()
    other = child_headers(api)
    assert api.delete(f"/api/jobs/{job['id']}", headers=other).status_code == 404
    assert api.get(f"/api/jobs/{job['id']}").json()["status"] == "queued"
    assert api.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert api.delete(f"/api/jobs/{job['id']}").status_code == 204  # repeating is safe
    assert api.get(f"/api/jobs/{job['id']}").json()["status"] == "cancelled"
    assert (
        lesson_job(api, "cancel-key-0001").json()["status"] == "cancelled"
    )  # the same key still means the same job
    assert api.delete("/api/jobs/nope").status_code == 404


def test_a_job_that_is_not_queued_cannot_be_cancelled(api, repository):
    job = lesson_job(api, "running-key-0001").json()
    with repository.session("00000000-0000-4000-8000-000000000001") as unit:
        record = unit.get("jobs", job["id"])
        record["status"] = "running"
        unit.put("jobs", job["id"], record)
    r = api.delete(f"/api/jobs/{job['id']}")
    assert r.status_code == 409 and "queued" in r.json()["detail"]


def test_generation_switched_off_refuses_new_jobs_but_keeps_existing_ones_readable(api):
    job = lesson_job(api, "before-disable-01").json()
    api.app.state.settings.generation_enabled = False
    assert lesson_job(api, "after-disable-001").status_code == 503
    assert (
        lesson_job(api, "before-disable-01").status_code == 503
    )  # not even a replay: the gate comes first
    assert api.get(f"/api/jobs/{job['id']}").status_code == 200
    assert api.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert len(api.get("/api/resources/jobs").json()["items"]) == 1


def test_a_job_describes_itself_without_leaking_inputs_or_credentials(api, lesson_id):
    job = lesson_job(api, "shape-key-000001", topic="PRIVATE-TOPIC-TEXT").json()
    allowed = {
        "id",
        "kind",
        "status",
        "attempts",
        "created_at",
        "result",
        "error",
        "lease_until",
        "source_id",
        "source_kind",
        "source_revision",
    }
    assert set(job) <= allowed
    for text in (
        api.get(f"/api/jobs/{job['id']}").text,
        str(api.get("/api/resources/jobs").json()),
    ):
        assert "PRIVATE-TOPIC-TEXT" not in text
    assert not {"input", "digest", "lease_token", "parameters"} & keys_anywhere(job)
    quiz_job = post_job(api, "quiz", "shape-key-000002", lesson_id, {}).json()
    assert (
        quiz_job["source_kind"] == "lessons"
        and quiz_job["source_id"] == lesson_id
        and quiz_job["source_revision"] == 1
    )


# ---------------------------------------------------------------- the worker (needs PostgreSQL)
def run_worker(api, repository):
    ran = 0
    while work_once(repository, api.app.state.settings):
        ran += 1
        assert ran < 20  # a runaway worker would be a bug, not a hang
    return ran


def test_every_job_kind_completes_through_the_worker_and_produces_owned_results(
    api, repository, lesson_id
):
    if not repository.durable:
        pytest.skip("The worker requires PostgreSQL")
    quiz = create_quiz(api, lesson_id)
    api.post(
        f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0}
    )  # a mistake, so a follow-up exists
    jobs = {
        "lesson": lesson_job(api, "worker-kind-lesson").json(),
        "quiz": post_job(api, "quiz", "worker-kind-quiz1", lesson_id, {"num_questions": 3}).json(),
        "flashcards": post_job(
            api, "flashcards", "worker-kind-cards", lesson_id, {"num_cards": 4}
        ).json(),
        "visuals": post_job(api, "visuals", "worker-kind-visua", lesson_id, {}).json(),
        "followup": post_job(api, "followup", "worker-kind-follow", quiz["id"], {}).json(),
    }
    assert run_worker(api, repository) == 5
    done = {kind: api.get(f"/api/jobs/{job['id']}").json() for kind, job in jobs.items()}
    assert {kind: job["status"] for kind, job in done.items()} == dict.fromkeys(jobs, "succeeded")
    assert all(job["attempts"] == 1 and job["error"] is None for job in done.values())
    assert api.get(f"/api/lessons/{done['lesson']['result']['resource_id']}").status_code == 200
    assert api.get(f"/api/quiz/{done['quiz']['result']['resource_id']}").status_code == 200
    assert (
        api.get(f"/api/flashcards/{done['flashcards']['result']['resource_id']}").status_code == 200
    )
    assert done["visuals"]["result"]["visual_ids"]
    follow = api.get(f"/api/quiz/{done['followup']['result']['resource_id']}").json()
    assert follow["parent_id"] == quiz["id"]
    assert (
        api.get(
            f"/api/quiz/{done['quiz']['result']['resource_id']}", headers=child_headers(api)
        ).status_code
        == 404
    )
    assert not work_once(repository, api.app.state.settings)  # nothing left to claim


def test_a_worker_run_cannot_outspend_the_daily_budget_or_leak_why(api, repository):
    if not repository.durable:
        pytest.skip("The worker requires PostgreSQL")
    settings = api.app.state.settings
    settings.generation_daily_limit, settings.job_attempt_limit = 1, 2
    first, second = (
        lesson_job(api, "budget-job-00001", topic="a").json(),
        lesson_job(api, "budget-job-00002", topic="b").json(),
    )
    run_worker(api, repository)
    outcomes = sorted(api.get(f"/api/jobs/{j['id']}").json()["status"] for j in (first, second))
    assert outcomes == ["failed", "succeeded"]
    failed = next(
        j for j in (first, second) if api.get(f"/api/jobs/{j['id']}").json()["status"] == "failed"
    )
    body = api.get(f"/api/jobs/{failed['id']}").json()
    assert body["attempts"] == 2 and "limit" not in (body["error"] or "").lower()
    assert (
        api.post("/api/lessons", json={"topic": "x", "age": 12}).status_code == 429
    )  # the budget really is spent


def test_a_deleted_source_stops_its_queued_jobs(api, repository, lesson_id):
    if not repository.durable:
        pytest.skip("The worker requires PostgreSQL")
    mid = upload(api, ("b.pdf", make_pdf(), "application/pdf")).json()["id"]
    api.post(f"/api/materials/{mid}/confirm", json=approval())
    job = lesson_job(api, "orphan-job-00001", topic="chapter 1", material_id=mid).json()
    api.delete(f"/api/materials/{mid}")
    assert api.get(f"/api/jobs/{job['id']}").status_code == 404
    assert not work_once(repository, api.app.state.settings)


def test_nul_in_job_parameters_is_not_a_server_error(api, repository, request):
    bug_on_postgres(
        request, repository, "BUG-01: a NUL byte in user text/ids becomes a 503 on PostgreSQL"
    )
    r = lesson_job(api, "nul-parameter-001", topic="a\u0000b")
    assert r.status_code < 500
