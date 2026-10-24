"""Lessons, approvals, revisions, quizzes, diagram questions, flashcards and tournaments."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser

import pytest

from learning_service.visuals.models import Visual
from tests.e2e.helpers import (
    CANARY,
    PARENT,
    approval,
    book,
    child_headers,
    create_quiz,
    draw_diagram,
    keys_anywhere,
    lesson_from,
)

ANSWER_KEYS = {
    "answer",
    "answers",
    "explanation",
    "answer_visual",
    "answer_visual_html",
    "node_id",
    "unresolved",
    "correct",
}
EDIT = {
    "lesson": {
        "title": "Edited",
        "summary": "Edited summary",
        "sections": [{"heading": "H", "body": "B"}],
        "key_points": ["K"],
    }
}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = []

    def handle_starttag(self, tag, attrs):
        self.names.append(tag)


def start_tags(page):
    parser = Tags()
    parser.feed(page)
    return parser.names


def instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def new_lesson(api, **extra):
    r = api.post("/api/lessons", json={"topic": "computer networks", "age": 12, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- creating lessons
@pytest.mark.parametrize(
    "age,ok",
    [
        (5, True),
        (99, True),
        (12, True),
        (4, False),
        (100, False),
        (0, False),
        (-1, False),
        (None, False),
        ("old", False),
        (12.5, False),
        ([12], False),
        ({"n": 12}, False),
    ],
)
def test_lesson_age_boundaries(api, age, ok):
    r = api.post("/api/lessons", json={"topic": "x", "age": age})
    assert (r.status_code == 201) is ok, r.text


@pytest.mark.parametrize(
    "topic,ok",
    [
        ("a", True),
        ("a" * 200, True),
        ("a" * 201, False),
        ("", False),
        ("   ", False),
        ("\n\t", False),
        (None, False),
        (5, False),
        (["x"], False),
        ("🦊" * 200, True),
        ("שלום", True),
        ("<script>alert(1)</script>", True),
        ("'; DROP TABLE lessons;--", True),
    ],
)
def test_lesson_topic_boundaries(api, topic, ok):
    r = api.post("/api/lessons", json={"topic": topic, "age": 12})
    assert (r.status_code == 201) is ok, r.text
    if ok:
        assert (
            r.json()["topic"] == topic.strip() and api.seen["lesson_request"].topic == topic.strip()
        )


def test_a_lesson_cannot_be_created_already_approved_or_with_a_chosen_id(api):
    forged = {
        "id": "chosen",
        "status": "approved",
        "approval": {"kind": "owner_confirmation"},
        "revision": 9,
        "previous_id": "x",
        "visuals": [{"id": "v"}],
        "source": {"material_id": "m"},
    }
    lesson = new_lesson(api, **forged)
    assert lesson["id"] != "chosen" and lesson["status"] == "draft" and lesson["revision"] == 1
    assert (
        lesson["approval"] is None
        and lesson["previous_id"] is None
        and lesson["visuals"] == []
        and lesson["source"] is None
    )
    assert api.post("/api/tournament", json={"lesson_id": lesson["id"]}).status_code == 409


def test_an_empty_material_id_means_general_knowledge_not_someone_elses_book(api):
    other = book(api, None)["id"]
    assert (
        api.post("/api/lessons", json={"topic": "x", "age": 12, "material_id": ""}).json()["source"]
        is None
    )
    assert lesson_from(api, other).status_code == 201
    assert (
        api.post(
            "/api/lessons",
            json={"topic": "chapter 1", "age": 12, "material_id": other},
            headers=child_headers(api),
        ).status_code
        == 404
    )


def test_a_book_lesson_needs_a_confirmed_current_revision(api):
    from tests.e2e.helpers import upload
    from tests.learning.files import make_pdf

    mid = upload(api, ("b.pdf", make_pdf(), "application/pdf")).json()["id"]
    assert lesson_from(api, mid).status_code == 409
    api.post(f"/api/materials/{mid}/confirm", json=approval())
    assert lesson_from(api, mid).status_code == 201
    api.put(
        f"/api/materials/{mid}/pages",
        json={"expected_revision": 1, "pages": [{"number": 1, "text": "New", "via": "text"}]},
    )
    assert lesson_from(api, mid).status_code == 409


# ---------------------------------------------------------------- loose typing
@pytest.mark.xfail(
    strict=True,
    reason="BUG-07: booleans, numeric strings and floats are accepted where integers are specified",
)
def test_integer_fields_do_not_accept_booleans_strings_or_whole_floats(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    deck = api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 3}).json()
    tournament = api.post("/api/tournament", json={"lesson_id": lesson_id}).json()
    api.post(f"/api/tournament/{tournament['id']}/approve", json=approval())
    attempts = [
        api.post("/api/lessons", json={"topic": "x", "age": "12"}),
        api.post("/api/lessons", json={"topic": "x", "age": 12.0}),
        api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": True}),
        api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": "1"}),
        api.post(f"/api/flashcards/{deck['id']}/review", json={"card": True, "result": "know"}),
        api.post(f"/api/tournament/{tournament['id']}/submit", json={"answers": [True, False]}),
        api.post("/api/rooms", json={"display_name": "A", "capacity": "4"}),
        api.post(
            f"/api/lessons/{lesson_id}/approve", json={"expected_revision": 1, "confirmed": 1}
        ),
    ]
    assert [r.status_code for r in attempts] == [422] * len(attempts)


# ---------------------------------------------------------------- approval and revisions
def test_approval_is_idempotent_and_keeps_the_first_record(api):
    lesson = new_lesson(api)
    first = api.post(f"/api/lessons/{lesson['id']}/approve", json=approval()).json()
    again = api.post(f"/api/lessons/{lesson['id']}/approve", json=approval()).json()
    assert (
        first == again
        and first["approval"]["kind"] == "owner_confirmation"
        and first["approval"]["reviewer_id"] == PARENT
    )
    stored = api.get(f"/api/lessons/{lesson['id']}").json()["approval"]
    assert instant(stored["reviewed_at"]) == instant(first["approval"]["reviewed_at"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG-17: the same reviewed_at instant is rendered as '...Z' and as '...+00:00'",
)
def test_the_approval_timestamp_has_one_spelling(api):
    lesson = new_lesson(api)
    approved = api.post(f"/api/lessons/{lesson['id']}/approve", json=approval()).json()
    assert (
        api.get(f"/api/lessons/{lesson['id']}").json()["approval"]["reviewed_at"]
        == approved["approval"]["reviewed_at"]
    )


@pytest.mark.parametrize(
    "body,status",
    [
        ({"expected_revision": 1, "confirmed": False}, 422),
        ({"expected_revision": 1}, 422),
        ({"confirmed": True}, 422),
        ({"expected_revision": 0, "confirmed": True}, 422),
        ({"expected_revision": 2, "confirmed": True}, 409),
        ({"expected_revision": 1, "confirmed": "true"}, 422),
        ({"expected_revision": 1, "confirmed": True, "approved_by": "me"}, 422),
    ],
)
def test_approval_needs_the_current_revision_and_a_literal_true(api, body, status):
    lesson = new_lesson(api)
    assert api.post(f"/api/lessons/{lesson['id']}/approve", json=body).status_code == status
    assert api.get(f"/api/lessons/{lesson['id']}").json()["status"] == "draft"


def test_an_empty_lesson_can_be_saved_as_a_draft_but_never_approved(api):
    lesson = new_lesson(api)
    empty = {"lesson": {"title": "t", "summary": "s", "sections": [], "key_points": []}}
    revised = api.post(f"/api/lessons/{lesson['id']}/revisions", json=empty).json()
    r = api.post(f"/api/lessons/{revised['id']}/approve", json=approval(2))
    assert r.status_code == 409 and "empty" in r.json()["detail"]


def test_drawing_new_visuals_withdraws_approval_and_blocks_scored_quizzes(api, lesson_id):
    assert api.get(f"/api/lessons/{lesson_id}").json()["status"] == "approved"
    draw_diagram(api, lesson_id)
    lesson = api.get(f"/api/lessons/{lesson_id}").json()
    assert lesson["status"] == "draft" and lesson["approval"] is None
    assert api.post("/api/tournament", json={"lesson_id": lesson_id}).status_code == 409
    renewed = api.post(f"/api/lessons/{lesson_id}/approve", json=approval()).json()
    assert renewed["status"] == "approved"
    assert (
        api.post(f"/api/lessons/{lesson_id}/visuals", json={}).json()["visuals"] == []
    )  # nothing new drawn
    assert api.get(f"/api/lessons/{lesson_id}").json()["status"] == "approved"  # so approval stands
    assert api.post("/api/tournament", json={"lesson_id": lesson_id}).status_code == 201


def test_a_revision_is_a_new_draft_and_never_touches_the_original(api, lesson_id):
    draw_diagram(api, lesson_id)
    api.post(f"/api/lessons/{lesson_id}/approve", json=approval())
    before = api.get(f"/api/lessons/{lesson_id}").json()
    second = api.post(f"/api/lessons/{lesson_id}/revisions", json=EDIT).json()
    third = api.post(f"/api/lessons/{second['id']}/revisions", json=EDIT).json()
    assert (second["revision"], third["revision"]) == (2, 3)
    assert second["previous_id"] == lesson_id and third["previous_id"] == second["id"]
    assert api.get(f"/api/lessons/{lesson_id}").json() == before
    fresh = api.get(f"/api/lessons/{third['id']}").json()
    assert (
        fresh["status"] == "draft"
        and fresh["approval"] is None
        and fresh["visuals"] == []
        and fresh["title"] == "Edited"
    )
    assert (
        len(api.post(f"/api/lessons/{third['id']}/visuals", json={}).json()["visuals"]) == 1
    )  # may be drawn again
    assert (
        api.post(f"/api/lessons/{second['id']}/approve", json=approval(1)).status_code == 409
    )  # stale: it is revision 2


def test_a_revision_of_a_book_lesson_keeps_its_provenance(api):
    mid = book(api)["id"]
    original = lesson_from(api, mid, "chapter 2").json()
    revised = api.post(f"/api/lessons/{original['id']}/revisions", json=EDIT).json()
    assert api.get(f"/api/lessons/{revised['id']}").json()["source"] == original["source"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"lesson": {"title": "t"}},
        {"lesson": "text"},
        {"lesson": None},
        {**EDIT, "status": "approved"},
        {"lesson": {**EDIT["lesson"], "sections": [{"heading": "h"}]}},
        {"lesson": {**EDIT["lesson"], "key_points": "no"}},
    ],
)
def test_a_malformed_revision_creates_nothing(api, lesson_id, body):
    before = len(api.get("/api/resources/lessons").json()["items"])
    assert api.post(f"/api/lessons/{lesson_id}/revisions", json=body).status_code == 422
    assert len(api.get("/api/resources/lessons").json()["items"]) == before


def test_completion_is_recorded_once_even_under_concurrency(api, lesson_id):
    def complete(_):
        r = api.post(f"/api/lessons/{lesson_id}/complete")
        return r.status_code, r.json().get("completed_at")

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(complete, range(6)))
    # PostgreSQL serialises the writers (all 200); the in-memory demo adapter turns a lost race into a 409.
    assert {code for code, _ in results} <= {200, 409} and any(code == 200 for code, _ in results)
    assert len({stamp for code, stamp in results if code == 200}) == 1
    assert api.get("/api/me/progress").json()["lessons_completed"] == 1
    assert api.post(f"/api/lessons/{lesson_id}/complete").status_code == 200


# ---------------------------------------------------------------- visuals and the page renderer
def evil_visual(**changes):
    base = dict(
        kind="diagram",
        section_index=0,
        title="t",
        caption="c",
        alt="a",
        code='flowchart LR\n  a["A"] --> b["B"]',
    )
    return Visual(**{**base, **changes})


def draw_with(api, monkeypatch, visual, lesson_id):
    import learning_service.routes as routes

    monkeypatch.setattr(routes, "make_visuals", lambda request: [visual])
    r = api.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "x"})
    assert r.status_code == 201, r.text
    shown = r.json()["visuals"][0]
    page = api.get(f"/api/lessons/{lesson_id}/visuals/{shown['id']}/view")
    return shown["html"], page


@pytest.mark.parametrize("field", ["title", "alt"])
def test_hostile_visual_text_cannot_break_out_of_the_page(api, lesson_id, monkeypatch, field):
    payload = "</title></script><img src=x onerror=alert(1)><script>alert(2)</script>"
    html, page = draw_with(api, monkeypatch, evil_visual(**{field: payload}), lesson_id)
    for text in (html, page.text):
        assert "</script><img" not in text and "</title><img" not in text
        tags = start_tags(text)
        # Parsed like a browser would: no injected element, and only the page's own three scripts.
        assert "img" not in tags and tags.count("script") == 3, tags
    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    assert page.headers["content-type"].startswith("text/html")


def test_the_page_pins_a_strict_content_security_policy(api, lesson_id, monkeypatch):
    html, _ = draw_with(api, monkeypatch, evil_visual(), lesson_id)
    assert "default-src 'none'" in html and "connect-src" not in html and "frame-src" not in html
    assert "cdn.jsdelivr.net" in html and "http://" not in html.replace("http://www.w3.org", "")


@pytest.mark.xfail(
    strict=True,
    reason="BUG-15: '<!--' inside the embedded JSON can swallow the closing </script> of the page's boot script",
)
def test_html_comment_openers_are_neutralised_inside_script_data(api, lesson_id, monkeypatch):
    _, page = draw_with(api, monkeypatch, evil_visual(alt="<!--<script>"), lesson_id)
    assert "<!--" not in page.text


def test_visual_pages_belong_to_their_owner_and_lesson(api, lesson_id):
    visual = draw_diagram(api, lesson_id)
    other = new_lesson(api)["id"]
    base = f"/api/lessons/{lesson_id}/visuals/{visual['id']}/view"
    assert api.get(base).status_code == 200
    assert api.get(base, headers=child_headers(api)).status_code == 404
    assert api.get(f"/api/lessons/{other}/visuals/{visual['id']}/view").status_code == 404
    assert api.get(f"/api/lessons/{lesson_id}/visuals/{visual['id']}x/view").status_code == 404


def test_the_ask_field_has_a_hard_length_limit_and_is_trimmed(api, lesson_id):
    assert api.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "x" * 300}).status_code == 201
    assert api.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "x" * 301}).status_code == 422
    assert api.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": 5}).status_code == 422
    assert api.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": None}).status_code in (
        200,
        201,
    )


def test_simultaneous_automatic_drawing_draws_each_planned_visual_once(api, lesson_id):
    def draw(_):
        return api.post(f"/api/lessons/{lesson_id}/visuals", json={}).status_code

    with ThreadPoolExecutor(5) as pool:
        codes = list(pool.map(draw, range(5)))
    assert 201 in codes and set(codes) <= {201, 409, 429}  # 429: all generation slots busy
    assert len(api.get(f"/api/lessons/{lesson_id}").json()["visuals"]) == 1


# ---------------------------------------------------------------- quizzes
@pytest.mark.parametrize(
    "count,ok",
    [
        (1, True),
        (15, True),
        (0, False),
        (16, False),
        (-3, False),
        (None, False),
        ("many", False),
        (2.5, False),
    ],
)
def test_quiz_size_boundaries(api, lesson_id, count, ok):
    r = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": count})
    assert (r.status_code == 201) is ok, r.text


def test_practice_quizzes_work_on_a_draft_lesson_but_scored_ones_do_not(api):
    draft = new_lesson(api)["id"]
    assert api.post(f"/api/lessons/{draft}/quiz", json={}).status_code == 201
    assert api.post("/api/tournament", json={"lesson_id": draft}).status_code == 409


def test_no_representation_of_a_playable_quiz_contains_an_answer_key(api, lesson_id):
    draw_diagram(api, lesson_id)
    api.post(
        f"/api/lessons/{lesson_id}/approve", json=approval()
    )  # new visuals withdrew the approval
    quiz = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 4}).json()
    tournament = api.post("/api/tournament", json={"lesson_id": lesson_id}).json()
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0})
    for index, question in enumerate(quiz["questions"]):
        if question["visual_html"] is None:
            api.post(f"/api/quiz/{quiz['id']}/check", json={"index": index, "choice": 0})
    attempt = api.post(f"/api/quiz/{quiz['id']}/attempts").json()
    followup = api.post(f"/api/quiz/{quiz['id']}/followup", json={})
    pages = [
        quiz,
        attempt,
        tournament,
        api.get(f"/api/quiz/{quiz['id']}").json(),
        api.get(f"/api/quiz/{attempt['id']}").json(),
        api.get(f"/api/tournament/{tournament['id']}").json(),
    ]
    if followup.status_code == 201:
        pages.append(followup.json())
    for page in pages:
        assert not ANSWER_KEYS & keys_anywhere(page), ANSWER_KEYS & keys_anywhere(page)
    # ...and a tournament stays answer-free even after it has been submitted.
    api.post(f"/api/tournament/{tournament['id']}/approve", json=approval())
    api.post(
        f"/api/tournament/{tournament['id']}/submit",
        json={"answers": [0] * len(tournament["questions"])},
    )
    assert not ANSWER_KEYS & keys_anywhere(api.get(f"/api/tournament/{tournament['id']}").json())


@pytest.mark.parametrize(
    "body,status",
    [
        ({"index": 0, "choice": 0}, 200),
        ({"index": 0, "choice": 3}, 200),
        ({"index": 0, "choice": 4}, 422),
        ({"index": 0, "choice": -1}, 422),
        ({"index": 1, "choice": 1}, 200),
        ({"index": 1, "choice": 2}, 422),
        ({"index": 2, "choice": 0}, 422),
        ({"index": -1, "choice": 0}, 422),
        ({"index": 10**30, "choice": 0}, 422),
        ({"index": 0, "choice": 10**30}, 422),
        ({"index": 0.5, "choice": 0}, 422),
        ({"index": "x", "choice": 0}, 422),
        ({"index": None, "choice": 0}, 422),
        ({"index": 0}, 422),
        ({"choice": 0}, 422),
        ({}, 422),
    ],
)
def test_answer_checking_bounds(api, lesson_id, body, status):
    quiz = create_quiz(api, lesson_id)
    assert api.post(f"/api/quiz/{quiz['id']}/check", json=body).status_code == status


def test_a_rejected_answer_is_not_recorded(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    assert (
        api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 9}).status_code == 422
    )
    assert api.get("/api/me/progress").json()["quiz_answers"] == 0
    assert api.post(f"/api/quiz/{quiz['id']}/followup", json={}).status_code == 409


def test_only_the_first_choice_counts_but_every_check_gets_graded(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    grades = [
        api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": c}).json()["correct"]
        for c in (0, 1, 2, 1)
    ]
    assert grades == [False, True, False, True]
    progress = api.get("/api/me/progress").json()
    assert progress["quiz_answers"] == 1 and progress["correct_answers"] == 0
    assert api.post(f"/api/quiz/{quiz['id']}/followup", json={}).status_code == 201


def test_concurrent_first_answers_record_exactly_one(api, lesson_id):
    quiz = create_quiz(api, lesson_id)

    def check(choice):
        return api.post(
            f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": choice}
        ).status_code

    with ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(check, [0, 1, 2, 3]))
    assert 200 in codes and set(codes) <= {200, 409}
    assert api.get("/api/me/progress").json()["quiz_answers"] == 1


def test_each_attempt_has_its_own_answers_and_no_attempt_unlocks_another(api, lesson_id):
    draw_diagram(api, lesson_id)
    quiz = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 4}).json()
    diagram = next(q for q in quiz["questions"] if q["visual_html"])
    attempt = api.post(f"/api/quiz/{quiz['id']}/attempts").json()
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": diagram["index"], "choice": 0})
    ok = api.get(
        f"/api/quiz/{quiz['id']}/questions/{diagram['index']}/view", params={"show": "answer"}
    )
    blocked = api.get(
        f"/api/quiz/{attempt['id']}/questions/{diagram['index']}/view", params={"show": "answer"}
    )
    assert ok.status_code == 200 and blocked.status_code == 409
    assert api.post(f"/api/quiz/{attempt['id']}/followup", json={}).status_code == 409


def test_the_hidden_box_label_never_reaches_the_browser_before_answering(api, lesson_id):
    draw_diagram(api, lesson_id)
    quiz = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 4}).json()
    diagram = next(q for q in quiz["questions"] if q["visual_html"])
    assert "Firewall" not in diagram["visual_html"]
    page = api.get(f"/api/quiz/{quiz['id']}/questions/{diagram['index']}/view")
    assert page.status_code == 200 and "Firewall" not in page.text
    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    assert (
        api.get(
            f"/api/quiz/{quiz['id']}/questions/{diagram['index']}/view", params={"show": "answer"}
        ).status_code
        == 409
    )
    right = diagram["options"].index("Firewall")
    after = api.post(
        f"/api/quiz/{quiz['id']}/check", json={"index": diagram["index"], "choice": right}
    ).json()
    assert "Firewall" in after["answer_visual_html"]
    assert (
        "Firewall"
        in api.get(
            f"/api/quiz/{quiz['id']}/questions/{diagram['index']}/view", params={"show": "answer"}
        ).text
    )


@pytest.mark.parametrize(
    "params,status",
    [({"show": "bogus"}, 422), ({"show": ""}, 422), ({"show": "QUESTION"}, 422), ({}, 200)],
)
def test_question_view_parameters(api, lesson_id, params, status):
    draw_diagram(api, lesson_id)
    quiz = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 4}).json()
    diagram = next(q for q in quiz["questions"] if q["visual_html"])
    assert (
        api.get(
            f"/api/quiz/{quiz['id']}/questions/{diagram['index']}/view", params=params
        ).status_code
        == status
    )


def test_question_view_indexes(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    assert (
        api.get(f"/api/quiz/{quiz['id']}/questions/0/view").status_code == 404
    )  # text question: no picture
    for index in (-1, 2, 99, 10**20):
        assert api.get(f"/api/quiz/{quiz['id']}/questions/{index}/view").status_code == 422
    assert api.get(f"/api/quiz/{quiz['id']}/questions/zero/view").status_code == 422


def test_follow_up_quizzes_chain_and_stay_inside_their_owner(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    assert api.post(f"/api/quiz/{quiz['id']}/followup", json={}).status_code == 409
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0})
    follow = api.post(f"/api/quiz/{quiz['id']}/followup", json={"num_questions": 2}).json()
    assert (
        follow["parent_id"] == quiz["id"] and follow["focus"] and follow["lesson_id"] == lesson_id
    )
    api.post(f"/api/quiz/{follow['id']}/check", json={"index": 0, "choice": 3})
    assert api.post(f"/api/quiz/{follow['id']}/followup", json={}).status_code == 201
    for count in (0, 11, -1, "3"):
        assert api.post(
            f"/api/quiz/{quiz['id']}/followup", json={"num_questions": count}
        ).status_code in (422, 201)
    assert (
        api.post(f"/api/quiz/{quiz['id']}/followup", json={"num_questions": 0}).status_code == 422
    )
    assert (
        api.post(f"/api/quiz/{quiz['id']}/followup", json={"num_questions": 11}).status_code == 422
    )
    assert (
        api.post(
            f"/api/quiz/{follow['id']}/followup", json={}, headers=child_headers(api)
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "which,status",
    [("missed", 409), ("all", 201), ("bogus", 422), ("", 422), (None, 422), ("MISSED", 422)],
)
def test_mistake_decks_before_any_answer(api, lesson_id, which, status):
    quiz = create_quiz(api, lesson_id)
    assert (
        api.post(f"/api/quiz/{quiz['id']}/flashcards", json={"which": which}).status_code == status
    )


def test_mistake_decks_contain_only_what_was_missed(api, lesson_id):
    quiz = create_quiz(api, lesson_id)
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 1})  # right
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 1, "choice": 0})  # wrong
    deck = api.post(f"/api/quiz/{quiz['id']}/flashcards", json={"which": "missed"}).json()
    assert (
        len(deck["cards"]) == 1
        and "NACLs are stateful" in deck["cards"][0]["front"]
        and "You chose" in deck["cards"][0]["back"]
    )
    assert (
        len(api.post(f"/api/quiz/{quiz['id']}/flashcards", json={"which": "all"}).json()["cards"])
        == 2
    )


# ---------------------------------------------------------------- flashcards
@pytest.mark.parametrize(
    "count,ok",
    [
        (3, True),
        (20, True),
        (2, False),
        (21, False),
        (0, False),
        (-1, False),
        (None, False),
        ("8", True),
    ],
)
def test_deck_size_boundaries(api, lesson_id, count, ok):
    r = api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": count})
    assert (r.status_code == 201) is ok or count == "8"


def make_deck(api, lesson_id, cards=3):
    return api.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": cards}).json()


def review_url(deck):
    return f"/api/flashcards/{deck['id']}/review"


def test_the_review_loop_ends_cleanly_and_then_refuses_more(api, lesson_id):
    deck = make_deck(api, lesson_id)
    url = review_url(deck)
    api.post(url, json={"card": 0, "result": "again"})
    order = []
    state = api.get(f"/api/flashcards/{deck['id']}").json()["state"]
    while state["next"] is not None:
        order.append(state["next"])
        state = api.post(url, json={"card": state["next"], "result": "know"}).json()
    assert sorted(order) == [0, 1, 2] and state == {
        "next": None,
        "known": 3,
        "total": 3,
        "reviews": 4,
        "hard": [0],
    }
    for card in (0, 1, 2, 3, -1):
        assert api.post(url, json={"card": card, "result": "know"}).status_code == 422
    assert api.get(f"/api/flashcards/{deck['id']}").json()["state"] == state


@pytest.mark.parametrize(
    "body",
    [
        {"card": 0},
        {"result": "know"},
        {"card": 0, "result": "KNOW"},
        {"card": 0, "result": ""},
        {"card": "zero", "result": "know"},
        {"card": 0.5, "result": "know"},
        {"card": 0, "result": "know", "expected_reviews": -1},
        {"card": 0, "result": "know", "expected_reviews": "x"},
    ],
)
def test_review_bodies_are_validated_before_anything_changes(api, lesson_id, body):
    deck = make_deck(api, lesson_id)
    assert api.post(review_url(deck), json=body).status_code == 422
    assert api.get(f"/api/flashcards/{deck['id']}").json()["state"]["reviews"] == 0


def test_idempotency_keys_replay_conflict_and_scope(api, lesson_id):
    deck, other = make_deck(api, lesson_id), make_deck(api, lesson_id)
    key = {"Idempotency-Key": "review-key-0001"}
    body = {"card": 0, "result": "again", "expected_reviews": 0}
    first = api.post(review_url(deck), json=body, headers=key)
    assert api.post(review_url(deck), json=body, headers=key).json() == first.json()
    assert api.get(f"/api/flashcards/{deck['id']}").json()["state"]["reviews"] == 1
    assert (
        api.post(review_url(deck), json={**body, "result": "know"}, headers=key).status_code == 409
    )
    assert api.post(review_url(deck), json={**body, "card": 1}, headers=key).status_code == 409
    assert (
        api.post(
            review_url(deck), json=body, headers={"Idempotency-Key": "review-key-0002"}
        ).status_code
        == 409
    )  # stale counter
    assert (
        api.post(review_url(other), json=body, headers=key).status_code == 200
    )  # same key, different deck
    assert (
        api.post(
            review_url(deck),
            json={"card": 1, "result": "know", "expected_reviews": 1},
            headers={"Idempotency-Key": "Review-Key-0001"},
        ).status_code
        == 200
    )  # keys are case-sensitive
    for bad in ("short", "x" * 129):
        assert (
            api.post(
                review_url(deck),
                json={"card": 2, "result": "know"},
                headers={"Idempotency-Key": bad},
            ).status_code
            == 422
        )


def test_a_deck_stops_accepting_new_keys_at_a_thousand_receipts(api, lesson_id, repository):
    deck = make_deck(api, lesson_id, 5)
    with repository.session(PARENT) as unit:
        record = unit.get("decks", deck["id"])
        record["review_receipts"] = {
            f"filler-{n:06d}": {"input": {}, "state": {}} for n in range(1000)
        }
        unit.put("decks", deck["id"], record)
    keyed = lambda key: {"Idempotency-Key": key}  # noqa: E731
    body = {"card": 0, "result": "know"}
    assert api.post(review_url(deck), json=body, headers=keyed("fresh-key-0001")).status_code == 429
    assert api.get(f"/api/flashcards/{deck['id']}").json()["state"]["reviews"] == 0
    assert (
        api.post(review_url(deck), json=body).status_code == 200
    )  # legacy un-keyed reviews still work


def test_concurrent_identical_keyed_reviews_count_once(api, lesson_id):
    deck = make_deck(api, lesson_id)

    def review(_):
        return api.post(
            review_url(deck),
            json={"card": 0, "result": "again", "expected_reviews": 0},
            headers={"Idempotency-Key": "race-key-0001"},
        ).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(review, range(6)))
    assert 200 in codes and set(codes) <= {200, 409}
    assert api.get(f"/api/flashcards/{deck['id']}").json()["state"]["reviews"] == 1


def test_concurrent_reviews_of_one_card_retire_it_once(api, lesson_id):
    deck = make_deck(api, lesson_id)

    def review(_):
        return api.post(review_url(deck), json={"card": 1, "result": "know"}).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(review, range(6)))
    assert 200 in codes
    state = api.get(f"/api/flashcards/{deck['id']}").json()["state"]
    assert state["known"] == 1 and state["reviews"] == 1


def test_card_pictures_have_strict_sides_and_bounds(api, lesson_id):
    draw_diagram(api, lesson_id)
    deck = make_deck(api, lesson_id, 6)
    index = next(c["index"] for c in deck["cards"] if c["kind"] == "diagram")
    base = f"/api/flashcards/{deck['id']}/cards"
    assert "Firewall" not in api.get(f"{base}/{index}/view", params={"side": "front"}).text
    assert "Firewall" in api.get(f"{base}/{index}/view", params={"side": "back"}).text
    assert api.get(f"{base}/{index}/view", params={"side": "both"}).status_code == 422
    assert (
        api.get(f"{base}/6/view").status_code == 404
        and api.get(f"{base}/-1/view").status_code == 404
    )
    assert api.get(f"{base}/{index}/view", headers=child_headers(api)).status_code == 404


# ---------------------------------------------------------------- tournaments
def tournament(api, lesson_id, approve=True):
    t = api.post("/api/tournament", json={"lesson_id": lesson_id}).json()
    if approve:
        assert api.post(f"/api/tournament/{t['id']}/approve", json=approval()).status_code == 200
    return t


def answer_key(api, t):
    return [
        q["options"].index(q["answer"])
        for q in api.get(f"/api/tournament/{t['id']}/review").json()["questions"]
    ]


@pytest.mark.parametrize(
    "count,ok", [(1, True), (15, True), (0, False), (16, False), (None, False)]
)
def test_tournament_size_boundaries(api, lesson_id, count, ok):
    r = api.post("/api/tournament", json={"lesson_id": lesson_id, "num_questions": count})
    assert (r.status_code == 201) is ok


def test_tournament_creation_requires_an_owned_approved_lesson(api, lesson_id):
    draft = new_lesson(api)["id"]
    assert api.post("/api/tournament", json={"lesson_id": draft}).status_code == 409
    assert api.post("/api/tournament", json={"lesson_id": "nope"}).status_code == 404
    assert api.post("/api/tournament", json={}).status_code == 422
    assert (
        api.post(
            "/api/tournament", json={"lesson_id": lesson_id}, headers=child_headers(api)
        ).status_code
        == 404
    )


def test_scoring_is_exact_at_both_extremes(api, lesson_id):
    t = tournament(api, lesson_id)
    key = answer_key(api, t)
    perfect = api.post(f"/api/tournament/{t['id']}/submit", json={"answers": key}).json()
    assert perfect["score"] == perfect["total"] == len(key) and all(
        r["correct"] for r in perfect["results"]
    )
    t2 = tournament(api, lesson_id)
    wrong = api.post(
        f"/api/tournament/{t2['id']}/submit",
        json={"answers": [(i + 1) % 2 for i in answer_key(api, t2)]},
    ).json()
    assert wrong["score"] == 0 and not any(r["correct"] for r in wrong["results"])


@pytest.mark.parametrize(
    "answers,status",
    [
        ([], 422),
        ([0], 422),
        ([0, 0, 0], 422),
        ([-1, 0], 422),
        ([0, 99], 422),
        ([None, 0], 422),
        (["a", "b"], 422),
        ([[0], [0]], 422),
        ("01", 422),
        (None, 422),
        ({"0": 0}, 422),
        ([10**30, 0], 422),
        ([0, 0], 200),
    ],
)
def test_submission_shapes(api, lesson_id, answers, status):
    t = tournament(api, lesson_id)
    assert (
        api.post(f"/api/tournament/{t['id']}/submit", json={"answers": answers}).status_code
        == status
    )
    if status != 200:  # a refused submission must not lock the attempt
        assert (
            api.post(f"/api/tournament/{t['id']}/submit", json={"answers": [0, 0]}).status_code
            == 200
        )


def test_submission_lifecycle_rules(api, lesson_id):
    t = tournament(api, lesson_id, approve=False)
    url = f"/api/tournament/{t['id']}"
    assert (
        api.post(f"{url}/submit", json={"answers": [0, 0]}).status_code == 409
    )  # not reviewed yet
    assert (
        api.post(f"{url}/approve", json={"expected_revision": 1, "confirmed": False}).status_code
        == 422
    )
    assert (
        api.post(f"{url}/approve", json={"expected_revision": 2, "confirmed": True}).status_code
        == 409
    )
    first_approval = api.post(f"{url}/approve", json=approval()).json()
    assert api.post(f"{url}/approve", json=approval()).json() == first_approval
    first = api.post(f"{url}/submit", json={"answers": [0, 1]})
    assert api.post(f"{url}/submit", json={"answers": [0, 1]}).json() == first.json()
    assert api.post(f"{url}/submit", json={"answers": [1, 1]}).status_code == 409
    assert (
        api.post(f"{url}/submit", json={"answers": [0]}).status_code == 409
    )  # a recorded attempt wins over shape checks
    assert api.get("/api/me/progress").json()["tournaments_submitted"] == 1


def test_concurrent_different_submissions_have_a_single_winner(api, lesson_id):
    t = tournament(api, lesson_id)
    options = [[0, 0], [0, 1], [1, 0], [1, 1]]

    def submit(answers):
        r = api.post(f"/api/tournament/{t['id']}/submit", json={"answers": answers})
        return answers, r.status_code, r.json()

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(submit, options))
    winners = [(a, body) for a, code, body in results if code == 200]
    assert len(winners) == 1 and sorted(code for _, code, _ in results) == [200, 409, 409, 409]
    assert (
        api.post(f"/api/tournament/{t['id']}/submit", json={"answers": winners[0][0]}).json()
        == winners[0][1]
    )
    assert api.get("/api/me/progress").json()["tournaments_submitted"] == 1


def test_the_review_page_is_the_only_place_answers_appear_and_only_for_the_owner(api, lesson_id):
    t = tournament(api, lesson_id, approve=False)
    review = api.get(f"/api/tournament/{t['id']}/review")
    assert review.status_code == 200 and "owner_confirmation" in review.json()["review_kind"]
    assert (
        api.get(f"/api/tournament/{t['id']}/review", headers=child_headers(api)).status_code == 404
    )
    assert (
        api.post(
            f"/api/tournament/{t['id']}/approve", json=approval(), headers=child_headers(api)
        ).status_code
        == 404
    )
    assert CANARY not in review.text
