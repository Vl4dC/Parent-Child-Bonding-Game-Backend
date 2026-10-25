"""The weekly challenge preview (show only): which topic comes when, what the preview says, and the cached sample
content the AI writes for it. The AI is faked; the calendar, the cache, the safety checks and the routes are real."""

import json
from datetime import date

import pytest

import learning_service.challenges as ch
import learning_service.weekly as weekly
from tests.learning.test_safety_api import BAD, OK, assert_blocked, scripted

SUNDAY = date(2026, 10, 25)  # the last day of ISO week 2026-W43


@pytest.fixture
def day(monkeypatch):
    monkeypatch.setattr(weekly, "today", lambda: SUNDAY)
    return SUNDAY


@pytest.fixture
def store(client):
    return client.app.state.memory


# ---------------------------------------------------------------- the calendar
def test_a_week_runs_monday_to_sunday_and_next_is_seven_days_on():
    this = weekly.week_of(SUNDAY, "current")
    following = weekly.week_of(SUNDAY, "next")
    assert (this.key, this.starts_on, this.ends_on) == ("2026-W43", "2026-10-19", "2026-10-25")
    assert (following.key, following.starts_on, following.ends_on) == (
        "2026-W44",
        "2026-10-26",
        "2026-11-01",
    )
    assert (
        weekly.week_of(date(2026, 10, 26), "current").key == "2026-W44"
    )  # Monday starts a new week


def test_the_year_end_uses_the_iso_week():
    assert weekly.week_of(date(2026, 12, 31), "next").key == "2027-W01"


def test_topics_rotate_so_neighbouring_weeks_differ_and_everything_comes_round():
    seen = []
    day = date(2026, 1, 5)
    for n in range(len(weekly.TOPICS)):
        from datetime import timedelta

        seen.append(weekly.topic_of(weekly.week_of(day + timedelta(weeks=n), "current"))[0])
    assert len(set(seen)) == len(weekly.TOPICS) and all(a != b for a, b in zip(seen, seen[1:]))
    assert weekly.topic_of(weekly.week_of(SUNDAY, "next")) == weekly.topic_of(
        weekly.week_of(SUNDAY, "next")
    )


def test_every_topic_is_a_general_school_topic_with_a_subject_and_a_blurb():
    subjects = {
        "Science",
        "Math",
        "History",
        "Geography",
        "Languages",
        "Computing",
        "Arts",
        "Other",
    }
    for topic, subject, blurb in weekly.TOPICS:
        assert topic and blurb.endswith(".") and subject in subjects


# ---------------------------------------------------------------- the preview
def test_the_preview_says_what_next_week_is_and_that_it_is_only_a_preview(client, day):
    body = client.get("/api/weekly/next").json()
    assert (
        body["preview"] is True
        and "Preview only" in body["notice"]
        and body["label"].startswith("Written by AI")
    )
    assert (
        body["which"] == "next"
        and body["week"]["key"] == "2026-W44"
        and body["band"] == "10-12"
        and body["age"] == 11
    )
    assert body["topic"] and body["subject"] and body["blurb"]
    assert [g["game"] for g in body["games"]] == ["mcq", "heads_up", "diagram"]
    assert [m["mode"] for m in body["modes"]] == ["solo", "friends", "parent_child", "duo"]
    assert any("two thirds" in rule for rule in body["rules"])
    assert body["samples_ready"] == {"mcq": False, "heads_up": False, "diagram": False}
    assert body["samples"] == {"mcq": None, "heads_up": None, "diagram": None}


def test_the_example_leaderboard_is_made_up_and_says_so(client, day):
    board = client.get("/api/weekly/next").json()["example_leaderboard"]
    assert len(board) == 5 and all(row["example"] is True for row in board)
    assert [row["rank"] for row in board] == [1, 2, 3, 4, 5]


def test_current_and_next_are_different_weeks_and_each_band_has_its_age(client, day):
    this, following = (
        client.get("/api/weekly/current").json(),
        client.get("/api/weekly/next").json(),
    )
    assert (this["which"], this["week"]["key"]) == ("current", "2026-W43") and this[
        "topic"
    ] != following["topic"]
    ages = {
        band: client.get("/api/weekly/next", params={"band": band}).json()["age"]
        for band in weekly.BANDS
    }
    assert ages == {"10-12": 11, "13-15": 14, "16-18": 17}
    assert client.get("/api/weekly/next", params={"band": "5-7"}).status_code == 422


# ---------------------------------------------------------------- the sample content
def prepared(client, **body):
    r = client.post("/api/weekly/next/prepare", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_preparing_writes_a_taste_of_each_game_without_any_answers(client, day, models):
    body = prepared(client, band="10-12")
    assert body["samples_ready"] == {"mcq": True, "heads_up": True, "diagram": True}
    mcq, heads, diagram = (body["samples"][g] for g in ("mcq", "heads_up", "diagram"))
    assert mcq["count"] == 5 and set(mcq["questions"][0]) == {"index", "question", "options"}
    assert "answer" not in json.dumps(mcq) and "Because" not in json.dumps(mcq)
    assert heads["count"] == 8 and len(heads["cards"]) == weekly.SAMPLE_CARDS
    assert (
        all(3 <= len(c["forbidden"]) <= 5 for c in heads["cards"])
        and heads["seconds_per_round"] == 60
    )
    assert len(diagram["slots"]) == 4 and "?" in diagram["mermaid"] and "answers" not in diagram
    assert diagram["wrong_finish_penalty_seconds"] == 10


def test_the_samples_are_written_for_the_leagues_age_and_the_weeks_topic(client, day, models):
    topic = client.get("/api/weekly/next").json()["topic"]
    prepared(client, band="13-15")
    assert models.quizzes[0].age == 14 and topic in models.quizzes[0].prompt
    assert "14-year-old" in models.decks[0] and topic in models.decks[0]
    assert "14-year-old" in models.flows[0] and topic in models.flows[0]
    assert (
        models.quizzes[0].num_questions == 5 + ch.MCQ_SPARE
    )  # 5 for the sample plus spare for the ones that do not fit


def test_samples_are_cached_for_the_week_so_showing_the_preview_again_costs_nothing(
    client, day, models
):
    prepared(client, band="10-12")
    calls = models.calls
    again = prepared(client, band="10-12")
    assert models.calls == calls and again["samples_ready"]["mcq"] is True
    assert (
        client.get("/api/weekly/next").json()["samples"]["mcq"] is not None
    )  # GET never generates
    assert models.calls == calls


def test_preparing_one_game_or_one_band_leaves_the_others_alone(client, day, models):
    only = prepared(client, band="13-15", games=["mcq"])
    assert only["samples_ready"] == {"mcq": True, "heads_up": False, "diagram": False}
    assert (len(models.decks), len(models.flows)) == (0, 0)
    assert (
        client.get("/api/weekly/next", params={"band": "10-12"}).json()["samples_ready"]["mcq"]
        is False
    )
    rest = prepared(client, band="13-15", games=["heads_up", "diagram"])
    assert rest["samples_ready"] == {"mcq": True, "heads_up": True, "diagram": True}
    assert len(models.quizzes) == 1  # the quiz was not written twice


def test_this_weeks_samples_are_not_next_weeks(client, day, models):
    prepared(client, band="10-12")
    assert client.get("/api/weekly/current").json()["samples_ready"]["mcq"] is False


def test_unsuitable_sample_content_is_blocked_and_never_cached(client, day, models, monkeypatch):
    scripted(monkeypatch, lambda n, kind: BAD if kind == "content" else OK)
    r = client.post("/api/weekly/next/prepare", json={"band": "10-12", "games": ["heads_up"]})
    assert_blocked(r)
    assert client.get("/api/weekly/next").json()["samples_ready"]["heads_up"] is False


@pytest.mark.parametrize("fail", ["quiz", "deck", "flow"])
def test_a_model_failure_is_a_502_without_the_provider_message(client, day, models, fail):
    models.fail = fail
    r = client.post("/api/weekly/next/prepare", json={"band": "10-12"})
    assert r.status_code == 502 and "sk-secret-123" not in r.text


def test_preparing_is_off_when_generation_is_disabled(client, day, models):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    app = create_app(
        Settings(app_env="test", demo_mode=True, generation_enabled=False, _env_file=None),
        repository=client.app.state.repository,
    )
    with TestClient(app) as off:
        assert off.post("/api/weekly/next/prepare", json={}).status_code == 503
        assert off.get("/api/weekly/next").status_code == 200  # looking is always free
    assert models.calls == 0


def test_the_preview_survives_a_restart(
    client, tmp_path, day, models
):  # `client` fakes the safety checker
    from learning_service.memory_store import MemoryStore

    path = str(tmp_path / "memory.db")
    first = MemoryStore(path)
    weekly.prepare(first, "next", "10-12", ["mcq"])
    first.close()
    again = MemoryStore(path)
    assert weekly.preview(again, "next", "10-12")["samples_ready"]["mcq"] is True
