"""Server-owned activity receipts; committed with the learning action, never client counters."""

import hashlib
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.repository import current_session
from learning_service.home_rules import RULES

PARTICIPATION_COINS = 20
ZONE = ZoneInfo("Europe/Berlin")
QUESTS = (
    ("d-lesson", "Finish 1 lesson", "daily", "lesson", 1, 60),
    ("d-quiz", "Answer 5 quiz questions", "daily", "quiz_answer", 5, 50),
    ("d-cards", "Review 8 flashcards", "daily", "card_review", 8, 40),
    ("d-duel", "Play 1 family duel", "daily", "duel", 1, 40),
    ("w-duels", "Play 3 family duels", "weekly", "duel", 3, 250),
    ("w-lessons", "Finish 5 lessons", "weekly", "lesson", 5, 200),
    ("w-weak", "Practise your weak spots twice", "weekly", "weak", 2, 150),
    ("w-upload", "Learn from 3 uploads", "weekly", "upload_learned", 3, 120),
)


def duel_reward(correct: int) -> int:
    return correct * RULES.quiz_coins_per_correct + PARTICIPATION_COINS


def clock() -> datetime:
    return datetime.now(timezone.utc)


def period(kind: str, at: datetime) -> tuple[str, datetime, datetime]:
    day = at.astimezone(ZONE).date()
    if kind == "weekly":
        day -= timedelta(days=day.weekday())
    start = datetime.combine(day, time(), ZONE)
    end = datetime.combine(day + timedelta(days=7 if kind == "weekly" else 1), time(), ZONE)
    return day.isoformat(), start, end


def record(kind: str, ref: str, at: datetime | None = None) -> None:
    unit = current_session.get()
    key = hashlib.sha256(f"{kind}:{ref}".encode()).hexdigest()
    if unit.get("economy_events", key) is None:
        unit.put(
            "economy_events",
            key,
            {
                "kind": kind,
                "at": (at or clock()).astimezone(timezone.utc).isoformat(),
            },
        )


def quiz_answered(quiz, index: int) -> None:
    record("quiz_answer", f"{quiz.id}/{index}")
    origin = (quiz.submission or {}).get("practice_origin", {})
    if len(quiz.attempts) == len(quiz.questions) and (
        quiz.focus or origin.get("which") == "missed"
    ):
        record("weak", f"quiz/{quiz.id}")


def counts(at: datetime, home=None) -> dict[tuple[str, str], int]:
    """Only committed activity is eligible. POST claims cannot count their own pending writes."""
    unit = current_session.get()
    bounds = {kind: period(kind, at) for kind in ("daily", "weekly")}
    earliest = bounds["weekly"][1].astimezone(timezone.utc).isoformat()
    result = {}
    if unit.connection:
        rows = unit.connection.execute(
            "select data from api_private.records where kind='economy_events' "
            "and data->>'at' >= %s",
            (earliest,),
        ).fetchall()
        events = (row[0] for row in rows)
    else:
        with unit.repository.lock:
            events = [
                dict(data)
                for (owner, kind, _), data in unit.repository.data.items()
                if owner == unit.owner and kind == "economy_events" and data["at"] >= earliest
            ]
    if home is not None:
        from itertools import chain

        extra = home._query(
            "select kind,at from live_social_events where player_id=? and at>=?",
            (unit.owner, earliest),
        )
        events = chain(events, (dict(row) for row in extra))
    for event in events:
        occurred = datetime.fromisoformat(event["at"])
        for kind, (_, start, end) in bounds.items():
            if start <= occurred.astimezone(ZONE) < end and occurred <= at:
                key = (kind, event["kind"])
                result[key] = result.get(key, 0) + 1
    return result
