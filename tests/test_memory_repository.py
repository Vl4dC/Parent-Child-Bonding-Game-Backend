"""The in-memory (demo) store: which concurrent writes conflict. PostgreSQL serialises an owner's requests with
row locks instead, so these rules are about MemoryRepository only."""

import pytest
from fastapi import HTTPException

from app.repository import MemoryRepository

OWNER = "00000000-0000-4000-8000-000000000001"


def repository_with_a_lesson():
    repo = MemoryRepository()
    with repo.session(OWNER) as unit:
        unit.put("lessons", "L", {"visuals": []})
    return repo


def test_a_record_that_was_only_read_never_conflicts():
    """A quiz request reads the lesson while another request draws its diagrams: the quiz must still be saved."""
    repo = repository_with_a_lesson()
    with repo.session(OWNER) as quiz_request:
        assert quiz_request.get("lessons", "L") == {"visuals": []}  # read only
        with repo.session(OWNER) as diagrams_request:
            diagrams_request.put("lessons", "L", {"visuals": ["drawn"]})
        quiz_request.put(
            "quizzes", "Q", {"lesson_id": "L", "question_set_id": "S", "questions": []}
        )
    with repo.session(OWNER) as check:
        assert check.get("lessons", "L") == {
            "visuals": ["drawn"]
        }  # the other request's write survives
        assert check.get("quizzes", "Q")["lesson_id"] == "L"


def test_two_requests_changing_the_same_record_still_conflict():
    repo = repository_with_a_lesson()
    with pytest.raises(HTTPException) as conflict:
        with repo.session(OWNER) as first:
            first.get("lessons", "L")
            first.put("lessons", "L", {"visuals": ["from the first"]})
            with repo.session(OWNER) as second:
                second.put("lessons", "L", {"visuals": ["from the second"]})
    assert conflict.value.status_code == 409 and "Concurrent update" in conflict.value.detail
    with repo.session(OWNER) as check:
        assert check.get("lessons", "L") == {"visuals": ["from the second"]}


def test_writing_what_another_request_already_wrote_is_not_a_conflict():
    repo = repository_with_a_lesson()
    with repo.session(OWNER) as first:
        first.get("lessons", "L")
        with repo.session(OWNER) as second:
            second.put("lessons", "L", {"visuals": ["same"]})
        first.put("lessons", "L", {"visuals": ["same"]})
