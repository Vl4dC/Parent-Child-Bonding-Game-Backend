import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest

from app.repository import PostgresRepository

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"


def test_pool_reuses_connections_and_resets_role_identity_and_readonly(repository):
    if not repository.durable:
        pytest.skip("Real connection pooling requires PostgreSQL")
    limited = PostgresRepository(repository.url, pool_size=1, control_pool_size=1)
    try:
        with limited.session(A) as unit:
            pid = unit.connection.info.backend_pid
            unit.put("profile", "self", {"display_name": "A"})
        with pytest.raises(psycopg.errors.DivisionByZero):
            with limited.session(A) as unit:
                assert unit.connection.info.backend_pid == pid
                unit.connection.execute("select 1/0")
        with limited.session(B, serialize=False) as unit:
            assert unit.connection.info.backend_pid == pid
            assert unit.get("profile", "self") is None
            assert unit.connection.execute("show transaction_read_only").fetchone()[0] == "on"
        with limited.session(B) as unit:
            assert unit.connection.execute("show transaction_read_only").fetchone()[0] == "off"
            unit.put("profile", "self", {"display_name": "B"})
        # Raw pool checkout proves SET LOCAL role/claims do not leak to the next borrower.
        with limited.pool.connection() as connection:
            role, claims = connection.execute(
                "select current_user,current_setting('request.jwt.claims',true)"
            ).fetchone()
            assert role != "authenticated" and claims in (None, "")
        assert limited.pool.get_stats()["connections_num"] == 1
    finally:
        limited.close()


def test_readonly_reads_do_not_wait_for_writer_row_lock(repository):
    if not repository.durable:
        pytest.skip("Real MVCC test requires PostgreSQL")
    with repository.session(A) as unit:
        unit.put("profile", "self", {"display_name": "before"})
    started, release = Event(), Event()

    def write():
        with repository.session(A) as unit:
            profile = unit.get("profile", "self")
            profile["display_name"] = "after"
            started.set()
            assert release.wait(10)

    def read():
        with repository.session(A, serialize=False) as unit:
            return unit.get("profile", "self")["display_name"]

    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(write)
        assert started.wait(5)
        try:
            assert pool.submit(read).result(timeout=2) == "before"
        finally:
            release.set()
            writer.result(timeout=5)
    with repository.session(A, serialize=False) as unit:
        assert unit.get("profile", "self")["display_name"] == "after"


def test_projections_and_progress_keep_ownership_and_pagination(repository):
    with repository.session(A) as unit:
        for i in range(3):
            unit.put(
                "decks",
                str(i),
                {
                    "title": None if i == 0 else "Deck",
                    "cards": ["private" * 1000],
                    "progress": {"reviews": i + 1},
                },
            )
        unit.put(
            "quizzes",
            "quiz",
            {
                "question_set_id": "qs",
                "questions": [],
                "attempts": {"0": {"correct": True}, "1": {"correct": False}},
            },
        )
        unit.put("completions", "lesson", {"lesson_id": "lesson"})
        unit.put(
            "tournaments",
            "t",
            {"question_set_id": "qs", "questions": [], "submission": {"score": 1}},
        )
        unit.put("jobs", "j", {"status": "queued", "input": {"private": "secret"}})
    with repository.session(B) as unit:
        unit.put("completions", "other", {"lesson_id": "other"})
    with repository.session(A, serialize=False) as unit:
        first = unit.summaries("decks", limit=2)
        assert first == [("0", {"title": None}), ("1", {"title": "Deck"})]
        assert unit.summaries("decks", after=first[-1][0], limit=2) == [("2", {"title": "Deck"})]
        assert "private" not in json.dumps(first)
        assert unit.pending_jobs() == 1
        assert unit.progress() == {
            "lessons_completed": 1,
            "quiz_answers": 2,
            "correct_answers": 1,
            "flashcard_reviews": 6,
            "tournaments_submitted": 1,
        }
    with repository.session(B, serialize=False) as unit:
        assert unit.summaries("decks") == [] and unit.pending_jobs() == 0
        assert unit.progress()["lessons_completed"] == 1


def test_sql_lock_and_statement_timeouts_release_connections(repository):
    if not repository.durable:
        pytest.skip("Real SQL timeout test requires PostgreSQL")
    limited = PostgresRepository(
        repository.url,
        pool_size=1,
        control_pool_size=1,
        lock_timeout_ms=100,
        statement_timeout_ms=1000,
    )
    try:
        with repository.session(A):
            with pytest.raises(psycopg.errors.LockNotAvailable):
                with limited.session(A):
                    pytest.fail("Owner lock should time out")
        with pytest.raises(psycopg.errors.QueryCanceled):
            with limited.session(B) as unit:
                unit.connection.execute("select pg_sleep(2)")
        with limited.session(B, serialize=False) as unit:
            assert unit.connection.execute("select 1").fetchone()[0] == 1
    finally:
        limited.close()


def test_lesson_library_summary_exposes_nested_title_without_lesson_text(repository):
    with repository.session(A) as unit:
        unit.put(
            "lessons",
            "nested-title",
            {"lesson": {"title": "Cells", "summary": "private body"}, "revision": 1},
        )
    with repository.session(A, serialize=False) as unit:
        assert unit.summaries("lessons") == [("nested-title", {"revision": 1, "title": "Cells"})]
