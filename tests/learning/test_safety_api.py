"""Guardrails wired into the app: every place the model writes for a learner is checked, blocks are friendly,
failures never leak provider text, and the age rules are in every generation prompt."""

import pytest

import learning_service.flashcards as flashcards
import learning_service.lesson as lesson_module
import learning_service.quiz_graph as quiz_graph
import learning_service.routes as app_module
import learning_service.safety as safety
import learning_service.store as store
import learning_service.visual_recall as vr
import learning_service.weakness as weakness
from learning_service.lesson import LessonSection, SectionVisual
from learning_service.safety import AgeBlocked, Verdict
from tests.learning.conftest import FAKE_LESSON
from tests.learning.test_followup_api import answer, followup, make_quiz
from tests.learning.test_visual_recall import LEAF, visual
from tests.learning.test_visuals import request as visual_request
from tests.learning.test_weakness import request as remedial_request

PARENT = "00000000-0000-4000-8000-000000000001"  # the demo parent's id (app/demo.py)
OK = Verdict(ok=True)
BAD = Verdict(ok=False, category="violence", reason="graphic")
BLOCKED = AgeBlocked("violence").message


def stored(repository, kind):
    """Ids of what the demo parent has kept of one kind (lessons, quizzes, decks, tournaments)."""
    with repository.session(PARENT) as unit:
        return [key for key, _ in unit.list(kind)]


def scripted(monkeypatch, decide):
    """Replace the checker: `decide(n, kind)` gives the verdict for the n-th check (from 1). Returns the call log."""
    calls = []

    def fake_check(text, age, *, topic="", kind="content"):
        calls.append((kind, age, text))
        return decide(len(calls), kind)

    monkeypatch.setattr(safety, "check", fake_check)
    return calls


def always_block_content(n, kind):
    return OK if kind == "request" else BAD


def broken_checker(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider key sk-secret-123 rejected")

    monkeypatch.setattr(safety, "check", boom)


def assert_blocked(r):
    assert r.status_code == 422, r.text
    assert r.json() == {"detail": BLOCKED}


def assert_unavailable(r):
    assert r.status_code == 502, r.text
    assert "safety check" in r.json()["detail"] and "sk-secret-123" not in r.text


# ---------------------------------------------------------------- lessons
def test_a_lesson_that_fails_the_check_twice_is_a_friendly_422_and_nothing_is_stored(
    client, monkeypatch, repository
):
    calls = scripted(monkeypatch, always_block_content)
    r = client.post("/api/lessons", json={"topic": "volcanoes", "age": 12})
    assert_blocked(r)
    assert (
        "volcanoes" not in r.text and "graphic" not in r.text
    )  # the unsafe text is never repeated
    assert [c[0] for c in calls] == ["request", "content", "content"]  # one rebuild, then the block
    assert not stored(repository, "lessons")


def test_a_lesson_that_passes_on_the_second_try_is_kept(client, monkeypatch, repository):
    calls = scripted(
        monkeypatch, lambda n, kind: BAD if n == 2 else OK
    )  # n=1 is the request screen
    r = client.post("/api/lessons", json={"topic": "volcanoes", "age": 12})
    assert r.status_code == 201 and r.json()["id"] in stored(repository, "lessons")
    assert [c[0] for c in calls] == ["request", "content", "content"]


def test_an_unsuitable_topic_is_stopped_before_the_model_writes_anything(
    client, monkeypatch, repository
):
    calls = scripted(monkeypatch, lambda n, kind: BAD)
    r = client.post("/api/lessons", json={"topic": "something unsuitable", "age": 10})
    assert_blocked(r)
    assert "something unsuitable" not in r.text
    assert "lesson_request" not in client.seen and len(calls) == 1  # no writing, no cost
    assert not stored(repository, "lessons")


def test_the_checker_reads_the_whole_lesson_including_picture_briefs(client, monkeypatch):
    planned = FAKE_LESSON.model_copy(deep=True)
    planned.sections[0] = LessonSection(
        heading="Addresses",
        body="Every device gets an IP address.",
        visual=SectionVisual(kind="diagram", purpose="p", brief="BRIEF: arrows to router"),
    )
    monkeypatch.setattr(app_module, "make_lesson", lambda request: planned.model_copy(deep=True))
    calls = scripted(monkeypatch, lambda n, kind: OK)
    assert client.post("/api/lessons", json={"topic": "networks", "age": 12}).status_code == 201
    kind, age, text = calls[1]
    assert (kind, age) == ("content", 12)
    assert "How Networks Talk" in text and "Every device gets an IP address." in text
    assert "NACLs are stateless" in text and "BRIEF: arrows to router" in text


def test_a_broken_checker_stops_a_young_learners_lesson(client, monkeypatch, repository):
    broken_checker(monkeypatch)
    assert_unavailable(client.post("/api/lessons", json={"topic": "volcanoes", "age": 12}))
    assert "lesson_request" not in client.seen and not stored(repository, "lessons")


def test_the_checker_sees_the_capped_age_for_an_adult(client, monkeypatch):
    calls = scripted(monkeypatch, lambda n, kind: OK)
    client.post("/api/lessons", json={"topic": "volcanoes", "age": 40})
    assert all(age == 40 for _, age, _ in calls)  # the cap is applied inside the checker


def test_a_lesson_from_a_book_is_checked_too_but_its_locator_is_not_screened(client, monkeypatch):
    from tests.learning.test_materials_api import book

    mid = book(client)["id"]
    calls = scripted(monkeypatch, lambda n, kind: OK)
    r = client.post("/api/lessons", json={"topic": "chapter 2", "age": 13, "material_id": mid})
    assert r.status_code == 201
    assert [c[0] for c in calls] == ["content"]  # "chapter 2" isn't a request to screen


def test_a_blocked_book_lesson_is_a_422(client, monkeypatch, repository):
    from tests.learning.test_materials_api import book

    mid = book(client)["id"]
    scripted(monkeypatch, always_block_content)
    assert_blocked(
        client.post("/api/lessons", json={"topic": "chapter 2", "age": 13, "material_id": mid})
    )
    assert not stored(repository, "lessons")


# ---------------------------------------------------------------- visuals
def test_an_unsuitable_picture_request_is_stopped_before_drawing(client, monkeypatch, lesson_id):
    scripted(monkeypatch, lambda n, kind: BAD)
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "something unsuitable"})
    assert_blocked(r)
    assert "visual_request" not in client.seen and "something unsuitable" not in r.text


def test_blocked_drawings_are_not_stored_and_can_be_tried_again(client, monkeypatch, lesson_id):
    scripted(monkeypatch, always_block_content)
    assert_blocked(client.post(f"/api/lessons/{lesson_id}/visuals", json={}))
    assert client.get(f"/api/lessons/{lesson_id}").json()["visuals"] == []
    scripted(monkeypatch, lambda n, kind: OK)  # the "draw once" latch was released
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    assert r.status_code == 201 and len(r.json()["visuals"]) == 1


def test_the_text_of_a_drawing_is_checked(client, monkeypatch, lesson_id):
    calls = scripted(monkeypatch, lambda n, kind: OK)
    client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    kind, _, text = calls[-1]
    assert kind == "content" and "How a firewall decides" in text and "Follow the arrows." in text


def test_a_broken_checker_stops_drawings(client, monkeypatch, lesson_id):
    broken_checker(monkeypatch)
    assert_unavailable(client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "a firewall"}))


# ---------------------------------------------------------------- quizzes, tournaments, decks, follow-ups
def test_a_blocked_quiz_is_not_stored(client, monkeypatch, lesson_id, repository):
    scripted(monkeypatch, always_block_content)
    assert_blocked(client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3}))
    assert not stored(repository, "quizzes")


def test_the_checker_reads_every_part_of_a_quiz(client, monkeypatch, lesson_id):
    calls = scripted(monkeypatch, lambda n, kind: OK)
    client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3})
    _, _, text = calls[-1]
    for part in (
        "Networking (medium)",
        "Usable IPs in a /24 on AWS?",
        "254",
        "AWS reserves 5.",
        "NACLs are stateless.",
    ):
        assert part in text


def test_a_blocked_tournament_is_not_stored(client, monkeypatch, lesson_id, repository):
    scripted(monkeypatch, always_block_content)
    assert_blocked(
        client.post("/api/tournament", json={"lesson_id": lesson_id, "num_questions": 3})
    )
    assert not stored(repository, "tournaments")


def test_a_blocked_deck_is_not_stored_and_the_cards_were_read(
    client, monkeypatch, lesson_id, repository
):
    calls = scripted(monkeypatch, always_block_content)
    assert_blocked(client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}))
    assert (
        not stored(repository, "decks") and "Card 0?" in calls[0][2] and "Answer 2" in calls[0][2]
    )


def test_a_blocked_followup_is_not_stored(client, monkeypatch, lesson_id, repository):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    scripted(monkeypatch, always_block_content)
    assert_blocked(followup(client, quiz["id"]))
    assert stored(repository, "quizzes") == [quiz["id"]]


def test_the_followup_check_includes_the_focus_text(client, monkeypatch, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    calls = scripted(monkeypatch, lambda n, kind: OK)
    assert followup(client, quiz["id"]).status_code == 201
    assert "Firewalls" in calls[-1][2] and "New question 0?" in calls[-1][2]


@pytest.mark.parametrize(
    "path, body, kind",
    [
        ("/api/lessons/{lesson}/quiz", {"num_questions": 3}, "quizzes"),
        ("/api/lessons/{lesson}/flashcards", {"num_cards": 3}, "decks"),
        ("/api/tournament", {"lesson_id": "{lesson}", "num_questions": 3}, "tournaments"),
    ],
)
def test_a_broken_checker_stops_everything_a_learner_would_see(
    client, monkeypatch, repository, lesson_id, path, body, kind
):
    broken_checker(monkeypatch)
    body = {k: v.format(lesson=lesson_id) if isinstance(v, str) else v for k, v in body.items()}
    assert_unavailable(client.post(path.format(lesson=lesson_id), json=body))
    assert not stored(repository, kind)


# ---------------------------------------------------------------- errors never leak what the provider said
def leaky(*a, **k):
    raise RuntimeError("provider says: sk-secret-123 is not a valid key")


def test_generation_failures_are_generic_502s(client, monkeypatch, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    monkeypatch.setattr(app_module, "make_lesson", leaky)
    monkeypatch.setattr(app_module, "make_visuals", leaky)
    monkeypatch.setattr(store, "make_quiz", leaky)
    monkeypatch.setattr(store, "make_cards", leaky)
    monkeypatch.setattr(store, "make_remedial", leaky)
    responses = [
        client.post("/api/lessons", json={"topic": "x", "age": 12}),
        client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "a firewall"}),
        client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3}),
        client.post("/api/tournament", json={"lesson_id": lesson_id, "num_questions": 3}),
        client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}),
        followup(client, quiz["id"]),
    ]
    for r in responses:
        assert r.status_code == 502, r.text
        assert "sk-secret" not in r.text and "provider says" not in r.text
        assert "try again" in r.json()["detail"].lower()


# ---------------------------------------------------------------- layer A: the age rules are in every prompt
def rules(age):
    return safety.content_rules(age)


PROMPTS = {
    "lesson": lambda age: lesson_module.lesson_prompt(
        lesson_module.LessonRequest(topic="volcanoes", age=age)
    ),
    "lesson from a book": lambda age: lesson_module.lesson_prompt(
        lesson_module.LessonRequest(topic="chapter 2", age=age), "[Page 1] Magma rises."
    ),
    "quiz context": lambda age: quiz_graph._context(
        quiz_graph.QuizRequest(prompt="x", age=age, source="LESSON")
    ),
    "flashcards": lambda age: flashcards.cards_prompt(FAKE_LESSON, "networks", age, 5),
    "follow-up question": lambda age: weakness.variant_prompt(
        remedial_request().model_copy(update={"age": age}),
        weakness.Slot(n=0, weakness=0, angle="a", qtype="multiple_choice", truth=True),
        weakness.Weakness(concept="c", misses=[0]),
        [],
        "",
    ),
    "hidden-box diagram": lambda age: vr.blank_prompt(visual(), vr.nodes(LEAF), "plants", age, 1),
    "visual drawing": lambda age: __import__(
        "learning_service.visuals.harness", fromlist=["x"]
    ).draw_prompt(
        visual_request().model_copy(update={"age": age}),
        __import__("learning_service.visuals.models", fromlist=["x"]).VisualPlanItem(
            kind="diagram", purpose="p", brief="b", section_index=0
        ),
    ),
    "visual request": lambda age: __import__(
        "learning_service.visuals.harness", fromlist=["x"]
    ).plan_prompt(visual_request(ask="show me a firewall").model_copy(update={"age": age})),
}


@pytest.mark.parametrize("name", PROMPTS)
@pytest.mark.parametrize("age", [8, 12, 16, 40])
def test_every_generation_prompt_carries_the_age_rules(name, age):
    assert rules(age) in PROMPTS[name](age)


def test_the_quiz_reviewer_sees_the_rules_too(monkeypatch):
    from tests.learning.test_quiz_graph import RecordingLLM

    llm = RecordingLLM(quiz_graph.Review())
    monkeypatch.setattr(quiz_graph, "_llm", lambda: llm)
    state = {
        "request": quiz_graph.QuizRequest(prompt="x", age=12, source="LESSON"),
        "questions": {
            0: quiz_graph.Question(
                index=0, type="true_false", question="q", answer="True", explanation="e"
            )
        },
    }
    quiz_graph.review(state)
    assert rules(12) in llm.prompts[0]


def test_a_quiz_with_no_age_has_no_rules_to_add():
    assert quiz_graph._context(quiz_graph.QuizRequest(prompt="x")) == ""


# ---------------------------------------------------------------- documentation
@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/lessons"),
        ("post", "/api/lessons/{lesson_id}/visuals"),
        ("post", "/api/lessons/{lesson_id}/quiz"),
        ("post", "/api/lessons/{lesson_id}/flashcards"),
        ("post", "/api/quiz/{quiz_id}/followup"),
        ("post", "/api/tournament"),
    ],
)
def test_the_blocked_response_is_documented(client, method, path):
    responses = client.app.openapi()["paths"][path][method]["responses"]
    assert "422" in responses and "age" in responses["422"]["description"].lower()


def test_the_walkthrough_mentions_the_safety_rules():
    from learning_service.api_docs import DESCRIPTION

    assert "age" in DESCRIPTION and "safety" in DESCRIPTION.lower()
