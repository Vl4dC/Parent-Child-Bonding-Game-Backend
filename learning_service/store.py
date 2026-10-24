"""Typed owned learning records and generators. Durable persistence is supplied by app.repository."""

from __future__ import annotations

import logging
import random
import uuid
from typing import TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.generation import ContextPool as ThreadPoolExecutor
from app.repository import OwnedStore, current_session
from learning_service.flashcards import (
    Flashcard,
    StoredDeck,
    card_from_blank,
    card_from_question,
    make_cards,
    start,
)
from learning_service.lesson import Lesson, LessonRequest, LessonSource
from learning_service.materials import Material
from learning_service.quiz_graph import QuestionType, QuizRequest, make_quiz
from learning_service.safety import ensure_safe
from learning_service.visual_recall import (
    BlankItem,
    IllustratedQuestion,
    make_blank_items,
    recallable,
    to_mcq,
    to_true_false,
)
from learning_service.visuals.models import Visual, VisualKind
from learning_service.visuals.render import document
from learning_service.weakness import Miss, RemedialRequest, make_remedial

log = logging.getLogger("quiz_app")
CLICKABLE_TYPES: list[QuestionType] = ["multiple_choice", "true_false"]
MAX_DIAGRAM_ITEMS = 2  # per quiz and per deck


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class StoredVisual(Visual):
    id: str


class PublicVisual(BaseModel):
    """A visual ready for <iframe sandbox="allow-scripts" srcdoc=html>."""

    id: str
    kind: VisualKind
    section_index: int
    title: str
    caption: str
    alt: str
    html: str

    @classmethod
    def of(cls, v: StoredVisual) -> PublicVisual:
        return cls(
            id=v.id,
            kind=v.kind,
            section_index=v.section_index,
            title=v.title,
            caption=v.caption,
            alt=v.alt,
            html=document(v, v.id),
        )


class StoredLesson(BaseModel):
    id: str
    request: LessonRequest
    lesson: Lesson
    source: LessonSource | None = None  # set when the lesson was built from an upload
    visuals: list[StoredVisual] = []
    status: str = "draft"
    revision: int = 1
    approval: dict | None = None
    previous_id: str | None = None
    auto_drawn: bool = False  # lesson-planned visuals already drawn (or being drawn)


class PublicQuestion(BaseModel):
    """A question as the browser sees it: no answer, no explanation."""

    index: int
    type: QuestionType
    question: str
    options: list[str]
    visual_html: str | None = None  # diagram with a hidden/dashed box, for diagram questions


class PublicQuiz(BaseModel):
    id: str
    lesson_id: str
    title: str
    dropped: int
    questions: list[PublicQuestion]
    focus: list[str] = []  # follow-up quizzes: the weak spots being practised
    parent_id: str | None = None  # follow-up quizzes: the quiz whose mistakes they came from


class CheckOut(BaseModel):
    correct: bool
    answer_index: int
    explanation: str
    answer_visual_html: str | None = None  # the completed diagram, for diagram questions


class Attempt(BaseModel):
    choice: int
    correct: bool


class StoredQuiz(BaseModel):
    id: str
    question_set_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    lesson_id: str
    title: str
    dropped: int
    questions: list[IllustratedQuestion]
    focus: list[str] = []
    parent_id: str | None = None
    approved: bool = False
    approval: dict | None = None
    submission: dict | None = None
    attempts: dict[int, Attempt] = {}  # question index -> the learner's first answer

    def public(self) -> PublicQuiz:
        return PublicQuiz(
            id=self.id,
            lesson_id=self.lesson_id,
            title=self.title,
            dropped=self.dropped,
            focus=self.focus,
            parent_id=self.parent_id,
            questions=[
                PublicQuestion(
                    index=q.index,
                    type=q.type,
                    question=q.question,
                    options=q.options,
                    visual_html=document(q.visual, f"{self.id}-q{q.index}") if q.visual else None,
                )
                for q in self.questions
            ],
        )


# Practice quizzes and tournaments live in separate stores, so an id from one
# can't be used against the other's endpoints.
LESSONS = OwnedStore("lessons", StoredLesson)
QUIZZES = OwnedStore("quizzes", StoredQuiz)
TOURNAMENTS = OwnedStore("tournaments", StoredQuiz)
DECKS = OwnedStore("decks", StoredDeck)
MATERIALS = OwnedStore("materials", Material)


T = TypeVar("T")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def new_id() -> str:
    return uuid.uuid4().hex


def remember_material(material: Material) -> Material:
    """Give an upload an ID and persist it under the verified owner."""
    material.id = new_id()
    MATERIALS[material.id] = material
    current_session.get().put(
        "material_revisions",
        f"{material.id}:1",
        {
            "material_id": material.id,
            "revision": 1,
            "name": material.name,
            "pages": [page.model_dump(mode="json") for page in material.pages],
        },
    )

    return material


def get_or_404(store: dict[str, T], item_id: str, what: str) -> T:
    item = store.get(item_id)
    if item is None:
        raise HTTPException(404, f"{what} not found or unavailable to this account")
    return item


def question_at(quiz: StoredQuiz, index: int) -> IllustratedQuestion:
    if not 0 <= index < len(quiz.questions):
        raise HTTPException(422, f"index must be between 0 and {len(quiz.questions) - 1}")
    return quiz.questions[index]


def grade(q: IllustratedQuestion, choice: int) -> CheckOut:
    if not 0 <= choice < len(q.options):
        raise HTTPException(422, f"choice must be between 0 and {len(q.options) - 1}")
    answer_index = q.options.index(q.answer)
    after = document(q.answer_visual, f"answer-{new_id()}") if q.answer_visual else None
    return CheckOut(
        correct=choice == answer_index,
        answer_index=answer_index,
        explanation=q.explanation,
        answer_visual_html=after,
    )


def answer_question(quiz: StoredQuiz, index: int, choice: int) -> CheckOut:
    """Grade an answer and remember the learner's first one (that's what the follow-up quiz is built from)."""
    out = grade(question_at(quiz, index), choice)
    quiz.attempts.setdefault(index, Attempt(choice=choice, correct=out.correct))
    return out


def misses(quiz: StoredQuiz) -> list[Miss]:
    return [
        Miss(index=i, question=quiz.questions[i], picked=quiz.questions[i].options[a.choice])
        for i, a in sorted(quiz.attempts.items())
        if not a.correct
    ]


# --------------------------------------------------------------------------
# Diagram recall (shared by quizzes and decks)
# --------------------------------------------------------------------------
def _diagrams(stored: StoredLesson) -> list[StoredVisual]:
    return [v for v in stored.visuals if v.kind == "diagram" and recallable(v.code)]


def diagram_slots(stored: StoredLesson, total: int) -> int:
    """How many of `total` questions/cards come from the lesson's diagrams: one box per diagram
    (two boxes from one picture would give each other away), always leaving 1 for text."""
    return max(0, min(MAX_DIAGRAM_ITEMS, total - 1, len(_diagrams(stored))))


def blank_items(stored: StoredLesson, wanted: int) -> list[BlankItem]:
    """Hide one box in each of `wanted` different diagrams. Failures just mean fewer items."""
    diagrams = _diagrams(stored)
    if not diagrams or wanted < 1:
        return []
    chosen = random.sample(diagrams, min(wanted, len(diagrams)))
    items: list[BlankItem] = []
    with ThreadPoolExecutor(len(chosen)) as pool:
        futures = [
            pool.submit(make_blank_items, d, stored.request.topic, stored.request.age, 1)
            for d in chosen
        ]
        for f in futures:
            try:
                items += f.result()
            except Exception:
                log.warning("diagram recall failed")
    return items[:wanted]


def interleave(main: list[T], extra: list[T], every: int) -> list[T]:
    """Slot one `extra` after every `every` items of `main`; leftovers go at the end."""
    out, extra = [], list(extra)
    for i, item in enumerate(main, 1):
        out.append(item)
        if i % every == 0 and extra:
            out.append(extra.pop(0))
    return out + extra


# --------------------------------------------------------------------------
# Safety: what the safety check reads (everything a learner will see), see safety.py
# --------------------------------------------------------------------------
def quiz_text(quiz: StoredQuiz) -> str:
    parts = [quiz.title, *quiz.focus]
    for q in quiz.questions:
        parts += [q.question, *q.options, q.explanation]
    return "\n".join(parts)


def deck_text(deck: StoredDeck) -> str:
    return "\n".join([deck.title, *(f"{c.front}\n{c.back}" for c in deck.cards)])


# --------------------------------------------------------------------------
# Builders (each result is checked for the learner's age before it is returned, so before it is stored)
# --------------------------------------------------------------------------
def quiz_from_lesson(stored: StoredLesson, num_questions: int) -> StoredQuiz:
    return ensure_safe(
        lambda: _build_quiz(stored, num_questions),
        quiz_text,
        stored.request.age,
        stored.request.topic,
    )


def _build_quiz(stored: StoredLesson, num_questions: int) -> StoredQuiz:
    """Quiz agent on the lesson text, plus diagram questions if the lesson has diagrams.
    Text questions that never passed review are dropped."""
    req = stored.request
    n_diagram = diagram_slots(stored, num_questions)
    quiz_request = QuizRequest(
        prompt=f"Quiz on {req.topic} for a {req.age}-year-old who just finished the lesson provided",
        num_questions=num_questions - n_diagram,
        allowed_types=CLICKABLE_TYPES,
        age=req.age,
        source=stored.lesson.as_text(),
    )
    with ThreadPoolExecutor(2) as pool:
        quiz_future = pool.submit(make_quiz, quiz_request)
        blanks_future = pool.submit(blank_items, stored, n_diagram)
        try:
            quiz = quiz_future.result()
        except Exception as exc:
            log.warning("quiz generation failed")
            raise HTTPException(502, "Quiz generation failed. Try again later.") from exc
        blanks = blanks_future.result()

    unresolved = set(quiz.unresolved)
    kept = [
        IllustratedQuestion(**q.model_dump()) for q in quiz.questions if q.index not in unresolved
    ]
    if not kept:
        raise HTTPException(502, "Couldn't produce reliable questions - try again")
    rng = random.Random()
    pictured = [
        (to_mcq if j % 2 == 0 else to_true_false)(item, rng) for j, item in enumerate(blanks)
    ]
    questions = interleave(kept, pictured, every=2)
    return StoredQuiz(
        id=new_id(),
        lesson_id=stored.id,
        title=quiz.title,
        dropped=len(quiz.questions) - len(kept),
        questions=[q.model_copy(update={"index": i}) for i, q in enumerate(questions)],
    )


def deck_from_lesson(stored: StoredLesson, num_cards: int) -> StoredDeck:
    return ensure_safe(
        lambda: _build_deck(stored, num_cards),
        deck_text,
        stored.request.age,
        stored.request.topic,
    )


def _build_deck(stored: StoredLesson, num_cards: int) -> StoredDeck:
    """Concept cards from the lesson text, plus diagram cards if the lesson has diagrams.
    Writes a full deck of concept cards and trims it, so the deck has exactly `num_cards`
    even when a diagram card falls through."""
    req = stored.request
    n_diagram = diagram_slots(stored, num_cards)
    with ThreadPoolExecutor(2) as pool:
        cards_future = pool.submit(make_cards, stored.lesson, req.topic, req.age, num_cards)
        blanks_future = pool.submit(blank_items, stored, n_diagram)
        try:
            concept = cards_future.result()
        except Exception as exc:
            log.warning("flashcard generation failed")
            raise HTTPException(502, "Flashcard generation failed. Try again later.") from exc
        blanks = blanks_future.result()

    if not concept:
        raise HTTPException(502, "Couldn't write flashcards for this lesson - try again")
    concept = concept[: max(1, num_cards - len(blanks))]
    cards: list[Flashcard] = interleave(concept, [card_from_blank(b) for b in blanks], every=3)
    return StoredDeck(
        id=new_id(),
        lesson_id=stored.id,
        title=f"{stored.lesson.title}: flashcards",
        cards=cards,
        progress=start(len(cards)),
    )


def followup_quiz(
    quiz: StoredQuiz, lesson: StoredLesson, num_questions: int | None = None
) -> StoredQuiz:
    """A new quiz on what the learner got wrong: different questions on the same weak ideas
    (weakness.py), so it can't be passed by remembering the old answers."""
    missed = misses(quiz)
    if not missed:
        raise HTTPException(409, "Nothing to practise yet: there are no wrong answers in this quiz")
    req = lesson.request

    def build() -> StoredQuiz:
        try:
            result = make_remedial(
                RemedialRequest(
                    topic=req.topic,
                    age=req.age,
                    lesson_text=lesson.lesson.as_text(),
                    misses=missed,
                    num_questions=num_questions,
                )
            )
        except Exception as exc:
            log.warning("follow-up quiz generation failed")
            raise HTTPException(502, "Follow-up quiz generation failed. Try again later.") from exc
        if not result.questions:
            raise HTTPException(
                502, "Couldn't write reliable new questions on those topics - try again"
            )
        return StoredQuiz(
            id=new_id(),
            lesson_id=lesson.id,
            # Neutral wording on purpose: the child-safety checker blocked "your weak spots" as shaming.
            title=f"{lesson.lesson.title}: practice round",
            dropped=0,
            questions=result.questions,
            focus=result.focus,
            parent_id=quiz.id,
        )

    return ensure_safe(build, quiz_text, req.age, req.topic)


def deck_from_quiz(quiz: StoredQuiz, which: str = "missed") -> StoredDeck:
    """Flashcards from a quiz, so the learner can memorise what they missed. No model call."""
    wrong = {
        i: quiz.questions[i].options[a.choice] for i, a in quiz.attempts.items() if not a.correct
    }
    pairs = (
        [(q, wrong.get(q.index)) for q in quiz.questions]
        if which == "all"
        else [(quiz.questions[i], picked) for i, picked in sorted(wrong.items())]
    )
    if not pairs:
        raise HTTPException(409, "Nothing to review yet: there are no wrong answers in this quiz")
    cards = [card_from_question(q, picked) for q, picked in pairs]
    return StoredDeck(
        id=new_id(),
        lesson_id=quiz.lesson_id,
        title="Mistakes to remember" if which == "missed" else "Quiz review",
        cards=cards,
        progress=start(len(cards)),
    )
