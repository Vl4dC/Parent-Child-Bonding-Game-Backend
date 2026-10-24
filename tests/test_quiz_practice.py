"""Exercise quiz lifecycle with seeded content and generation disabled throughout."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository import PostgresRepository
from learning_service.lesson import Lesson, LessonRequest, LessonSection, LessonSource
from learning_service.materials import Material, Page
from learning_service.store import StoredLesson, StoredQuiz
from learning_service.visual_recall import IllustratedQuestion
from learning_service.visuals.models import Visual

PARENT = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def practice(repository, monkeypatch):
    import learning_service.quiz_graph as graph
    import learning_service.safety as safety
    import learning_service.store as store

    def no_ai(*args, **kwargs):
        pytest.fail("A deterministic quiz endpoint called AI/generation")

    for name in ("make_quiz", "make_remedial", "make_cards", "make_blank_items", "ensure_safe"):
        monkeypatch.setattr(store, name, no_ai)
    monkeypatch.setattr(safety, "ensure_safe", no_ai)
    monkeypatch.setattr(graph, "_llm", no_ai)
    material = Material(
        id="material-fixture",
        name="reviewed-maths.pdf",
        kind="pdf",
        confirmed=True,
        pages=[Page(number=1, text="One plus one is two. Two is even.", via="text")],
    )
    lesson = StoredLesson(
        id="lesson-fixture",
        request=LessonRequest(topic="Arithmetic", age=12),
        lesson=Lesson(
            title="Arithmetic",
            summary="A reviewed sample.",
            sections=[LessonSection(heading="Numbers", body="One plus one is two.")],
            key_points=["Two is even"],
        ),
        source=LessonSource(
            material_id=material.id, title=material.name, pages=[1], label="reviewed page 1"
        ),
        status="approved",
    )
    questions = [
        IllustratedQuestion(
            index=0,
            type="multiple_choice",
            question="What is one plus one?",
            options=["One", "Two", "Three", "Four"],
            answer="Two",
            explanation="1 + 1 = 2.",
        ),
        IllustratedQuestion(
            index=1,
            type="true_false",
            question="Two is even.",
            answer="True",
            explanation="Two is divisible by two.",
        ),
        IllustratedQuestion(
            index=2,
            type="multiple_choice",
            question="What is two plus one?",
            options=["One", "Two", "Three", "Four"],
            answer="Three",
            explanation="2 + 1 = 3.",
        ),
    ]
    quiz = StoredQuiz(
        id="quiz-fixture",
        lesson_id=lesson.id,
        title="Arithmetic practice",
        dropped=0,
        questions=questions,
    )
    with repository.session(PARENT) as unit:
        unit.put("materials", material.id, material)
        unit.put("lessons", lesson.id, lesson)
        unit.put("quizzes", quiz.id, quiz)
    application = create_app(
        Settings(app_env="test", demo_mode=True, generation_enabled=False, _env_file=None),
        repository=repository,
    )
    with TestClient(application) as client:
        client.headers["X-Player-ID"] = "00000000-0000-4000-8000-000000000001"
        yield client, repository, quiz


def answer(client, qid, index, choice, count, key=None):
    return client.post(
        f"/api/quiz/{qid}/answers",
        json={"index": index, "choice": choice, "expected_answered": count},
        headers={"Idempotency-Key": key or f"answer-receipt-{index}"},
    )


def complete(client, qid):
    for i, choice in enumerate([1, 1, 2]):
        response = answer(client, qid, i, choice, i)
        assert response.status_code == 200, response.text
    result = client.post(f"/api/quiz/{qid}/finish", json={"expected_answered": 3})
    assert result.status_code == 200, result.text
    return result.json()


def test_resume_results_and_review_without_ai(practice):
    client, _, quiz = practice
    qid = quiz.id
    initial = client.get(f"/api/quiz/{qid}/progress")
    assert initial.status_code == 200
    assert initial.json()["status"] == "not_started"
    assert initial.json()["next_index"] == 0 and initial.json()["answers"] == []
    assert initial.headers["cache-control"] == "no-store"
    assert client.get(f"/api/quiz/{qid}/review").status_code == 409
    first = answer(client, qid, 0, 1, 0).json()
    assert first["feedback"]["correct"] and first["progress"]["answered"] == 1
    summary = client.get(f"/api/quiz/{qid}/results").json()
    assert summary["score"] == 1 and summary["percentage"] == 33
    assert summary["accuracy_on_answered"] == 100
    assert summary["result_kind"] == "private_formative_practice"
    assert "answer_index" not in json.dumps(client.get(f"/api/quiz/{qid}/progress").json())
    assert "explanation" not in json.dumps(summary)
    assert answer(client, qid, 1, 1, 1).status_code == 200
    assert answer(client, qid, 2, 2, 2).status_code == 200
    assert client.get(f"/api/quiz/{qid}/progress").json()["status"] == "ready_to_finish"
    finished = client.post(f"/api/quiz/{qid}/finish", json={"expected_answered": 3}).json()
    assert finished["status"] == "completed" and finished["score"] == 2
    assert finished["total"] == 3 and finished["percentage"] == 67
    review = client.get(f"/api/quiz/{qid}/review").json()
    assert review["questions"][1]["feedback"]["answer_index"] == 0
    assert review["lesson_id"] == quiz.lesson_id
    assert client.get("/api/me/progress").json()["quiz_answers"] == 3
    assert client.get("/api/me/progress").json()["correct_answers"] == 2


def test_answer_retries_conflicts_and_first_choice(practice):
    client, _, quiz = practice
    qid = quiz.id
    first = answer(client, qid, 0, 0, 0, key="repeat-answer-key")
    assert first.status_code == 200
    assert answer(client, qid, 0, 0, 0, key="repeat-answer-key").json() == first.json()
    assert answer(client, qid, 0, 1, 0, key="repeat-answer-key").status_code == 409
    assert answer(client, qid, 0, 1, 1, key="different-answer-key").status_code == 409
    assert answer(client, qid, 1, 0, 0).status_code == 409
    assert client.post(f"/api/quiz/{qid}/check", json={"index": 0, "choice": 1}).json()["correct"]
    # Legacy feedback can be repeated, but the authoritative first answer never changes.
    state = client.get(f"/api/quiz/{qid}/progress").json()
    assert state["answers"] == [{"index": 0, "choice": 0, "correct": False}]
    assert state["score"] == 0 and state["answered"] == 1


def test_finish_is_idempotent_and_requires_all_answers(practice):
    client, _, quiz = practice
    assert (
        client.post(f"/api/quiz/{quiz.id}/finish", json={"expected_answered": 0}).status_code == 409
    )
    done = complete(client, quiz.id)
    assert client.post(f"/api/quiz/{quiz.id}/finish", json={"expected_answered": 3}).json() == done
    assert (
        client.post(f"/api/quiz/{quiz.id}/finish", json={"expected_answered": 2}).status_code == 409
    )
    assert answer(client, quiz.id, 0, 1, 3, key="new-after-finish").status_code == 409
    # The exact original receipt remains replayable after completion without changing results.
    assert answer(client, quiz.id, 0, 1, 0).status_code == 200
    assert client.get(f"/api/quiz/{quiz.id}/results").json() == done


def test_retry_all_and_missed_are_independent_and_idempotent(practice):
    client, repository, quiz = practice
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/retry",
            json={"which": "missed"},
            headers={"Idempotency-Key": "retry-missed-early"},
        ).status_code
        == 409
    )
    complete(client, quiz.id)
    first = client.post(
        f"/api/quiz/{quiz.id}/retry",
        json={"which": "all"},
        headers={"Idempotency-Key": "retry-all-request"},
    )
    assert first.status_code == 201
    again = client.post(
        f"/api/quiz/{quiz.id}/retry",
        json={"which": "all"},
        headers={"Idempotency-Key": "retry-all-request"},
    )
    assert again.json() == first.json()
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/retry",
            json={"which": "missed"},
            headers={"Idempotency-Key": "retry-all-request"},
        ).status_code
        == 409
    )
    missed = client.post(
        f"/api/quiz/{quiz.id}/retry",
        json={"which": "missed"},
        headers={"Idempotency-Key": "retry-mistakes-request"},
    ).json()
    assert len(missed["questions"]) == 1 and missed["questions"][0]["index"] == 0
    assert missed["questions"][0]["question"] == "Two is even."
    assert "answer_index" not in json.dumps(missed)
    a = client.get(f"/api/quiz/{first.json()['id']}/progress").json()
    b = client.get(f"/api/quiz/{missed['id']}/progress").json()
    assert a["answered"] == b["answered"] == 0
    assert a["completed_at"] is b["completed_at"] is None
    assert a["question_set_id"] == quiz.question_set_id
    assert b["question_set_id"] != quiz.question_set_id
    with repository.session(PARENT) as unit:
        assert len(unit.list("question_sets")) == 2
    assert answer(client, missed["id"], 0, 0, 0).status_code == 200
    assert client.get(f"/api/quiz/{quiz.id}/results").json()["score"] == 2


def test_history_pagination_only_same_snapshot_and_owner(practice):
    client, _, quiz = practice
    for i in range(2):
        client.post(
            f"/api/quiz/{quiz.id}/retry",
            json={},
            headers={"Idempotency-Key": f"history-retry-key-{i}"},
        )
    items = []
    after = ""
    while True:
        response = client.get(f"/api/quiz/{quiz.id}/attempts", params={"limit": 1, "after": after})
        assert response.status_code == 200, response.text
        page = response.json()
        items += page["items"]
        if page["next_cursor"] is None:
            break
        after = page["next_cursor"]
    assert len(items) == 3 and len({item["id"] for item in items}) == 3
    assert all("answers" not in item and "questions" not in item for item in items)
    headers = {"X-Player-ID": "00000000-0000-4000-8000-000000000002"}
    for path in ("progress", "results", "review", "attempts"):
        assert client.get(f"/api/quiz/{quiz.id}/{path}", headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/answers",
            json={"index": 0, "choice": 0, "expected_answered": 0},
            headers=headers | {"Idempotency-Key": "other-user-key"},
        ).status_code
        == 404
    )
    client.headers.pop("X-Player-ID")
    assert client.get(f"/api/quiz/{quiz.id}/results").status_code == 200


def test_strict_validation_and_cross_origin_idempotency(practice):
    client, _, quiz = practice
    url = f"/api/quiz/{quiz.id}/answers"
    for body in (
        {"index": True, "choice": 0, "expected_answered": 0},
        {"index": 0, "choice": "1", "expected_answered": 0},
        {"index": -1, "choice": 0, "expected_answered": 0},
        {"index": 0, "choice": 99, "expected_answered": 0},
        {"index": 0, "choice": 1, "expected_answered": 0, "score": 999},
    ):
        assert (
            client.post(
                url, json=body, headers={"Idempotency-Key": "invalid-answer-key"}
            ).status_code
            == 422
        )
    assert client.get(f"/api/quiz/{quiz.id}/progress").json()["answered"] == 0
    response = client.options(
        url,
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert "access-control-allow-credentials" not in response.headers


def test_concurrent_answers_have_one_winner(practice):
    client, _, quiz = practice
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda i: answer(client, quiz.id, i, 0, 0), [0, 1]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert client.get(f"/api/quiz/{quiz.id}/progress").json()["answered"] == 1


def test_concurrent_retry_has_no_orphan_attempt(practice):
    client, repository, quiz = practice

    def retry(_):
        return client.post(
            f"/api/quiz/{quiz.id}/retry",
            json={},
            headers={"Idempotency-Key": "simultaneous-retry-key"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(retry, range(2)))
    assert all(r.status_code in (201, 409) for r in responses)
    retried = retry(0)
    assert retried.status_code == 201
    ids = {r.json()["id"] for r in [*responses, retried] if r.status_code == 201}
    assert len(ids) == 1
    with repository.session(PARENT) as unit:
        assert len(unit.list("quizzes")) == 2


def test_cascade_deletion_removes_results_retry_and_keys(practice):
    client, repository, quiz = practice
    complete(client, quiz.id)
    other = client.post(
        f"/api/quiz/{quiz.id}/retry",
        json={"which": "missed"},
        headers={"Idempotency-Key": "deletion-retry-key"},
    ).json()
    assert client.delete("/api/materials/material-fixture").status_code == 204
    for qid in (quiz.id, other["id"]):
        assert client.get(f"/api/quiz/{qid}/results").status_code == 404
    with repository.session(PARENT) as unit:
        assert unit.list("quizzes") == [] and unit.list("question_sets") == []


def test_account_data_deletion_removes_lifecycle_metadata(practice):
    client, repository, quiz = practice
    complete(client, quiz.id)
    assert client.delete("/api/me/data").status_code == 204
    with repository.session(PARENT) as unit:
        assert unit.list("quizzes") == [] and unit.list("question_sets") == []


def test_diagram_feedback_receipt_is_stable(practice):
    client, repository, quiz = practice
    visual = Visual(
        kind="diagram",
        section_index=1,
        title="Numbers",
        caption="A number.",
        alt="Two.",
        code='flowchart LR\n a["Two"]',
    )
    with repository.session(PARENT) as unit:
        value = unit.get("quizzes", quiz.id, StoredQuiz)
        # Make a separate immutable snapshot before play; production producer is unchanged.
        value.question_set_id = "diagram-fixture-snapshot"
        value.questions[0].answer_visual = visual
    first = answer(client, quiz.id, 0, 1, 0)
    assert first.status_code == 200
    assert "Two" in first.json()["feedback"]["answer_visual_html"]
    assert answer(client, quiz.id, 0, 1, 0).json() == first.json()


def test_existing_new_attempt_and_check_still_work(practice):
    client, _, quiz = practice
    assert (
        client.post(f"/api/quiz/{quiz.id}/check", json={"index": 0, "choice": 1}).status_code == 200
    )
    fresh = client.post(f"/api/quiz/{quiz.id}/attempts").json()
    assert client.get(f"/api/quiz/{fresh['id']}/progress").json()["answered"] == 0
    assert client.get(f"/api/quiz/{quiz.id}/progress").json()["answered"] == 1
    # Existing model generation remains disabled; deterministic lifecycle routes did not enable it.
    assert client.post(f"/api/lessons/{quiz.lesson_id}/quiz", json={}).status_code == 503


def test_idempotent_review_cards_do_not_generate_or_duplicate(practice):
    client, repository, quiz = practice
    headers = {"Idempotency-Key": "review-deck-request"}
    path = f"/api/quiz/{quiz.id}/review-deck"
    assert client.post(path, json={}, headers=headers).status_code == 409
    complete(client, quiz.id)
    first = client.post(path, json={}, headers=headers)
    assert first.status_code == 201, first.text
    assert len(first.json()["cards"]) == 1
    assert first.json()["cards"][0]["back"].startswith("True")
    assert client.post(path, json={}, headers=headers).json() == first.json()
    assert client.post(path, json={"which": "all"}, headers=headers).status_code == 409
    with repository.session(PARENT) as unit:
        assert len(unit.list("decks")) == 1
    assert client.delete("/api/materials/material-fixture").status_code == 204
    with repository.session(PARENT) as unit:
        assert unit.list("decks") == []


def test_retry_missed_freezes_the_subset_at_creation(practice):
    client, _, quiz = practice
    assert answer(client, quiz.id, 0, 0, 0).status_code == 200
    url = f"/api/quiz/{quiz.id}/retry"
    headers = {"Idempotency-Key": "frozen-subset-key"}
    first = client.post(url, json={"which": "missed"}, headers=headers).json()
    assert len(first["questions"]) == 1
    assert answer(client, quiz.id, 1, 1, 1).status_code == 200
    assert client.post(url, json={"which": "missed"}, headers=headers).json() == first
    second = client.post(
        url, json={"which": "missed"}, headers={"Idempotency-Key": "different-subset-key"}
    ).json()
    assert len(second["questions"]) == 2


def test_receipt_cap_rejects_without_recording_an_answer(practice):
    client, repository, quiz = practice
    with repository.session(PARENT) as unit:
        value = unit.get("quizzes", quiz.id, StoredQuiz)
        value.submission = {
            "practice_v1": {
                "answers": {str(i): {} for i in range(1000)},
                "retries": {},
                "completion": None,
            }
        }
    assert answer(client, quiz.id, 0, 1, 0).status_code == 409
    assert client.get(f"/api/quiz/{quiz.id}/progress").json()["answered"] == 0


def test_rounding_matches_integer_ui_percentages(practice):
    client, repository, quiz = practice
    with repository.session(PARENT) as unit:
        value = unit.get("quizzes", quiz.id, StoredQuiz)
        value.question_set_id = "rounding-snapshot"
        value.questions = [
            quiz.questions[0].model_copy(deep=True, update={"index": i}) for i in range(8)
        ]
    assert answer(client, quiz.id, 0, 1, 0).status_code == 200
    result = client.get(f"/api/quiz/{quiz.id}/results").json()
    assert result["percentage"] == 13 and result["accuracy_on_answered"] == 100


def test_results_and_receipts_survive_postgres_restart(practice):
    client, repository, quiz = practice
    done = complete(client, quiz.id)
    if not repository.durable:
        pytest.skip("Repository restart requires PostgreSQL")
    restarted = PostgresRepository(repository.url)
    try:
        app = create_app(
            Settings(app_env="test", demo_mode=True, _env_file=None), repository=restarted
        )
        with TestClient(app) as other:
            other.headers["X-Player-ID"] = "00000000-0000-4000-8000-000000000001"
            assert other.get(f"/api/quiz/{quiz.id}/results").json() == done
            assert answer(other, quiz.id, 0, 1, 0).status_code == 200
            assert other.get(f"/api/quiz/{quiz.id}/review").status_code == 200
    finally:
        restarted.close()


def revision_body():
    return {
        "title": "Corrected arithmetic",
        "questions": [
            {
                "type": "multiple_choice",
                "question": "What is one plus one?",
                "options": ["Two", "Three", "Four", "Five"],
                "answer_index": 0,
                "explanation": "One and one make two.",
            }
        ],
    }


def test_author_revision_and_approval_preserve_old_attempt(practice):
    client, _, quiz = practice
    original = client.get(f"/api/quiz/{quiz.id}").json()
    finished = complete(client, quiz.id)
    review = client.get(f"/api/quiz/{quiz.id}/author-review").json()
    assert review["questions"][0]["answer_index"] == 1
    assert review["review_kind"] == "owner_confirmation"
    headers = {"Idempotency-Key": "author-revision-request"}
    revised = client.post(f"/api/quiz/{quiz.id}/revisions", json=revision_body(), headers=headers)
    assert revised.status_code == 201, revised.text
    fresh = revised.json()
    assert fresh["id"] != quiz.id and fresh["question_set_id"] != quiz.question_set_id
    assert fresh["revision"] == 2 and fresh["status"] == "draft"
    assert (
        client.post(f"/api/quiz/{quiz.id}/revisions", json=revision_body(), headers=headers).json()
        == fresh
    )
    assert client.get(f"/api/quiz/{quiz.id}").json() == original
    assert client.get(f"/api/quiz/{quiz.id}/results").json() == finished
    author = client.get(f"/api/quiz/{fresh['id']}/author-review").json()
    assert not author["approved"] and author["approval"] is None
    assert author["questions"][0]["answer_index"] == 0
    assert client.get(f"/api/quiz/{fresh['id']}/progress").json()["answered"] == 0
    path = f"/api/quiz/{fresh['id']}/approve"
    assert (
        client.post(
            path, json={"expected_question_set_id": quiz.question_set_id, "confirmed": True}
        ).status_code
        == 409
    )
    approved = client.post(
        path, json={"expected_question_set_id": fresh["question_set_id"], "confirmed": True}
    )
    assert approved.status_code == 200
    assert (
        client.post(
            path, json={"expected_question_set_id": fresh["question_set_id"], "confirmed": True}
        ).json()
        == approved.json()
    )
    assert approved.json()["approval"]["question_set_id"] == fresh["question_set_id"]


def test_revision_validation_and_owner_isolation(practice):
    client, _, quiz = practice
    body = revision_body()
    for fields in (
        {"options": ["Two", "two", "Three", "Four"]},
        {"answer_index": 99},
        {"question": " "},
        {"type": "true_false", "options": ["False", "True"]},
    ):
        invalid = body | {"questions": [body["questions"][0] | fields]}
        assert (
            client.post(
                f"/api/quiz/{quiz.id}/revisions",
                json=invalid,
                headers={"Idempotency-Key": "invalid-revision-key"},
            ).status_code
            == 422
        )
    headers = {"X-Player-ID": "00000000-0000-4000-8000-000000000002"}
    assert client.get(f"/api/quiz/{quiz.id}/author-review", headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/revisions",
            json=body,
            headers=headers | {"Idempotency-Key": "other-revision-key"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/approve",
            json={"expected_question_set_id": quiz.question_set_id, "confirmed": True},
            headers=headers,
        ).status_code
        == 404
    )


def test_missed_subset_does_not_inherit_snapshot_approval(practice):
    client, _, quiz = practice
    assert (
        client.post(
            f"/api/quiz/{quiz.id}/approve",
            json={"expected_question_set_id": quiz.question_set_id, "confirmed": True},
        ).status_code
        == 200
    )
    assert answer(client, quiz.id, 0, 0, 0).status_code == 200
    retry = client.post(
        f"/api/quiz/{quiz.id}/retry",
        json={"which": "missed"},
        headers={"Idempotency-Key": "subset-approval-key"},
    ).json()
    review = client.get(f"/api/quiz/{retry['id']}/author-review").json()
    assert review["question_set_id"] != quiz.question_set_id
    assert review["approved"] is False and review["approval"] is None
