"""Deterministic practice lifecycle over existing private question snapshots.

No generation, safety-checker, model-provider, or prompt function is called here.
The legacy question producer and practice/check routes retain their existing contracts.
"""

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.context import api_context
from app.repository import current_session
from learning_service.flashcards import PublicDeck
from learning_service.store import (
    DECKS,
    LESSONS,
    QUIZZES,
    Attempt,
    CheckOut,
    PublicQuiz,
    StoredQuiz,
    deck_from_quiz,
    get_or_404,
    grade,
    new_id,
    question_at,
)
from learning_service.visual_recall import IllustratedQuestion

router = APIRouter(
    prefix="/api/quiz",
    tags=["Quiz practice"],
    dependencies=[Depends(api_context, scope="function")],
    responses={
        404: {"description": "Quiz absent or in another player partition"},
        409: {"description": "Stale state, conflicting retry, or incomplete attempt"},
        429: {"description": "Request limit reached"},
        503: {"description": "Storage unavailable"},
    },
)
QuizId = Annotated[str, Path(min_length=1, max_length=128)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
MAX_RECEIPTS = 1000


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswerInput(StrictInput):
    index: int = Field(ge=0, strict=True)
    choice: int = Field(ge=0, strict=True)
    expected_answered: int = Field(ge=0, strict=True)


class FinishInput(StrictInput):
    expected_answered: int = Field(ge=0, strict=True)


class RetryInput(StrictInput):
    which: Literal["all", "missed"] = "all"


class ReviewDeckInput(StrictInput):
    which: Literal["all", "missed"] = "missed"


class QuestionInput(StrictInput):
    type: Literal["multiple_choice", "true_false"]
    question: str = Field(min_length=1, max_length=2000)
    options: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=2, max_length=4
    )
    answer_index: int = Field(ge=0, strict=True)
    explanation: str = Field(min_length=1, max_length=6000)

    @model_validator(mode="after")
    def checked(self):
        self.question = self.question.strip()
        self.explanation = self.explanation.strip()
        self.options = [option.strip() for option in self.options]
        if not self.question or not self.explanation or any(not x for x in self.options):
            raise ValueError("Questions, options and explanations must contain text")
        if len({x.casefold() for x in self.options}) != len(self.options):
            raise ValueError("Options must be distinct")
        if self.type == "multiple_choice" and len(self.options) != 4:
            raise ValueError("Multiple-choice questions require four options")
        if self.type == "true_false" and self.options != ["True", "False"]:
            raise ValueError("True/false options must be True then False")
        if self.answer_index >= len(self.options):
            raise ValueError("Answer index is outside the options")
        return self


class QuizRevisionInput(StrictInput):
    title: str = Field(min_length=1, max_length=200, pattern=r".*\S.*")
    questions: list[QuestionInput] = Field(min_length=1, max_length=50)


class QuizApprovalInput(StrictInput):
    expected_question_set_id: str = Field(min_length=1, max_length=128)
    confirmed: Literal[True]


class QuizRevisionOutput(BaseModel):
    id: str
    previous_id: str
    question_set_id: str
    revision: int
    status: Literal["draft"] = "draft"


class QuizApprovalOutput(BaseModel):
    id: str
    question_set_id: str
    approved: bool
    approval: dict


class AuthorQuestion(BaseModel):
    index: int
    type: str
    question: str
    options: list[str]
    answer_index: int
    explanation: str


class AuthorReview(BaseModel):
    id: str
    question_set_id: str
    lesson_id: str
    title: str
    approved: bool
    approval: dict | None
    questions: list[AuthorQuestion]
    review_kind: Literal["owner_confirmation"] = "owner_confirmation"


class SavedAnswer(BaseModel):
    index: int
    choice: int
    correct: bool


class AttemptSummary(BaseModel):
    id: str
    question_set_id: str
    lesson_id: str
    title: str
    parent_id: str | None
    status: Literal["not_started", "in_progress", "ready_to_finish", "completed"]
    answered: int
    total: int
    score: int
    completed_at: str | None


class Progress(AttemptSummary):
    next_index: int | None
    answers: list[SavedAnswer]


class Results(AttemptSummary):
    percentage: int
    accuracy_on_answered: int | None
    scoring_basis: Literal["first_recorded_answers"] = "first_recorded_answers"
    result_kind: Literal["private_formative_practice"] = "private_formative_practice"


class AnswerResponse(BaseModel):
    index: int
    recorded_choice: int
    feedback: CheckOut
    progress: Progress


class ReviewItem(BaseModel):
    index: int
    question: str
    options: list[str]
    choice: int
    feedback: CheckOut


class Review(BaseModel):
    results: Results
    questions: list[ReviewItem]
    lesson_id: str


class AttemptPage(BaseModel):
    items: list[AttemptSummary]
    next_cursor: str | None


def metadata(quiz, *, write=False):
    data = (quiz.submission or {}).get("practice_v1")
    if data is None:
        data = {"answers": {}, "retries": {}, "completion": None}
        if write:
            quiz.submission = (quiz.submission or {}) | {"practice_v1": data}
    return data


def quiz_for(quiz_id):
    quiz = get_or_404(QUIZZES, quiz_id, "Quiz")
    if not quiz.questions:
        raise HTTPException(409, "This question set is empty.")
    if [q.index for q in quiz.questions] != list(range(len(quiz.questions))):
        raise HTTPException(409, "The question snapshot has invalid indexes.")
    for q in quiz.questions:
        if not q.options or q.options.count(q.answer) != 1:
            raise HTTPException(409, "The question snapshot has an invalid answer key.")
    if any(
        i < 0
        or i >= len(quiz.questions)
        or a.choice < 0
        or a.choice >= len(quiz.questions[i].options)
        for i, a in quiz.attempts.items()
    ):
        raise HTTPException(409, "The saved attempt has invalid answer indexes.")
    return quiz


def progress_of(quiz):
    completed = metadata(quiz).get("completion")
    answered, total = len(quiz.attempts), len(quiz.questions)
    status = (
        "completed"
        if completed
        else "ready_to_finish"
        if answered == total
        else "in_progress"
        if answered
        else "not_started"
    )
    return Progress(
        id=quiz.id,
        question_set_id=quiz.question_set_id,
        lesson_id=quiz.lesson_id,
        title=quiz.title,
        parent_id=quiz.parent_id,
        status=status,
        answered=answered,
        total=total,
        score=sum(a.correct for a in quiz.attempts.values()),
        next_index=next((q.index for q in quiz.questions if q.index not in quiz.attempts), None),
        answers=[
            SavedAnswer(index=i, choice=a.choice, correct=a.correct)
            for i, a in sorted(quiz.attempts.items())
        ],
        completed_at=completed["completed_at"] if completed else None,
    )


def results_of(quiz):
    completion = metadata(quiz).get("completion")
    if completion and completion.get("result"):
        return Results.model_validate(completion["result"])
    progress = progress_of(quiz)
    data = progress.model_dump(exclude={"next_index", "answers"})
    return Results(
        **data,
        percentage=(200 * progress.score + progress.total) // (2 * progress.total),
        accuracy_on_answered=(200 * progress.score + progress.answered) // (2 * progress.answered)
        if progress.answered
        else None,
    )


def fingerprint(body):
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


def receipt_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def cached(receipts, key, body):
    result = receipts.get(receipt_key(key))
    if result and result["fingerprint"] != fingerprint(body):
        raise HTTPException(409, "Idempotency key was used for different details.")
    return result


def capacity(receipts):
    if len(receipts) >= MAX_RECEIPTS:
        raise HTTPException(409, "Retry receipt limit reached for this attempt.")


@router.get("/{quiz_id}/progress", response_model=Progress)
def progress(quiz_id: QuizId):
    """Resume from saved first answers; never return unanswered keys or explanations."""
    return progress_of(quiz_for(quiz_id))


@router.get("/{quiz_id}/results", response_model=Results)
def results(quiz_id: QuizId):
    """Factual partial/final counts, without rewards, pass thresholds or mastery claims."""
    return results_of(quiz_for(quiz_id))


@router.post("/{quiz_id}/answers", response_model=AnswerResponse)
def answer(quiz_id: QuizId, body: AnswerInput, key: Key):
    quiz = quiz_for(quiz_id)
    data = metadata(quiz, write=True)
    saved = cached(data["answers"], key, body)
    if saved:
        return saved["response"]
    if data["completion"]:
        raise HTTPException(409, "This attempt is complete; start another attempt.")
    if body.expected_answered != len(quiz.attempts):
        raise HTTPException(409, "Attempt changed; read its current progress.")
    question = question_at(quiz, body.index)
    previous = quiz.attempts.get(body.index)
    if previous is not None and previous.choice != body.choice:
        raise HTTPException(409, "The first recorded choice cannot be changed.")
    capacity(data["answers"])
    feedback = grade(question, body.choice)
    quiz.attempts.setdefault(body.index, Attempt(choice=body.choice, correct=feedback.correct))
    response = AnswerResponse(
        index=body.index,
        recorded_choice=quiz.attempts[body.index].choice,
        feedback=feedback,
        progress=progress_of(quiz),
    )
    data["answers"][receipt_key(key)] = {
        "fingerprint": fingerprint(body),
        "response": response.model_dump(mode="json"),
    }
    return response


@router.post("/{quiz_id}/finish", response_model=Results)
def finish(quiz_id: QuizId, body: FinishInput):
    quiz = quiz_for(quiz_id)
    if body.expected_answered != len(quiz.attempts) or len(quiz.attempts) != len(quiz.questions):
        raise HTTPException(409, "Answer every question and refresh progress before finishing.")
    data = metadata(quiz, write=True)
    if data["completion"]:
        return data["completion"]["result"]
    data["completion"] = {"completed_at": datetime.now(timezone.utc).isoformat()}
    response = results_of(quiz)
    data["completion"]["result"] = response.model_dump(mode="json")
    return response


@router.get("/{quiz_id}/review", response_model=Review)
def review(quiz_id: QuizId):
    quiz = quiz_for(quiz_id)
    if not metadata(quiz)["completion"]:
        raise HTTPException(409, "Finish this attempt before opening the full review.")
    return Review(
        results=results_of(quiz),
        lesson_id=quiz.lesson_id,
        questions=[
            ReviewItem(
                index=q.index,
                question=q.question,
                options=q.options,
                choice=quiz.attempts[q.index].choice,
                feedback=grade(q, quiz.attempts[q.index].choice),
            )
            for q in quiz.questions
        ],
    )


@router.post("/{quiz_id}/retry", status_code=201, response_model=PublicQuiz)
def retry(quiz_id: QuizId, body: RetryInput, key: Key):
    source = quiz_for(quiz_id)
    data = metadata(source, write=True)
    saved = cached(data["retries"], key, body)
    if saved:
        return quiz_for(saved["attempt_id"]).public()
    capacity(data["retries"])
    indexes = (
        list(range(len(source.questions)))
        if body.which == "all"
        else [i for i, a in sorted(source.attempts.items()) if not a.correct]
    )
    if not indexes:
        raise HTTPException(409, "There are no recorded mistakes to retry.")
    origin = {"source_id": source.id, "source_question_indexes": indexes, "which": body.which}
    fresh = source.model_copy(
        deep=True,
        update={
            "id": new_id(),
            "parent_id": source.id,
            "attempts": {},
            "submission": {"practice_origin": origin},
        },
    )
    if body.which == "missed":
        fresh.question_set_id = new_id()
        fresh.approved = False
        fresh.approval = None
        fresh.questions = [
            source.questions[i].model_copy(deep=True, update={"index": j})
            for j, i in enumerate(indexes)
        ]
    elif (source.submission or {}).get("authoring_v1"):
        fresh.submission["authoring_v1"] = copy.deepcopy(source.submission["authoring_v1"])
    QUIZZES[fresh.id] = fresh
    data["retries"][receipt_key(key)] = {"fingerprint": fingerprint(body), "attempt_id": fresh.id}
    return fresh.public()


@router.get("/{quiz_id}/attempts", response_model=AttemptPage)
def attempts(
    quiz_id: QuizId, after: str = Query("", max_length=128), limit: int = Query(20, ge=1, le=100)
):
    """Owned attempts sharing this exact question snapshot; no sibling answer payloads."""
    source = quiz_for(quiz_id)
    unit = current_session.get()
    if unit.connection:
        rows = unit.connection.execute(
            "select id,(data - 'submission') || jsonb_build_object('submission', "
            "jsonb_build_object('practice_v1',jsonb_build_object('completion', "
            "data#>'{submission,practice_v1,completion}'))) "
            "from api_private.records where kind='quizzes' "
            "and data->>'question_set_id'=%s and id>%s order by id limit %s",
            (source.question_set_id, after, limit + 1),
        ).fetchall()
    else:
        with unit.repository.lock:
            rows = sorted(
                (
                    key,
                    copy.deepcopy({k: v for k, v in value.items() if k != "submission"})
                    | {
                        "submission": {
                            "practice_v1": {
                                "completion": copy.deepcopy(
                                    (value.get("submission") or {})
                                    .get("practice_v1", {})
                                    .get("completion")
                                )
                            }
                        }
                    },
                )
                for (owner, kind, key), value in unit.repository.data.items()
                if owner == unit.owner
                and kind == "quizzes"
                and key > after
                and value["question_set_id"] == source.question_set_id
            )[: limit + 1]
    items = []
    for key, value in rows[:limit]:
        sibling = StoredQuiz.model_validate(value | {"questions": source.questions})
        state = progress_of(sibling).model_dump(exclude={"answers", "next_index"})
        items.append(AttemptSummary(**state))
    return AttemptPage(items=items, next_cursor=rows[limit - 1][0] if len(rows) > limit else None)


@router.post("/{quiz_id}/review-deck", status_code=201, response_model=PublicDeck)
def review_deck(quiz_id: QuizId, body: ReviewDeckInput, key: Key):
    """Reuse checked question answers as cards; never generate new content."""
    source = quiz_for(quiz_id)
    data = metadata(source, write=True)
    receipts = data.setdefault("review_decks", {})
    saved = cached(receipts, key, body)
    if saved:
        return get_or_404(DECKS, saved["deck_id"], "Deck").public()
    capacity(receipts)
    deck = deck_from_quiz(source, body.which)
    DECKS[deck.id] = deck
    receipts[receipt_key(key)] = {"fingerprint": fingerprint(body), "deck_id": deck.id}
    return deck.public()


@router.get("/{quiz_id}/author-review", response_model=AuthorReview)
def author_review(quiz_id: QuizId):
    """Explicit answer-containing owner view, distinct from answer-free play and saved progress."""
    quiz = quiz_for(quiz_id)
    return AuthorReview(
        id=quiz.id,
        question_set_id=quiz.question_set_id,
        lesson_id=quiz.lesson_id,
        title=quiz.title,
        approved=quiz.approved,
        approval=quiz.approval,
        questions=[
            AuthorQuestion(
                index=q.index,
                type=q.type,
                question=q.question,
                options=q.options,
                answer_index=q.options.index(q.answer),
                explanation=q.explanation,
            )
            for q in quiz.questions
        ],
    )


@router.post("/{quiz_id}/revisions", status_code=201, response_model=QuizRevisionOutput)
def revise_questions(quiz_id: QuizId, body: QuizRevisionInput, key: Key):
    source = quiz_for(quiz_id)
    get_or_404(LESSONS, source.lesson_id, "Lesson")
    data = metadata(source, write=True)
    receipts = data.setdefault("author_revisions", {})
    saved = cached(receipts, key, body)
    if saved:
        quiz_for(saved["response"]["id"])
        return saved["response"]
    capacity(receipts)
    revision = (source.submission or {}).get("authoring_v1", {}).get("revision", 1) + 1
    questions = [
        IllustratedQuestion(
            index=i,
            type=q.type,
            question=q.question,
            options=q.options,
            answer=q.options[q.answer_index],
            explanation=q.explanation,
        )
        for i, q in enumerate(body.questions)
    ]
    revised = source.model_copy(
        deep=True,
        update={
            "id": new_id(),
            "question_set_id": new_id(),
            "title": body.title.strip(),
            "questions": questions,
            "attempts": {},
            "approved": False,
            "approval": None,
            "dropped": 0,
            "parent_id": source.id,
            "focus": [],
            "submission": {"authoring_v1": {"revision": revision, "previous_id": source.id}},
        },
    )
    QUIZZES[revised.id] = revised
    response = QuizRevisionOutput(
        id=revised.id,
        previous_id=source.id,
        question_set_id=revised.question_set_id,
        revision=revision,
    )
    receipts[receipt_key(key)] = {
        "fingerprint": fingerprint(body),
        "response": response.model_dump(),
    }
    return response


@router.post("/{quiz_id}/approve", response_model=QuizApprovalOutput)
def approve_questions(quiz_id: QuizId, body: QuizApprovalInput):
    quiz = quiz_for(quiz_id)
    if quiz.question_set_id != body.expected_question_set_id:
        raise HTTPException(409, "Question snapshot changed; review the current version.")
    quiz.approved = True
    quiz.approval = quiz.approval or {
        "reviewer_id": current_session.get().owner,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "kind": "owner_confirmation",
        "question_set_id": quiz.question_set_id,
    }
    return QuizApprovalOutput(
        id=quiz.id,
        question_set_id=quiz.question_set_id,
        approved=True,
        approval=quiz.approval,
    )


def install(app):
    app.include_router(router)
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            allowed = middleware.kwargs.setdefault("allow_headers", [])
            if "Idempotency-Key" not in allowed:
                allowed.append("Idempotency-Key")
            exposed = middleware.kwargs.setdefault("expose_headers", [])
            for header in ("X-Request-ID", "Retry-After"):
                if header not in exposed:
                    exposed.append(header)
