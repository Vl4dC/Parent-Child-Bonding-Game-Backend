"""
Tournament mode: placeholder for the gamified quiz.

For now this is the old exam flow, moved here so it stays separate from
learning/practice: answer every question, submit once, get a score.
Gamification (points, timers, streaks, leaderboards, how answers are revealed)
gets built inside this module; app.py only mounts the router.

Endpoints (prefix /api/tournament):
  POST /             {lesson_id, num_questions}  -> PublicQuiz
  GET  /{id}         -> PublicQuiz
  POST /{id}/submit  {answers: [choice, ...]}    -> SubmitOut
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.generation import generation_permit
from learning_service.api_docs import TOURNAMENT, TournamentId, errors
from learning_service.store import (
    LESSONS,
    TOURNAMENTS,
    CheckOut,
    PublicQuiz,
    get_or_404,
    grade,
    quiz_from_lesson,
)

router = APIRouter(prefix="/api/tournament", tags=[TOURNAMENT])


class CreateTournament(BaseModel):
    lesson_id: str = Field(..., description="The lesson `id` returned by `POST /api/lessons`")
    num_questions: int = Field(5, ge=1, le=15)


class SubmitIn(BaseModel):
    answers: list[int] = Field(
        ..., description="One option number (from 0) per question, in question order"
    )


class Result(CheckOut):
    index: int
    choice: int


class SubmitOut(BaseModel):
    score: int
    total: int
    results: list[Result]


CreateBody = Annotated[
    CreateTournament,
    Body(
        openapi_examples={
            "five": {
                "summary": "Five questions",
                "value": {"lesson_id": "<lesson id>", "num_questions": 5},
            }
        }
    ),
]
SubmitBody = Annotated[
    SubmitIn,
    Body(
        openapi_examples={
            "five": {
                "summary": "Five answers (all the first option)",
                "value": {"answers": [0, 0, 0, 0, 0]},
            }
        }
    ),
]


@router.post(
    "",
    status_code=201,
    summary="Create a tournament from a lesson",
    responses=errors(404, 422, 502),
    dependencies=[Depends(generation_permit, scope="function")],
)
def create_tournament(body: CreateBody) -> PublicQuiz:
    """Same questions as a practice quiz, but answered all at once: nothing is revealed until `submit`.
    Takes about 20-40 s."""
    lesson = get_or_404(LESSONS, body.lesson_id, "Lesson")
    if lesson.status != "approved":
        raise HTTPException(409, "Approve the lesson before creating a scored quiz.")
    stored = quiz_from_lesson(lesson, body.num_questions)
    TOURNAMENTS[stored.id] = stored
    return stored.public()


@router.get("/{tournament_id}", summary="Get a tournament", responses=errors(404))
def get_tournament(tournament_id: TournamentId) -> PublicQuiz:
    """The questions and options, without answers."""
    return get_or_404(TOURNAMENTS, tournament_id, "Tournament").public()


@router.post(
    "/{tournament_id}/submit", summary="Submit all answers and get the score", responses=errors(404)
)
def submit(tournament_id: TournamentId, body: SubmitBody) -> SubmitOut:
    """Score every answer and reveal the correct options and explanations. 422 if the number of answers differs
    from the number of questions, or an option number is out of range."""
    stored = get_or_404(TOURNAMENTS, tournament_id, "Tournament")
    if not stored.approved:
        raise HTTPException(409, "Review and approve the question set before scoring.")
    if stored.submission is not None:
        if stored.submission["answers"] != body.answers:
            raise HTTPException(409, "This attempt has already been submitted.")
        return SubmitOut.model_validate(stored.submission["result"])
    if len(body.answers) != len(stored.questions):
        raise HTTPException(
            422, f"expected {len(stored.questions)} answers, got {len(body.answers)}"
        )
    results = [
        Result(index=q.index, choice=choice, **grade(q, choice).model_dump())
        for q, choice in zip(stored.questions, body.answers)
    ]
    out = SubmitOut(score=sum(r.correct for r in results), total=len(results), results=results)
    stored.submission = {"answers": body.answers, "result": out.model_dump(mode="json")}
    return out


class DuelIn(BaseModel):
    model_config = {"extra": "forbid"}
    answers: list[list[Annotated[int, Field(strict=True, ge=0)]]] = Field(
        min_length=2,
        max_length=2,
        description="Two complete answer sheets. Sheet 0 belongs to the selected wallet; sheet 1 is the local guest.",
    )


class DuelOut(BaseModel):
    players: list[SubmitOut]
    completed_at: str
    reward_coins: int


DuelBody = Annotated[
    DuelIn,
    Body(
        openapi_examples={
            "two_players": {
                "summary": "Both players answer a two-question duel",
                "value": {"answers": [[1, 0], [0, 1]]},
            },
        }
    ),
]


@router.post("/{tournament_id}/duel", response_model=DuelOut, responses=errors(404, 409, 422))
def submit_duel(tournament_id: TournamentId, body: DuelBody):
    """Freeze both local players' answers before revealing either result. Identical replays are free.
    Use /api/me/coins/claim-duel afterwards to collect player 0's reward."""
    from app.economy import clock, duel_reward, record

    stored = get_or_404(TOURNAMENTS, tournament_id, "Tournament")
    if not stored.approved:
        raise HTTPException(409, "Review and approve the question set before scoring.")
    if stored.submission is not None:
        duel = stored.submission.get("duel")
        if duel and duel["answers"] == body.answers:
            return duel["result"]
        raise HTTPException(409, "This tournament has already been submitted.")
    players = []
    for sheet in body.answers:
        if len(sheet) != len(stored.questions):
            raise HTTPException(422, "Each player must answer every question.")
        results = [
            Result(index=q.index, choice=choice, **grade(q, choice).model_dump())
            for q, choice in zip(stored.questions, sheet)
        ]
        players.append(
            SubmitOut(score=sum(r.correct for r in results), total=len(results), results=results)
        )
    at = clock()
    out = DuelOut(
        players=players, completed_at=at.isoformat(), reward_coins=duel_reward(players[0].score)
    )
    stored.submission = {
        "answers": body.answers[0],
        "result": players[0].model_dump(mode="json"),
        "duel": {"answers": body.answers, "result": out.model_dump(mode="json")},
    }
    record("duel", f"tournament/{stored.id}", at)
    return out
