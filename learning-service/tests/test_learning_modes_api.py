"""Flashcard endpoints, and diagram questions/cards mixed into quizzes and decks automatically."""


def with_diagram(client, lesson_id):
    """Draw the lesson's visuals (the fake harness returns one diagram)."""
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    assert r.status_code == 201 and r.json()["visuals"][0]["kind"] == "diagram"


# ---------------------------------------------------------------- flashcards
def create_deck(client, lesson_id, n=5):
    r = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": n})
    assert r.status_code == 201, r.text
    return r.json()


def test_deck_without_diagrams_is_all_concept_cards(client, lesson_id):
    deck = create_deck(client, lesson_id, n=5)
    assert [c["kind"] for c in deck["cards"]] == ["concept"] * 5
    assert client.seen["cards_request"] == ("computer networks", 12, 5)
    assert deck["state"] == {"next": 0, "known": 0, "total": 5, "reviews": 0, "hard": []}
    assert client.get(f"/api/flashcards/{deck['id']}").json() == deck


def test_deck_mixes_in_diagram_cards_automatically(client, lesson_id):
    with_diagram(client, lesson_id)
    deck = create_deck(client, lesson_id, n=6)
    kinds = [c["kind"] for c in deck["cards"]]
    assert kinds.count("diagram") == 1 and len(kinds) == 6  # one diagram -> one hidden box; deck still exactly 6
    assert client.seen["cards_request"][2] == 6 and client.seen["blank_calls"] == [1]
    card = deck["cards"][kinds.index("diagram")]
    assert "Firewall" not in card["front_visual_html"] and "Firewall" in card["back_visual_html"]
    assert card["back"].startswith("Firewall")


def test_review_flow_until_done(client, lesson_id):
    deck = create_deck(client, lesson_id, n=3)
    url = f"/api/flashcards/{deck['id']}/review"
    s = client.post(url, json={"card": 0, "result": "again"}).json()
    assert s["next"] == 1 and s["hard"] == [0] and s["reviews"] == 1
    for card in (1, 2, 0):
        s = client.post(url, json={"card": card, "result": "know"}).json()
    assert s == {"next": None, "known": 3, "total": 3, "reviews": 4, "hard": [0]}


def test_review_errors(client, lesson_id):
    deck = create_deck(client, lesson_id, n=3)
    url = f"/api/flashcards/{deck['id']}/review"
    client.post(url, json={"card": 0, "result": "know"})
    assert client.post(url, json={"card": 0, "result": "know"}).status_code == 422   # already known
    assert client.post(url, json={"card": 1, "result": "maybe"}).status_code == 422  # bad result
    assert client.post("/api/flashcards/nope/review", json={"card": 0, "result": "know"}).status_code == 404
    assert client.post("/api/lessons/nope/flashcards", json={}).status_code == 404
    assert client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 2}).status_code == 422


def test_deck_failure_is_502(client, lesson_id, monkeypatch):
    import store

    def boom(*_a):
        raise RuntimeError("model down")

    monkeypatch.setattr(store, "make_cards", boom)
    r = client.post(f"/api/lessons/{lesson_id}/flashcards", json={})
    assert r.status_code == 502 and "model down" in r.json()["detail"]


# ---------------------------------------------------------------- diagram questions in the quiz
def test_quiz_without_diagrams_has_no_picture_questions(client, lesson_id):
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3}).json()
    assert all(q["visual_html"] is None for q in quiz["questions"])
    assert client.seen["quiz_request"].num_questions == 3
    assert "blank_calls" not in client.seen


def test_quiz_mixes_in_a_diagram_question_automatically(client, lesson_id):
    with_diagram(client, lesson_id)
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 4}).json()
    assert client.seen["quiz_request"].num_questions == 4 - 1  # one diagram -> one picture question reserved
    pictured = [q for q in quiz["questions"] if q["visual_html"]]
    assert len(pictured) == 1  # the fake recall returns one box per call
    q = pictured[0]
    assert q["type"] == "multiple_choice" and sorted(q["options"]) == ["Cable", "Firewall", "Modem", "Router"]
    assert "Firewall" not in q["visual_html"]  # the hidden label never reaches the browser early
    assert [x["index"] for x in quiz["questions"]] == list(range(len(quiz["questions"])))

    right = q["options"].index("Firewall")
    out = client.post(f"/api/quiz/{quiz['id']}/check", json={"index": q["index"], "choice": right}).json()
    assert out["correct"] is True and "Firewall" in out["answer_visual_html"]


def test_one_question_quiz_stays_text_only(client, lesson_id):
    with_diagram(client, lesson_id)
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 1}).json()
    assert client.seen["quiz_request"].num_questions == 1
    assert all(q["visual_html"] is None for q in quiz["questions"])


def test_two_diagrams_give_two_picture_questions_one_each(client, lesson_id):
    with_diagram(client, lesson_id)
    client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "another"})  # second diagram
    quiz = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 5}).json()
    assert client.seen["blank_calls"] == [1, 1]
    kinds = sorted(q["type"] for q in quiz["questions"] if q["visual_html"])
    assert kinds == ["multiple_choice", "true_false"]  # one "what goes here?", one "is this right?"
