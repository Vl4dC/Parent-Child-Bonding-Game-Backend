"""Insecure-direct-object-reference sweep: another account holds every id and still gets nothing.

One account builds a complete world (material, lesson, picture, quiz, attempt, tournament, deck, job, room).
A second account then tries every endpoint that takes one of those ids, and so does an anonymous caller.
Each attempt must be refused as if the thing did not exist, and the owner's data must be byte-identical afterwards.
"""

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import approval, book, child_headers, draw_diagram, lesson_from

EDIT = {
    "lesson": {
        "title": "Hijacked",
        "summary": "x",
        "sections": [{"heading": "h", "body": "b"}],
        "key_points": [],
    }
}


@pytest.fixture
def world(api):
    """Everything the parent owns, keyed by role."""
    material = book(api)["id"]
    lesson = lesson_from(api, material, "chapter 2").json()["id"]
    api.post(f"/api/lessons/{lesson}/approve", json=approval())
    visual = draw_diagram(api, lesson)["id"]
    api.post(f"/api/lessons/{lesson}/approve", json=approval())  # new visuals withdrew the approval
    quiz = api.post(f"/api/lessons/{lesson}/quiz", json={"num_questions": 4}).json()
    api.post(f"/api/quiz/{quiz['id']}/check", json={"index": 0, "choice": 0})
    attempt = api.post(f"/api/quiz/{quiz['id']}/attempts").json()["id"]
    tournament = api.post("/api/tournament", json={"lesson_id": lesson}).json()["id"]
    deck = api.post(f"/api/lessons/{lesson}/flashcards", json={"num_cards": 3}).json()["id"]
    job = api.post(
        "/api/jobs",
        json={"kind": "lesson", "parameters": {"topic": "x", "age": 12}},
        headers={"Idempotency-Key": "isolation-job-0001"},
    ).json()["id"]
    room = api.post("/api/rooms", json={"display_name": "Alex"}).json()
    api.post(f"/api/lessons/{lesson}/complete")
    return {
        "material": material,
        "lesson": lesson,
        "visual": visual,
        "quiz": quiz["id"],
        "attempt": attempt,
        "tournament": tournament,
        "deck": deck,
        "job": job,
        "room": room["id"],
        "code": room["code"],
    }


def read_everything(api, w):
    urls = {
        "profile": "/api/me/profile",
        "progress": "/api/me/progress",
        "material": f"/api/materials/{w['material']}",
        "pages": f"/api/materials/{w['material']}/pages",
        "revision": f"/api/materials/{w['material']}/revisions/1",
        "lesson": f"/api/lessons/{w['lesson']}",
        "quiz": f"/api/quiz/{w['quiz']}",
        "attempt": f"/api/quiz/{w['attempt']}",
        "tournament": f"/api/tournament/{w['tournament']}",
        "review": f"/api/tournament/{w['tournament']}/review",
        "deck": f"/api/flashcards/{w['deck']}",
        "job": f"/api/jobs/{w['job']}",
        "room": f"/api/rooms/{w['room']}",
    }
    state = {name: api.get(url).json() for name, url in urls.items()}
    for kind in ("materials", "lessons", "quizzes", "tournaments", "decks", "jobs", "completions"):
        state["list-" + kind] = api.get(f"/api/resources/{kind}").json()
    return state


def attacks(w):
    """(method, url, json) for every route that takes an id from the world."""
    L, M, Q, A, T, D, J, R, V = (
        w[k]
        for k in (
            "lesson",
            "material",
            "quiz",
            "attempt",
            "tournament",
            "deck",
            "job",
            "room",
            "visual",
        )
    )
    vote = {"expected_revision": 1, "confirmed": True}
    return [
        ("GET", f"/api/materials/{M}", None),
        ("GET", f"/api/materials/{M}/pages", None),
        ("GET", f"/api/materials/{M}/revisions/1", None),
        ("GET", f"/api/materials/{M}/find?topic=chapter%201", None),
        (
            "PUT",
            f"/api/materials/{M}/pages",
            {
                "expected_revision": 1,
                "pages": [{"number": 1, "text": "overwritten", "via": "text"}],
            },
        ),
        ("POST", f"/api/materials/{M}/confirm", vote),
        ("DELETE", f"/api/materials/{M}", None),
        ("POST", "/api/lessons", {"topic": "chapter 1", "age": 12, "material_id": M}),
        ("GET", f"/api/lessons/{L}", None),
        ("POST", f"/api/lessons/{L}/revisions", EDIT),
        ("POST", f"/api/lessons/{L}/approve", vote),
        ("POST", f"/api/lessons/{L}/complete", None),
        ("POST", f"/api/lessons/{L}/visuals", {}),
        ("GET", f"/api/lessons/{L}/visuals/{V}/view", None),
        ("POST", f"/api/lessons/{L}/quiz", {}),
        ("POST", f"/api/lessons/{L}/flashcards", {}),
        ("POST", "/api/tournament", {"lesson_id": L}),
        ("GET", f"/api/quiz/{Q}", None),
        ("GET", f"/api/quiz/{A}", None),
        ("POST", f"/api/quiz/{Q}/check", {"index": 0, "choice": 0}),
        ("POST", f"/api/quiz/{A}/check", {"index": 0, "choice": 0}),
        ("POST", f"/api/quiz/{Q}/attempts", None),
        ("POST", f"/api/quiz/{Q}/followup", {}),
        ("POST", f"/api/quiz/{Q}/flashcards", {"which": "all"}),
        ("GET", f"/api/quiz/{Q}/questions/0/view", None),
        ("GET", f"/api/tournament/{T}", None),
        ("GET", f"/api/tournament/{T}/review", None),
        ("POST", f"/api/tournament/{T}/approve", vote),
        ("POST", f"/api/tournament/{T}/submit", {"answers": [0, 0]}),
        ("GET", f"/api/flashcards/{D}", None),
        ("POST", f"/api/flashcards/{D}/review", {"card": 0, "result": "know"}),
        ("GET", f"/api/flashcards/{D}/cards/0/view", None),
        ("GET", f"/api/jobs/{J}", None),
        ("DELETE", f"/api/jobs/{J}", None),
        ("POST", "/api/jobs", {"kind": "quiz", "resource_id": L}),
        (
            "POST",
            "/api/jobs",
            {"kind": "lesson", "parameters": {"topic": "x", "age": 12, "material_id": M}},
        ),
        ("GET", f"/api/rooms/{R}", None),
        ("POST", f"/api/rooms/{R}/commands", {"expected_version": 1, "action": "close"}),
        ("POST", f"/api/rooms/{R}/commands", {"expected_version": 1, "action": "leave"}),
        (
            "POST",
            f"/api/rooms/{R}/commands",
            {"expected_version": 1, "action": "ready", "ready": True},
        ),
    ]


def send(client, method, url, body, headers):
    extra = {"headers": {**headers, "Idempotency-Key": "attacker-key-0001"}}
    if body is not None:
        extra["json"] = body
    return client.request(method, url, **extra)


def test_another_account_is_told_nothing_exists_and_changes_nothing(api, world):
    before = read_everything(api, world)
    attacker = child_headers(api)
    for method, url, body in attacks(world):
        r = send(api, method, url, body, attacker)
        assert r.status_code == 404, (method, url, r.status_code, r.text[:100])
        assert "not found" in r.text.lower() or "unavailable" in r.text.lower()
    assert read_everything(api, world) == before
    # The attacker's own account is untouched too: nothing was created on their behalf.
    for kind in ("materials", "lessons", "quizzes", "tournaments", "decks", "jobs", "completions"):
        assert api.get(f"/api/resources/{kind}", headers=attacker).json()["items"] == [], kind


def test_an_anonymous_caller_is_refused_everywhere_and_changes_nothing(api, world):
    before = read_everything(api, world)
    anonymous = TestClient(api.app)
    for method, url, body in attacks(world):
        r = send(anonymous, method, url, body, {})
        assert r.status_code == 401, (method, url, r.status_code)
    assert read_everything(api, world) == before


def test_errors_for_foreign_and_missing_ids_are_indistinguishable(api, world):
    """An attacker must not be able to tell 'exists but not yours' from 'does not exist'."""
    attacker = child_headers(api)
    fake = {
        **world,
        **{
            k: "0" * 32
            for k in ("material", "lesson", "visual", "quiz", "attempt", "tournament", "deck")
        },
        "job": "0" * 64,
        "room": "00000000-0000-4000-8000-00000000dead",
    }
    for (m1, u1, b1), (m2, u2, b2) in zip(attacks(world), attacks(fake)):
        real, nothing = send(api, m1, u1, b1, attacker), send(api, m2, u2, b2, attacker)
        assert (real.status_code, real.headers.get("content-type")) == (
            nothing.status_code,
            nothing.headers.get("content-type"),
        ), (m1, u1)
        assert real.json() == nothing.json() or u1.startswith("/api/jobs") or "rooms" in u1, (
            m1,
            u1,
            real.text,
            nothing.text,
        )


def test_joining_a_room_by_code_does_not_open_the_hosts_learning_data(api, world):
    other = child_headers(api)
    joined = api.post(
        "/api/rooms/join", headers=other, json={"code": world["code"], "display_name": "Sam"}
    )
    assert joined.status_code == 200
    # Lobby membership grants the lobby and nothing else.
    for url in (
        f"/api/lessons/{world['lesson']}",
        f"/api/quiz/{world['quiz']}",
        f"/api/materials/{world['material']}",
        f"/api/tournament/{world['tournament']}/review",
        "/api/me/profile",
    ):
        r = api.get(url, headers=other)
        assert r.status_code == 404 or url.endswith("profile")
    assert api.get("/api/me/profile", headers=other).json()["display_name"] == "Learner"
    assert api.get(f"/api/rooms/{world['room']}", headers=other).status_code == 200


# ---------------------------------------------------------------- ids of one kind never work as another kind
KINDS = {
    "lesson": [
        ("get", "/api/lessons/{}", None),
        ("post", "/api/lessons/{}/quiz", {}),
        ("post", "/api/lessons/{}/flashcards", {}),
        ("post", "/api/lessons/{}/complete", None),
    ],
    "material": [("get", "/api/materials/{}", None), ("get", "/api/materials/{}/pages", None)],
    "quiz": [("get", "/api/quiz/{}", None), ("post", "/api/quiz/{}/attempts", None)],
    "tournament": [("get", "/api/tournament/{}", None), ("get", "/api/tournament/{}/review", None)],
    "deck": [("get", "/api/flashcards/{}", None)],
    "job": [("get", "/api/jobs/{}", None)],
    "room": [("get", "/api/rooms/{}", None)],
}


def test_an_id_from_one_kind_of_record_is_nothing_in_another(api, world):
    pool = {
        "lesson": [world["lesson"]],
        "material": [world["material"]],
        "quiz": [world["quiz"], world["attempt"]],
        "tournament": [world["tournament"]],
        "deck": [world["deck"]],
        "job": [world["job"]],
        "room": [world["room"]],
        "visual": [world["visual"]],
    }
    checked = 0
    for kind, routes in KINDS.items():
        for method, route, body in routes:
            for other, ids in pool.items():
                if other == kind:
                    continue
                for ident in ids:
                    extra = {"json": body} if body is not None else {}
                    r = getattr(api, method)(route.format(ident), **extra)
                    assert r.status_code == 404, (route, other, r.status_code)
                    checked += 1
    assert checked > 80


def test_a_practice_quiz_id_cannot_be_scored_as_a_tournament_and_vice_versa(api, world):
    assert (
        api.post(f"/api/tournament/{world['quiz']}/submit", json={"answers": [0, 0]}).status_code
        == 404
    )
    assert (
        api.post(
            f"/api/tournament/{world['quiz']}/approve",
            json={"expected_revision": 1, "confirmed": True},
        ).status_code
        == 404
    )
    assert (
        api.post(
            f"/api/quiz/{world['tournament']}/check", json={"index": 0, "choice": 0}
        ).status_code
        == 404
    )
    assert api.post(f"/api/quiz/{world['tournament']}/followup", json={}).status_code == 404
    assert api.get(f"/api/tournament/{world['quiz']}/review").status_code == 404
