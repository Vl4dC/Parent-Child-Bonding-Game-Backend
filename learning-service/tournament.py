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

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from api_docs import TOURNAMENT, TournamentId, errors
from store import LESSONS, TOURNAMENTS, CheckOut, PublicQuiz, get_or_404, grade, quiz_from_lesson

router = APIRouter(prefix="/api/tournament", tags=[TOURNAMENT])


class CreateTournament(BaseModel):
    lesson_id: str = Field(..., description="The lesson `id` returned by `POST /api/lessons`")
    num_questions: int = Field(5, ge=1, le=15)


class SubmitIn(BaseModel):
    answers: list[int] = Field(..., description="One option number (from 0) per question, in question order")


class Result(CheckOut):
    index: int
    choice: int


class SubmitOut(BaseModel):
    score: int
    total: int
    results: list[Result]


CreateBody = Annotated[CreateTournament, Body(openapi_examples={
    "five": {"summary": "Five questions", "value": {"lesson_id": "<lesson id>", "num_questions": 5}}})]
SubmitBody = Annotated[SubmitIn, Body(openapi_examples={
    "five": {"summary": "Five answers (all the first option)", "value": {"answers": [0, 0, 0, 0, 0]}}})]


@router.post("", status_code=201, summary="Create a tournament from a lesson", responses=errors(404, 502))
def create_tournament(body: CreateBody) -> PublicQuiz:
    """Same questions as a practice quiz, but answered all at once: nothing is revealed until `submit`.
    Takes about 20-40 s."""
    lesson = get_or_404(LESSONS, body.lesson_id, "Lesson")
    stored = quiz_from_lesson(lesson, body.num_questions)
    TOURNAMENTS[stored.id] = stored
    return stored.public()


@router.get("/{tournament_id}", summary="Get a tournament", responses=errors(404))
def get_tournament(tournament_id: TournamentId) -> PublicQuiz:
    """The questions and options, without answers."""
    return get_or_404(TOURNAMENTS, tournament_id, "Tournament").public()


@router.post("/{tournament_id}/submit", summary="Submit all answers and get the score", responses=errors(404))
def submit(tournament_id: TournamentId, body: SubmitBody) -> SubmitOut:
    """Score every answer and reveal the correct options and explanations. 422 if the number of answers differs
    from the number of questions, or an option number is out of range."""
    stored = get_or_404(TOURNAMENTS, tournament_id, "Tournament")
    if len(body.answers) != len(stored.questions):
        raise HTTPException(422, f"expected {len(stored.questions)} answers, got {len(body.answers)}")
    results = [
        Result(index=q.index, choice=choice, **grade(q, choice).model_dump())
        for q, choice in zip(stored.questions, body.answers)
    ]
    return SubmitOut(score=sum(r.correct for r in results), total=len(results), results=results)
