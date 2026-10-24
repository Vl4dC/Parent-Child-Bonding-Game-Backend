import pytest
from fastapi.testclient import TestClient

import app as app_module
import materials
import store
from lesson import Lesson, LessonSection
from quiz_graph import Question, Quiz
from flashcards import Flashcard
from visual_recall import BlankItem, IllustratedQuestion
from visuals.models import Visual
from weakness import RemedialQuiz

FAKE_VISUAL = Visual(kind="diagram", section_index=1, title="How a firewall decides", caption="Follow the arrows.",
                     alt="Message goes to firewall, which allows or blocks it.",
                     code='flowchart LR\n  m["Message"] --> f{"Firewall"}\n  f -->|"allowed"| in["Inside"]')

FAKE_LESSON = Lesson(
    title="How Networks Talk",
    summary="Computers share addresses so messages find the right place.",
    sections=[
        LessonSection(heading="Addresses", body="Every device gets an IP address."),
        LessonSection(heading="Rules", body="Firewalls decide which messages get in."),
    ],
    key_points=["Devices have addresses", "AWS reserves 5 IPs per subnet", "NACLs are stateless"],
)

FAKE_QUIZ = Quiz(
    title="Networking (medium)",
    topic="Networking",
    difficulty="medium",
    questions=[
        Question(index=0, type="multiple_choice", question="Usable IPs in a /24 on AWS?",
                 options=["254", "251", "256", "252"], answer="251", explanation="AWS reserves 5."),
        Question(index=1, type="true_false", question="Flagged question", answer="True", explanation="x"),
        Question(index=2, type="true_false", question="NACLs are stateful.", answer="False",
                 explanation="NACLs are stateless."),
    ],
    unresolved=[1],
)


@pytest.fixture(autouse=True)
def _no_visual_model(monkeypatch):
    """A VISUAL_MODEL in the developer's .env must not leak into tests that fake `_llm`."""
    monkeypatch.delenv("VISUAL_MODEL", raising=False)


@pytest.fixture
def client(monkeypatch):
    """TestClient with the model calls faked; `client.seen` records what the agents were asked."""
    seen = {}

    def fake_make_lesson(request):
        seen["lesson_request"] = request
        return FAKE_LESSON.model_copy(deep=True)

    def fake_make_quiz(request):
        seen["quiz_request"] = request
        return FAKE_QUIZ.model_copy(deep=True)

    def fake_make_visuals(request):
        seen["visual_request"] = request
        return [FAKE_VISUAL.model_copy()]

    def fake_make_cards(lesson, topic, age, n):
        seen["cards_request"] = (topic, age, n)
        return [Flashcard(kind="concept", front=f"Card {i}?", back=f"Answer {i}") for i in range(n)]

    def fake_make_blank_items(visual, topic, age, k=1):
        seen.setdefault("blank_calls", []).append(k)
        return [BlankItem(visual=visual, node_id="f", answer="Firewall", distractors=["Router", "Modem", "Cable"],
                          explanation="The firewall decides.")][:k]

    def fake_make_remedial(request):
        seen["remedial_request"] = request
        questions = [IllustratedQuestion(index=i, type="multiple_choice", question=f"New question {i}?",
                                         options=[f"Right {i}", f"Wrong {i}a", f"Wrong {i}b", f"Wrong {i}c"],
                                         answer=f"Right {i}", explanation=f"Because {i}.") for i in range(2)]
        return RemedialQuiz(focus=["Firewalls"], questions=questions)

    def fake_make_lesson_from_pages(request, pages):
        seen["grounded_request"] = request
        seen["pages_text"] = pages
        return FAKE_LESSON.model_copy(deep=True)

    def fake_vision_reader():
        def read(_jpeg):
            seen["vision_calls"] = seen.get("vision_calls", 0) + 1
            return "A photographed page about volcanoes, lava and magma rising through the crust."
        return read

    monkeypatch.setattr(app_module, "make_lesson_from_pages", fake_make_lesson_from_pages)
    monkeypatch.setattr(materials, "model_vision_reader", fake_vision_reader)
    monkeypatch.setattr(store, "make_remedial", fake_make_remedial)
    monkeypatch.setattr(store, "make_cards", fake_make_cards)
    monkeypatch.setattr(store, "make_blank_items", fake_make_blank_items)
    monkeypatch.setattr(app_module, "make_lesson", fake_make_lesson)
    monkeypatch.setattr(app_module, "make_visuals", fake_make_visuals)
    monkeypatch.setattr(store, "make_quiz", fake_make_quiz)
    for s in (store.LESSONS, store.QUIZZES, store.TOURNAMENTS, store.DECKS, store.MATERIALS):
        s.clear()
    c = TestClient(app_module.app)
    c.seen = seen
    return c


@pytest.fixture
def lesson_id(client):
    r = client.post("/api/lessons", json={"topic": "computer networks", "age": 12})
    assert r.status_code == 201, r.text
    return r.json()["id"]
