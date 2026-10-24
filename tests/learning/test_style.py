"""House style for everything the model writes for a learner: no em dashes (style.py).

The prompts ask the model not to use them, and code removes any that slip through, because models ignore
prompt rules some of the time.
"""

import pytest

import learning_service.safety as safety
import learning_service.store as store
import learning_service.style as style
from learning_service.flashcards import Flashcard
from learning_service.quiz_graph import Question, Quiz
from learning_service.safety import Verdict
from learning_service.visual_recall import IllustratedQuestion
from learning_service.weakness import RemedialQuiz
from tests.learning.conftest import FAKE_LESSON, FAKE_QUIZ, FAKE_VISUAL
from tests.learning.test_followup_api import answer, followup, make_quiz

EM = chr(0x2014)
BAR = chr(0x2015)  # horizontal bar, the other long dash


def no_em(response):
    assert response.status_code in (200, 201), response.text
    assert EM not in response.text and "\\u2014" not in response.text and BAR not in response.text


# ---------------------------------------------------------------- the text rule
@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            f"Plants make food {EM} a process called photosynthesis.",
            "Plants make food - a process called photosynthesis.",
        ),
        (f"The sun {EM} our star {EM} is hot", "The sun - our star - is hot"),
        (f"Sun{EM}the star", "Sun-the star"),
        (f"Wait{EM} what?", "Wait - what?"),
        (f"Pause {BAR} then go", "Pause - then go"),
        (
            "Ages 10–18 and a plain-hyphen stay as they are.",
            "Ages 10–18 and a plain-hyphen stay as they are.",
        ),
        ("", ""),
    ],
)
def test_plain_replaces_every_long_dash(raw, expected):
    assert style.plain(raw) == expected


def test_plain_keeps_line_breaks_and_is_idempotent():
    text = f"first line {EM} more\nsecond line{EM}end"
    once = style.plain(text)
    assert once.count("\n") == 1 and EM not in once
    assert style.plain(once) == once


# ---------------------------------------------------------------- cleaning whole results
def test_clean_reaches_every_text_field_and_leaves_the_original_alone():
    lesson = FAKE_LESSON.model_copy(deep=True)
    lesson.title = f"Networks {EM} a tour"
    lesson.sections[0].body = f"Addresses{EM}everywhere."
    lesson.key_points[0] = f"IP {EM} an address"
    out = style.clean(lesson)
    assert EM not in str(out.model_dump())
    assert (out.title, out.sections[0].body, out.key_points[0]) == (
        "Networks - a tour",
        "Addresses-everywhere.",
        "IP - an address",
    )
    assert EM in lesson.title and EM in lesson.key_points[0]


def test_a_multiple_choice_answer_still_matches_its_option_after_cleaning():
    q = Question(
        type="multiple_choice",
        question=f"Which {EM} one?",
        options=[f"A {EM} b", "B", "C", "D"],
        answer=f"A {EM} b",
        explanation="e",
    )
    out = style.clean(q)
    assert out.answer in out.options and EM not in str(out.model_dump())


def test_diagram_code_is_cleaned_too_and_stays_a_diagram():
    visual = FAKE_VISUAL.model_copy(
        update={
            "code": f'flowchart LR\n  a["Sun {EM} star"] --> b["Leaf"]',
            "caption": f"Look {EM} closely",
        }
    )
    out = style.clean(visual)
    assert (
        out.code == 'flowchart LR\n  a["Sun - star"] --> b["Leaf"]'
        and out.caption == "Look - closely"
    )


def test_clean_walks_lists_dicts_and_tuples_and_ignores_other_values():
    value = {"a": [f"x {EM} y", 3, None], "b": (f"p{EM}q", 2.5), "c": True}
    assert style.clean(value) == {"a": ["x - y", 3, None], "b": ("p-q", 2.5), "c": True}


# ---------------------------------------------------------------- layer 1: the prompts say so
def test_the_rule_names_the_character_and_gives_alternatives():
    rules = style.WRITING_RULES.lower()
    assert "em dash" in rules and "comma" in rules and "colon" in rules


def test_every_age_band_including_adult_carries_the_rule(monkeypatch):
    monkeypatch.setenv("AUDIENCE_MAX_AGE", "99")
    for age in (6, 10, 13, 17, 40):
        assert style.WRITING_RULES in safety.content_rules(age)


# ---------------------------------------------------------------- layer 2: the gate removes what slips through
def test_the_gate_cleans_what_was_built_before_it_is_checked_or_returned(monkeypatch):
    seen = []
    monkeypatch.setattr(
        safety, "check", lambda text, age, **kw: seen.append(text) or Verdict(ok=True)
    )
    out = safety.ensure_safe(lambda: [f"a {EM} b"], "\n".join, 12)
    assert out == ["a - b"] and seen == ["a - b"]


# ---------------------------------------------------------------- end to end: nothing reaches the learner
def test_a_lesson_with_long_dashes_is_stored_and_returned_without_them(client, monkeypatch):
    import learning_service.routes as app_module

    dashed = FAKE_LESSON.model_copy(deep=True)
    dashed.sections[1].body = f"Firewalls decide {EM} message by message {EM} what gets in."
    monkeypatch.setattr(app_module, "make_lesson", lambda request: dashed.model_copy(deep=True))
    r = client.post("/api/lessons", json={"topic": "networks", "age": 12})
    no_em(r)
    assert "Firewalls decide - message by message - what gets in." in r.text
    no_em(client.get(f"/api/lessons/{r.json()['id']}"))


def test_drawings_with_long_dashes_are_cleaned(client, monkeypatch, lesson_id):
    import learning_service.routes as app_module

    dashed = FAKE_VISUAL.model_copy(
        update={
            "title": f"Firewall {EM} the gatekeeper",
            "alt": f"A box {EM} a gate",
            "code": f'flowchart LR\n  m["Message"] --> f{{"Fire {EM} wall"}}',
        }
    )
    monkeypatch.setattr(app_module, "make_visuals", lambda request: [dashed.model_copy()])
    no_em(client.post(f"/api/lessons/{lesson_id}/visuals", json={}))


def test_quizzes_and_tournaments_with_long_dashes_are_cleaned(client, monkeypatch, lesson_id):
    dashed = Quiz(
        title=f"Networks {EM} quiz",
        topic="Networking",
        difficulty="medium",
        questions=[
            Question(
                index=0,
                type="multiple_choice",
                question=f"Which {EM} one?",
                options=[f"Right {EM} one", "B", "C", "D"],
                answer=f"Right {EM} one",
                explanation=f"Because {EM} yes",
            ),
        ],
    )
    monkeypatch.setattr(store, "make_quiz", lambda request: dashed.model_copy(deep=True))
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3})
    no_em(quiz)
    index = quiz.json()["questions"][0]["options"].index("Right - one")
    graded = client.post(f"/api/quiz/{quiz.json()['id']}/check", json={"index": 0, "choice": index})
    no_em(graded)
    assert graded.json()["correct"] is True  # the answer still matches its option
    no_em(client.post("/api/tournament", json={"lesson_id": lesson_id, "num_questions": 3}))


def test_flashcards_with_long_dashes_are_cleaned(client, monkeypatch, lesson_id):
    monkeypatch.setattr(
        store,
        "make_cards",
        lambda lesson, topic, age, n: [
            Flashcard(kind="concept", front=f"Front {EM} {i}?", back=f"Back {EM} {i}")
            for i in range(n)
        ],
    )
    deck = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3})
    no_em(deck)
    assert deck.json()["cards"][0]["front"] == "Front - 0?"


def test_followups_with_long_dashes_are_cleaned(client, monkeypatch, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    questions = [
        IllustratedQuestion(
            index=0,
            type="multiple_choice",
            question=f"New {EM} question?",
            options=[f"Right {EM} 0", "W1", "W2", "W3"],
            answer=f"Right {EM} 0",
            explanation=f"Because {EM} so",
        )
    ]
    monkeypatch.setattr(
        store,
        "make_remedial",
        lambda request: RemedialQuiz(focus=[f"Firewalls {EM} basics"], questions=questions),
    )
    r = followup(client, quiz["id"])
    no_em(r)
    assert r.json()["focus"] == ["Firewalls - basics"]


def test_the_unmodified_quiz_fixture_has_no_long_dashes():
    assert EM not in FAKE_QUIZ.model_dump_json() and EM not in FAKE_LESSON.model_dump_json()
