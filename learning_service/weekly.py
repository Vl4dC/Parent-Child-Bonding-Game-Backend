"""
The weekly challenge: what next week's tournament will be. SHOW ONLY for now.

Every week has one common school topic that needs nobody's memory, and the same three games for everyone, in leagues
by age band. Today this is only a preview, to show where the product is going: there is no sign-up, no scoring and no
real leaderboard (the example one is made up and labelled so). The sample content is real: the AI writes a few quiz
questions, a heads-up deck and a fill-in-the-blanks diagram for the topic, checked for the age band and cached for the
week, so it costs one generation per game per band per week no matter how often the preview is shown.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

import learning_service.challenges as ch
from learning_service.memory_store import MemoryStore
from learning_service.safety import ensure_safe

log = logging.getLogger("weekly")

Band = Literal["10-12", "13-15", "16-18"]
BANDS: dict[str, int] = {
    "10-12": 11,
    "13-15": 14,
    "16-18": 17,
}  # the age each league's content is written for
Which = Literal["current", "next"]
GAMES = ("mcq", "heads_up", "diagram")
SAMPLE_CARDS = 4  # heads-up terms shown in the preview

# (topic, subject, one-line blurb): common school and general topics, so nobody needs to have studied them
TOPICS = [
    ("The Solar System", "Science", "Planets, moons and the Sun: how our corner of space works."),
    ("Fractions and Percentages", "Math", "Parts of a whole, from pizza slices to sale prices."),
    ("The Water Cycle", "Science", "How water travels from the sea to the clouds and back."),
    ("Ancient Egypt", "History", "Pharaohs, pyramids and life beside the Nile."),
    ("Continents and Oceans", "Geography", "Find your way around the whole world."),
    ("How the Internet Works", "Computing", "From a click on your phone to a page on screen."),
    ("The Human Body: Heart and Lungs", "Science", "How your body moves oxygen and blood."),
    ("Shapes, Angles and Symmetry", "Math", "The geometry hiding in buildings, nature and games."),
    (
        "Rainforests and Biodiversity",
        "Geography",
        "Why the busiest places on Earth need protecting.",
    ),
    ("The Industrial Revolution", "History", "How machines and factories changed everyday life."),
    ("Electricity and Magnets", "Science", "Circuits, currents and the invisible pull of magnets."),
    ("Famous Inventions", "History", "The ideas and inventors that changed the world."),
]

GAME_INFO = {
    "mcq": {
        "title": "Quick Quiz",
        "tagline": "Ten questions, a timer on each. Most correct wins; less total time breaks ties.",
    },
    "heads_up": {
        "title": "Heads Up!",
        "tagline": "Phone on your forehead: your partner explains the word without saying the forbidden ones.",
    },
    "diagram": {
        "title": "Diagram Race",
        "tagline": "Drag the missing words back into the diagram. First perfect finish wins; each mistake costs 10 seconds.",
    },
}

MODE_INFO = [
    {"mode": "solo", "title": "Solo", "how": "Beat the clock and your own best score."},
    {
        "mode": "friends",
        "title": "Child vs friend",
        "how": "Same questions at the same time, each for themselves.",
    },
    {
        "mode": "parent_child",
        "title": "Parent vs child",
        "how": "The parent gets two thirds of the time, so it is a fair fight.",
    },
    {
        "mode": "duo",
        "title": "Duo battle",
        "how": "Two parent-child pairs, two teams: points are added up per team.",
    },
]

RULES = [
    "One common topic each week, picked from school subjects: nobody needs to have studied it first.",
    "Everyone in your age league gets exactly the same content.",
    "Games are timed by the server's clock, so it is fair even on different phones.",
    "Content is written by AI for casual play and is not reviewed by a person.",
    "In parent-child and duo matches, parents get two thirds of the time.",
]

EXAMPLE_LEADERBOARD = [  # made up, to show what a leaderboard will look like
    {"rank": 1, "name": "Maya & Dad", "mode": "parent_child", "score": 27},
    {"rank": 2, "name": "Team Sunflower", "mode": "duo", "score": 25},
    {"rank": 3, "name": "Leo", "mode": "solo", "score": 24},
    {"rank": 4, "name": "Nora & Sam", "mode": "friends", "score": 22},
    {"rank": 5, "name": "Chen & Mum", "mode": "parent_child", "score": 21},
]


def today() -> date:
    """The date in UTC. A seam: tests replace it."""
    return datetime.now(timezone.utc).date()


class Week(BaseModel):
    key: str = Field(..., description="Like 2026-W44")
    starts_on: str
    ends_on: str


def week_of(day: date, which: str) -> Week:
    target = day + timedelta(days=7 if which == "next" else 0)
    year, number, _ = target.isocalendar()
    monday = target - timedelta(days=target.weekday())
    return Week(
        key=f"{year}-W{number:02d}",
        starts_on=monday.isoformat(),
        ends_on=(monday + timedelta(days=6)).isoformat(),
    )


def topic_of(week: Week) -> tuple[str, str, str]:
    """The week's topic, rotating through the list so neighbouring weeks differ."""
    year, number = week.key.split("-W")
    return TOPICS[(int(year) * 53 + int(number)) % len(TOPICS)]


# --------------------------------------------------------------------------
# Sample content
# --------------------------------------------------------------------------
def sample_content(game: str, topic: str, age: int, week: Week, band: str):
    """Write (once) the sample for one game, checked for the age band."""
    seed = int(week.key.replace("-W", "")) * 7 + list(BANDS).index(band)
    count = {"mcq": 5, "heads_up": 8}.get(game)
    content, text_of = ch.build_content(game, topic, age, count, None, seed)
    return ensure_safe(lambda: content, text_of, age, topic)


def sample_view(game: str, content: dict) -> dict:
    """What the preview shows of a sample: no answers, and only a few heads-up cards."""
    if game == "mcq":
        return ch.mcq_public(ch.McqContent(**content)).model_dump()
    if game == "heads_up":
        deck = ch.HeadsUpContent(**content)
        return {
            "count": len(deck.terms),
            "seconds_per_round": deck.seconds_per_round,
            "cards": [c.model_dump() for c in deck.terms[:SAMPLE_CARDS]],
        }
    return ch.diagram_public(ch.DiagramContent(**content)).model_dump()


def prepare(store: MemoryStore, which: str, band: str, games: list[str] | None = None) -> list[str]:
    """Write whichever samples this week and band do not have yet. Returns the games that were written."""
    week = week_of(today(), which)
    topic, _, _ = topic_of(week)
    written = []
    for game in games or GAMES:
        if store.get_weekly_sample(week.key, band, game) is not None:
            continue
        content = sample_content(game, topic, BANDS[band], week, band)
        store.save_weekly_sample(week.key, band, game, content.model_dump())
        written.append(game)
    return written


def preview(store: MemoryStore, which: str, band: str) -> dict:
    week = week_of(today(), which)
    topic, subject, blurb = topic_of(week)
    samples = {g: store.get_weekly_sample(week.key, band, g) for g in GAMES}
    return {
        "preview": True,
        "notice": "Preview only: weekly play is not open yet. The leaderboard below is an example, not real results.",
        "label": ch.CASUAL_LABEL,
        "which": which,
        "week": week.model_dump(),
        "band": band,
        "age": BANDS[band],
        "topic": topic,
        "subject": subject,
        "blurb": blurb,
        "games": [{"game": g, **GAME_INFO[g]} for g in GAMES],
        "modes": MODE_INFO,
        "rules": RULES,
        "samples_ready": {g: samples[g] is not None for g in GAMES},
        "samples": {g: (sample_view(g, c) if c is not None else None) for g, c in samples.items()},
        "example_leaderboard": [row | {"example": True} for row in EXAMPLE_LEADERBOARD],
    }
