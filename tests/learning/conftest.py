import pytest
from fastapi.testclient import TestClient

import learning_service.materials as materials
import learning_service.rag as rag
import learning_service.routes as app_module
import learning_service.safety as safety
import learning_service.store as store
from learning_service.flashcards import Flashcard
from learning_service.lesson import Lesson, LessonSection
from learning_service.quiz_graph import Question, Quiz
from learning_service.visual_recall import BlankItem, IllustratedQuestion
from learning_service.visuals.models import Visual
from learning_service.weakness import RemedialQuiz

FAKE_VISUAL = Visual(
    kind="diagram",
    section_index=1,
    title="How a firewall decides",
    caption="Follow the arrows.",
    alt="Message goes to firewall, which allows or blocks it.",
    code='flowchart LR\n  m["Message"] --> f{"Firewall"}\n  f -->|"allowed"| in["Inside"]',
)

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
        Question(
            index=0,
            type="multiple_choice",
            question="Usable IPs in a /24 on AWS?",
            options=["254", "251", "256", "252"],
            answer="251",
            explanation="AWS reserves 5.",
        ),
        Question(
            index=1, type="true_false", question="Flagged question", answer="True", explanation="x"
        ),
        Question(
            index=2,
            type="true_false",
            question="NACLs are stateful.",
            answer="False",
            explanation="NACLs are stateless.",
        ),
    ],
    unresolved=[1],
)


@pytest.fixture(autouse=True)
def _no_visual_model(monkeypatch):
    """A VISUAL_MODEL, SAFETY_MODEL or AUDIENCE_MAX_AGE in the developer's .env must not leak into tests."""
    for name in ("VISUAL_MODEL", "SAFETY_MODEL", "AUDIENCE_MAX_AGE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(monkeypatch, repository):
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
        return [
            BlankItem(
                visual=visual,
                node_id="f",
                answer="Firewall",
                distractors=["Router", "Modem", "Cable"],
                explanation="The firewall decides.",
            )
        ][:k]

    def fake_make_remedial(request):
        seen["remedial_request"] = request
        questions = [
            IllustratedQuestion(
                index=i,
                type="multiple_choice",
                question=f"New question {i}?",
                options=[f"Right {i}", f"Wrong {i}a", f"Wrong {i}b", f"Wrong {i}c"],
                answer=f"Right {i}",
                explanation=f"Because {i}.",
            )
            for i in range(2)
        ]
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

    def fake_check(text, age, *, topic="", kind="content"):
        """The safety checker approves everything; tests of the guardrails swap it for a scripted one."""
        seen.setdefault("safety_checks", []).append((kind, age, text))
        return safety.Verdict(ok=True)

    def fake_rag_index(_material_id, _pages):
        pass  # no-op: tests fall back to BM25 retrieval for PDF lessons

    def fake_rag_retrieve(_material_id, _query, k=8):
        return []  # empty triggers BM25 fallback in create_lesson

    def fake_make_lesson_from_images(request, _images_b64):
        seen["image_lesson_request"] = request
        return FAKE_LESSON.model_copy(deep=True)

    monkeypatch.setattr(safety, "check", fake_check)
    monkeypatch.setattr(app_module, "make_lesson_from_pages", fake_make_lesson_from_pages)
    monkeypatch.setattr(app_module, "make_lesson_from_images", fake_make_lesson_from_images)
    monkeypatch.setattr(materials, "model_vision_reader", fake_vision_reader)
    monkeypatch.setattr(rag, "index", fake_rag_index)
    monkeypatch.setattr(rag, "retrieve", fake_rag_retrieve)
    monkeypatch.setattr(store, "make_remedial", fake_make_remedial)
    monkeypatch.setattr(store, "make_cards", fake_make_cards)
    monkeypatch.setattr(store, "make_blank_items", fake_make_blank_items)
    monkeypatch.setattr(app_module, "make_lesson", fake_make_lesson)
    monkeypatch.setattr(app_module, "make_visuals", fake_make_visuals)
    monkeypatch.setattr(store, "make_quiz", fake_make_quiz)
    from app.config import Settings
    from app.main import create_app

    application = create_app(
        Settings(
            app_env="test",
            demo_mode=True,
            generation_enabled=True,
            generation_daily_limit=1000,
            _env_file=None,
        ),
        repository=repository,
    )
    with TestClient(application) as c:
        session = c.post(
            "/auth/login", json={"email": "parent@demo.example.com", "password": "DemoParent123!"}
        ).json()
        c.headers["Authorization"] = "Bearer " + session["access_token"]
        c.seen = seen
        yield c


@pytest.fixture
def lesson_id(client):
    r = client.post("/api/lessons", json={"topic": "computer networks", "age": 12})
    assert r.status_code == 201, r.text
    key = r.json()["id"]
    assert (
        client.post(
            f"/api/lessons/{key}/approve", json={"expected_revision": 1, "confirmed": True}
        ).status_code
        == 200
    )
    return key
