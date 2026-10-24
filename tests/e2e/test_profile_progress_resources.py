"""Profile, progress, resource listing and account-data deletion under awkward input."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.e2e.helpers import PARENT, approval, child_headers, create_quiz, keys_anywhere

DEFAULT_PROFILE = {
    "display_name": "Learner",
    "daily_goal": 15,
    "avatar_reference": None,
    "preferences": {
        "theme": "light",
        "text": "md",
        "contrast": "normal",
        "motion": "full",
        "captions": True,
        "read_aloud": False,
    },
}


# ---------------------------------------------------------------- profile
def test_a_new_account_gets_the_documented_defaults(api):
    assert api.get("/api/me/profile").json() == DEFAULT_PROFILE


def test_put_replaces_the_whole_profile_not_a_merge(api):
    api.put(
        "/api/me/profile",
        json={
            "display_name": "Alex",
            "daily_goal": 60,
            "avatar_reference": "fox",
            "preferences": {"theme": "dark"},
        },
    )
    api.put("/api/me/profile", json={"display_name": "Alex"})
    assert api.get("/api/me/profile").json() == {**DEFAULT_PROFILE, "display_name": "Alex"}


@pytest.mark.parametrize(
    "body,ok",
    [
        ({"display_name": "a"}, True),
        ({"display_name": "a" * 60}, True),
        ({"display_name": "a" * 61}, False),
        ({"display_name": ""}, False),
        ({"display_name": " \t\n "}, False),
        ({"display_name": "🦊" * 60}, True),
        ({"display_name": "🦊" * 61}, False),
        ({"display_name": "שלום עולם"}, True),
        ({"display_name": None}, False),
        ({"display_name": ["Alex"]}, False),
        ({"daily_goal": 1}, True),
        ({"daily_goal": 120}, True),
        ({"daily_goal": 0}, False),
        ({"daily_goal": 121}, False),
        ({"daily_goal": -5}, False),
        ({"daily_goal": 15.5}, False),
        ({"daily_goal": None}, False),
        ({"avatar_reference": "a" * 128}, True),
        ({"avatar_reference": "a" * 129}, False),
        ({"avatar_reference": None}, True),
        ({"avatar_reference": 5}, False),
        ({"preferences": {"text": "xxl"}}, False),
        ({"preferences": {"captions": "maybe"}}, False),
        ({"preferences": {"readAloud": True}}, False),
        ({"preferences": {"motion": "reduce", "contrast": "more", "text": "xl"}}, True),
        ({"preferences": None}, False),
        ({"preferences": []}, False),
        ({"user_id": PARENT}, False),
        ({"owner_id": PARENT}, False),
        ({"account_type": "admin"}, False),
        ({"role": "admin"}, False),
    ],
)
def test_profile_boundaries(api, body, ok):
    assert (api.put("/api/me/profile", json=body).status_code == 200) is ok


def test_a_rejected_profile_write_changes_nothing(api):
    api.put("/api/me/profile", json={"display_name": "Kept", "daily_goal": 30})
    assert (
        api.put("/api/me/profile", json={"display_name": "Lost", "daily_goal": 999}).status_code
        == 422
    )
    assert api.get("/api/me/profile").json()["display_name"] == "Kept"


def test_the_display_name_is_trimmed_but_otherwise_stored_exactly(api):
    name = "  Zoë  O'Neil-Ünal ‮ rtl "
    stored = api.put("/api/me/profile", json={"display_name": name}).json()["display_name"]
    assert stored == name.strip() and api.get("/api/me/profile").json()["display_name"] == stored


def test_markup_in_text_fields_is_returned_as_inert_json_text(api):
    payload = "<img src=x onerror=alert(1)>"
    api.put(
        "/api/me/profile", json={"display_name": payload, "avatar_reference": "javascript:alert(1)"}
    )
    r = api.get("/api/me/profile")
    assert r.headers["content-type"] == "application/json"
    assert (
        r.json()["display_name"] == payload
        and r.json()["avatar_reference"] == "javascript:alert(1)"
    )


def test_concurrent_profile_writes_leave_one_whole_profile(api):
    names = [f"Writer {n}" for n in range(8)]

    def write(name):
        return api.put(
            "/api/me/profile", json={"display_name": name, "daily_goal": 10 + len(name)}
        ).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(write, names))
    assert set(codes) <= {200, 409} and 200 in codes
    final = api.get("/api/me/profile").json()
    assert final["display_name"] in names and final["daily_goal"] == 10 + len(final["display_name"])


def test_profiles_are_private_to_each_account(api):
    api.put("/api/me/profile", json={"display_name": "Parent"})
    assert api.get("/api/me/profile", headers=child_headers(api)).json() == DEFAULT_PROFILE


# ---------------------------------------------------------------- progress
def test_a_fresh_account_has_zero_progress_and_nothing_else(api):
    progress = api.get("/api/me/progress").json()
    assert progress == {
        "lessons_completed": 0,
        "quiz_answers": 0,
        "correct_answers": 0,
        "flashcard_reviews": 0,
        "tournaments_submitted": 0,
    }


def test_progress_never_contains_rewards_or_inferred_mastery(api, lesson_id):
    api.post(f"/api/lessons/{lesson_id}/complete")
    banned = {"coins", "xp", "points", "badges", "streak", "mastery", "level", "rank", "time_spent"}
    assert not banned & keys_anywhere(api.get("/api/me/progress").json())


def test_progress_counts_only_first_answers_and_each_review_once(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    for choice in (0, 1, 2, 3):  # question 0: first answer wrong, later ones right
        api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": choice})
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 1, "choice": 0})
    assert api.get("/api/me/progress").json()["quiz_answers"] == 2
    assert api.get("/api/me/progress").json()["correct_answers"] == 0
    deck = api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}).json()
    for card in range(3):
        api.post(f"/api/flashcards/{deck['id']}/review", json={"card": card, "result": "know"})
    api.post(
        f"/api/flashcards/{deck['id']}/review", json={"card": 0, "result": "know"}
    )  # refused: already known
    assert api.get("/api/me/progress").json()["flashcard_reviews"] == 3


def test_new_attempts_add_answers_but_not_double_counted_questions(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    second = api.post(f"/api/quiz/{quiz['id']}/attempts").json()
    for attempt in (quiz, second):
        api.post(f"/api/quiz/{attempt['id']}/check", json={"index": 0, "choice": 1})
        api.post(f"/api/quiz/{attempt['id']}/check", json={"index": 0, "choice": 1})
    assert api.get("/api/me/progress").json()["quiz_answers"] == 2


def test_progress_is_independent_per_account(api, lesson_id):
    api.post(f"/api/lessons/{lesson_id}/complete")
    assert api.get("/api/me/progress", headers=child_headers(api)).json()["lessons_completed"] == 0


# ---------------------------------------------------------------- listings
def make_lessons(api, count):
    return [
        api.post("/api/lessons", json={"topic": f"topic {n}", "age": 12}).json()["id"]
        for n in range(count)
    ]


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 100])
def test_paging_visits_every_item_once_in_ascending_order(api, limit):
    created = sorted(make_lessons(api, 5))
    seen, cursor = [], ""
    for _ in range(10):
        page = api.get("/api/resources/lessons", params={"limit": limit, "after": cursor}).json()
        assert len(page["items"]) <= limit
        seen += [item["id"] for item in page["items"]]
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert seen == created


def test_paging_edges(api):
    ids = sorted(make_lessons(api, 3))
    assert api.get("/api/resources/lessons", params={"after": ids[-1]}).json() == {
        "items": [],
        "next_cursor": None,
    }
    assert [
        i["id"] for i in api.get("/api/resources/lessons", params={"after": ""}).json()["items"]
    ] == ids
    exact = api.get("/api/resources/lessons", params={"limit": 3}).json()
    assert len(exact["items"]) == 3 and exact["next_cursor"] is None  # no phantom extra page
    for bad in (0, 101, -1, "abc", "1.5"):
        assert api.get("/api/resources/lessons", params={"limit": bad}).status_code == 422
    assert api.get("/api/resources/lessons", params={"after": "' OR 1=1 --"}).status_code == 200


@pytest.mark.parametrize(
    "kind",
    [
        "users",
        "Lessons",
        "materials;",
        "all",
        "question_sets",
        "profile",
        "material_revisions",
        "%2e%2e",
        "lessons%00",
    ],
)
def test_only_the_seven_documented_kinds_can_be_listed(api, kind):
    assert api.get(f"/api/resources/{kind}").status_code in (404, 422)


def test_every_kind_lists_owned_metadata_only(api, lesson_id):
    from tests.e2e.helpers import book

    material = book(api)["id"]
    quiz = create_quiz(api, lesson_id)
    deck = api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}).json()
    api.post(f"/api/lessons/{lesson_id}/complete")
    tournament = api.post("/api/tournament", json={"lesson_id": lesson_id}).json()
    job = api.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "SECRET-JOB-TOPIC", "age": 12}},
        headers={"Idempotency-Key": "listing-job-0001"},
    ).json()
    expected = {
        "materials": material,
        "lessons": lesson_id,
        "quizzes": quiz["id"],
        "decks": deck["id"],
        "completions": lesson_id,
        "tournaments": tournament["id"],
        "jobs": job["id"],
    }
    forbidden = {
        "pages",
        "sections",
        "questions",
        "cards",
        "answer",
        "answers",
        "explanation",
        "options",
        "input",
        "digest",
        "lease_token",
        "text",
        "key_points",
        "summary",
    }
    for kind, identifier in expected.items():
        r = api.get(f"/api/resources/{kind}")
        assert r.status_code == 200, kind
        ids = [item["id"] for item in r.json()["items"]]
        assert identifier in ids, kind
        assert not forbidden & keys_anywhere(r.json()), kind
        assert "SECRET-JOB-TOPIC" not in r.text
        assert (
            api.get(f"/api/resources/{kind}", headers=child_headers(api)).json()["items"] == []
        ), kind


# ---------------------------------------------------------------- deleting all application data
def test_deleting_account_data_is_repeatable_and_total(api, lesson_id):
    from tests.e2e.helpers import book

    material = book(api)["id"]
    quiz = create_quiz(api, lesson_id)
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 1})
    api.post(f"/api/lessons/{lesson_id}/complete")
    api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3})
    api.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "x", "age": 12}},
        headers={"Idempotency-Key": "delete-me-0001"},
    )
    api.put("/api/me/profile", json={"display_name": "Gone"})
    room = api.post("/api/rooms", json={"display_name": "Alex"}).json()
    assert api.delete("/api/me/data").status_code == 204
    assert api.delete("/api/me/data").status_code == 204  # nothing left, still fine
    for kind in ("materials", "lessons", "quizzes", "decks", "completions", "tournaments", "jobs"):
        assert api.get(f"/api/resources/{kind}").json()["items"] == [], kind
    assert api.get("/api/me/profile").json() == DEFAULT_PROFILE
    assert api.get("/api/me/progress").json()["quiz_answers"] == 0
    for url in (
        f"/api/lessons/{lesson_id}",
        f"/api/quiz/{quiz['id']}",
        f"/api/materials/{material}",
        f"/api/materials/{material}/revisions/1",
        f"/api/rooms/{room['id']}",
    ):
        assert api.get(url).status_code == 404, url
    # The account itself still works and can start again.
    assert api.post("/api/lessons", json={"topic": "fresh start", "age": 12}).status_code == 201


def test_deleting_one_accounts_data_leaves_everyone_elses(api, lesson_id):
    other = child_headers(api)
    theirs = api.post(
        "/api/lessons", json={"topic": "child lesson", "age": 9}, headers=other
    ).json()["id"]
    api.put("/api/me/profile", json={"display_name": "Sam"}, headers=other)
    api.delete("/api/me/data")
    assert api.get(f"/api/lessons/{theirs}", headers=other).status_code == 200
    assert api.get("/api/me/profile", headers=other).json()["display_name"] == "Sam"


def test_deleted_ids_are_not_reusable_and_budgets_survive_deletion(api):
    api.app.state.settings.generation_daily_limit = 2
    for _ in range(2):
        api.post("/api/lessons", json={"topic": "x", "age": 12})
    api.delete("/api/me/data")
    # Deleting content must not refund the abuse counters (the report documents this as intended).
    assert api.post("/api/lessons", json={"topic": "x", "age": 12}).status_code == 429


def test_approving_after_deleting_everything_is_a_404_not_a_resurrection(api, lesson_id):
    api.delete("/api/me/data")
    assert api.post(f"/api/lessons/{lesson_id}/approve", json=approval()).status_code == 404
    assert api.get("/api/resources/lessons").json()["items"] == []
