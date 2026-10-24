"""Follow-up quiz on the learner's weak spots, and flashcards of their mistakes."""

import learning_service.store as store
from learning_service.weakness import RemedialQuiz
from tests.learning.test_learning_modes_api import with_diagram

# The fake quiz (conftest) has two questions after the flagged one is dropped:
#   0: multiple choice, "Usable IPs in a /24 on AWS?"  options 254/251/256/252, answer 251 (index 1)
#   1: true/false, "NACLs are stateful."               answer False (index 1)


def make_quiz(client, lesson_id, n=3):
    r = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": n})
    assert r.status_code == 201, r.text
    return r.json()


def answer(client, quiz_id, index, choice):
    r = client.post(f"/api/quiz/{quiz_id}/check", json={"index": index, "choice": choice})
    assert r.status_code == 200, r.text
    return r.json()


def followup(client, quiz_id, **body):
    return client.post(f"/api/quiz/{quiz_id}/followup", json=body)


# ---------------------------------------------------------------- recording answers
def test_only_the_first_answer_to_a_question_counts(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)  # wrong: 254
    answer(client, quiz["id"], 0, 1)  # then the right one: must not erase the mistake
    answer(client, quiz["id"], 1, 1)  # right
    assert followup(client, quiz["id"]).status_code == 201
    missed = client.seen["remedial_request"].misses
    assert [(m.index, m.picked) for m in missed] == [(0, "254")]


# ---------------------------------------------------------------- follow-up quiz
def test_followup_needs_a_wrong_answer(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    assert followup(client, quiz["id"]).status_code == 409  # nothing answered yet
    answer(client, quiz["id"], 0, 1)
    answer(client, quiz["id"], 1, 1)
    r = followup(client, quiz["id"])
    assert r.status_code == 409 and "no wrong answers" in r.json()["detail"]


def test_followup_is_built_from_the_misses_and_the_lesson(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    answer(client, quiz["id"], 1, 0)
    r = followup(client, quiz["id"], num_questions=4)
    assert r.status_code == 201
    body = r.json()
    req = client.seen["remedial_request"]
    assert [(m.index, m.question.answer, m.picked) for m in req.misses] == [
        (0, "251", "254"),
        (1, "False", "True"),
    ]
    assert req.topic == "computer networks" and req.age == 12 and req.num_questions == 4
    assert "AWS reserves 5 IPs per subnet" in req.lesson_text
    assert (
        body["focus"] == ["Firewalls"]
        and body["parent_id"] == quiz["id"]
        and body["lesson_id"] == lesson_id
    )
    assert [q["question"] for q in body["questions"]] == ["New question 0?", "New question 1?"]
    assert "Right 0" in str(body["questions"][0]["options"])  # options are shown...
    assert "explanation" not in str(body) and "answer" not in str(body)  # ...answers are not
    assert client.get(f"/api/quiz/{body['id']}").json() == body


def test_followup_title_is_neutral_not_shaming(client, lesson_id):
    """The child-safety checker blocked "...: your weak spots" as shaming, which broke the whole feature."""
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    title = followup(client, quiz["id"]).json()["title"]
    assert title == "How Networks Talk: practice round" and "weak" not in title.lower()


def test_followup_is_a_normal_quiz_and_can_have_its_own_followup(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    fu = followup(client, quiz["id"]).json()
    right = fu["questions"][0]["options"].index("Right 0")
    assert answer(client, fu["id"], 0, right)["correct"] is True
    assert followup(client, fu["id"]).status_code == 409  # nothing missed yet in the follow-up
    answer(client, fu["id"], 1, 1)  # a wrong answer ("Wrong 1a")
    second = followup(client, fu["id"])
    assert second.status_code == 201 and second.json()["parent_id"] == fu["id"]
    assert client.seen["remedial_request"].misses[0].picked == fu["questions"][1]["options"][1]


def test_followup_errors(client, lesson_id, monkeypatch):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    assert followup(client, "nope").status_code == 404
    assert followup(client, quiz["id"], num_questions=0).status_code == 422
    assert followup(client, quiz["id"], num_questions=11).status_code == 422

    def boom(_request):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(store, "make_remedial", boom)
    r = followup(client, quiz["id"])
    assert r.status_code == 502 and "rate limited" not in r.json()["detail"]

    monkeypatch.setattr(store, "make_remedial", lambda _r: RemedialQuiz(focus=[], questions=[]))
    r = followup(client, quiz["id"])
    assert r.status_code == 502 and "reliable" in r.json()["detail"]


def test_followup_for_a_missed_diagram_question_gets_the_diagram_back(client, lesson_id):
    with_diagram(client, lesson_id)
    quiz = make_quiz(client, lesson_id, n=4)
    picture = next(q for q in quiz["questions"] if q["visual_html"])
    wrong = next(i for i, o in enumerate(picture["options"]) if o != "Firewall")
    answer(client, quiz["id"], picture["index"], wrong)
    assert followup(client, quiz["id"]).status_code == 201
    [m] = client.seen["remedial_request"].misses
    assert (
        m.question.node_id == "f" and m.question.answer_visual is not None
    )  # what the harness needs to hide another box
    assert m.picked == picture["options"][wrong]


# ---------------------------------------------------------------- flashcards of mistakes
def quiz_deck(client, quiz_id, **body):
    return client.post(f"/api/quiz/{quiz_id}/flashcards", json=body)


def test_mistake_cards_cover_only_what_was_missed(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)  # wrong
    answer(client, quiz["id"], 1, 1)  # right
    r = quiz_deck(client, quiz["id"])
    assert r.status_code == 201
    deck = r.json()
    assert deck["title"] == "Mistakes to remember" and len(deck["cards"]) == 1
    card = deck["cards"][0]
    assert card["kind"] == "concept" and card["front"] == "Usable IPs in a /24 on AWS?"
    assert card["back"] == "251 - AWS reserves 5. (You chose “254”.)"
    assert deck["state"] == {"next": 0, "known": 0, "total": 1, "reviews": 0, "hard": []}


def test_true_false_mistake_card_reads_as_a_statement(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 1, 0)  # "True" is wrong
    card = quiz_deck(client, quiz["id"]).json()["cards"][0]
    assert card["front"] == "True or false: NACLs are stateful."
    assert card["back"] == "False. NACLs are stateless. (You chose “True”.)"


def test_diagram_mistake_card_hides_the_box_on_the_front_and_shows_it_on_the_back(
    client, lesson_id
):
    with_diagram(client, lesson_id)
    quiz = make_quiz(client, lesson_id, n=4)
    picture = next(q for q in quiz["questions"] if q["visual_html"])
    wrong = next(i for i, o in enumerate(picture["options"]) if o != "Firewall")
    answer(client, quiz["id"], picture["index"], wrong)
    card = quiz_deck(client, quiz["id"]).json()["cards"][0]
    assert card["kind"] == "diagram" and card["front"].startswith("Look at the diagram")
    assert "Firewall" not in card["front_visual_html"] and "Firewall" in card["back_visual_html"]
    assert card["back"].startswith("Firewall - ")


def test_all_cards_mode_includes_right_answers_without_a_you_chose_note(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)  # wrong
    answer(client, quiz["id"], 1, 1)  # right
    deck = quiz_deck(client, quiz["id"], which="all").json()
    assert deck["title"] == "Quiz review" and len(deck["cards"]) == 2
    assert "You chose" in deck["cards"][0]["back"] and "You chose" not in deck["cards"][1]["back"]


def test_mistake_cards_need_a_mistake_and_a_known_quiz(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    r = quiz_deck(client, quiz["id"])
    assert r.status_code == 409 and "no wrong answers" in r.json()["detail"]
    assert quiz_deck(client, "nope").status_code == 404
    assert quiz_deck(client, quiz["id"], which="some").status_code == 422


def test_mistake_deck_uses_the_normal_review_loop(client, lesson_id):
    quiz = make_quiz(client, lesson_id)
    answer(client, quiz["id"], 0, 0)
    answer(client, quiz["id"], 1, 0)
    deck = quiz_deck(client, quiz["id"]).json()
    url = f"/api/flashcards/{deck['id']}/review"
    s = client.post(url, json={"card": 0, "result": "again"}).json()
    assert s["next"] == 1 and s["hard"] == [0]
    client.post(url, json={"card": 1, "result": "know"})
    s = client.post(url, json={"card": 0, "result": "know"}).json()
    assert s["next"] is None and s["known"] == 2
