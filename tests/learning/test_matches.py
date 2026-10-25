"""Playing challenges: the server-clocked engines for the timed quiz, heads-up and the diagram race, and the modes
(solo, friends, parent_child, duo). A fake clock drives every timing rule; the AI that writes the content is faked."""

import json

import pytest

import learning_service.matches as matches
from learning_service.matches import MatchError
from tests.learning.test_challenges import create, make_room

ALEX = "00000000-0000-4000-8000-000000000001"  # the default player in the test client
SAM = "00000000-0000-4000-8000-000000000002"
KIM = "00000000-0000-4000-8000-000000000003"
LEE = "00000000-0000-4000-8000-000000000004"
HEADERS = {
    ALEX: {},
    SAM: {"X-Player-ID": SAM},
    KIM: {"X-Player-ID": KIM},
    LEE: {"X-Player-ID": LEE},
}
SECOND = 1000


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(matches, "clock", fake)
    return fake


@pytest.fixture
def store(client):
    return client.app.state.memory


@pytest.fixture(autouse=True)
def named_players(store):
    profiles(store, alex="learner", sam="learner", kim="learner", lee="learner")


def profiles(store, **roles):
    for pid, role in roles.items():
        store.set_profile(
            {"alex": ALEX, "sam": SAM, "kim": KIM, "lee": LEE}[pid], role, pid.title()
        )


def setup(client, game, mode, others=(), teams=None, **challenge):
    """A challenge for a room of `others` plus the default player, and a match over it. Returns (match id, challenge id)."""
    room = make_room(client, *[(HEADERS[pid], pid) for pid in others]) if others else None
    made = create(client, game, room_id=room, **challenge)
    body = {"challenge_id": made["id"], "mode": mode} | ({"teams": teams} if teams else {})
    r = client.post("/api/matches", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"], made["id"]


def start(client, mid, clock):
    r = client.post(f"/api/matches/{mid}/start")
    assert r.status_code == 200, r.text
    return r.json()["play_at_ms"]


def state(client, mid, who=ALEX):
    r = client.get(f"/api/matches/{mid}", headers=HEADERS[who])
    assert r.status_code == 200, r.text
    return r.json()


def answer(client, mid, index, choice, who=ALEX):
    return client.post(
        f"/api/matches/{mid}/answer",
        headers=HEADERS[who],
        json={"question_index": index, "choice": choice},
    )


CORRECT = 1  # in the fake quiz the right option is always the second one
QUESTION = 30 * SECOND
CYCLE = QUESTION + matches.MCQ_GAP_MS


# ---------------------------------------------------------------- pure rules
def test_equal_results_share_a_rank():
    rows = matches.ranked([{"k": 3}, {"k": 1}, {"k": 1}, {"k": 2}], key=lambda r: r["k"])
    assert [(r["k"], r["rank"]) for r in rows] == [(1, 1), (1, 1), (2, 3), (3, 4)]


def test_the_parent_gets_two_thirds_of_the_time_in_the_modes_that_say_so():
    doc = {
        "mode": "parent_child",
        "players": [{"player_id": "p", "role": "parent"}, {"player_id": "c", "role": "learner"}],
    }
    assert matches.seconds_for(doc, "p", 30) == 20 and matches.seconds_for(doc, "c", 30) == 30
    assert matches.seconds_for(doc, "p", 6) == matches.MIN_SECONDS  # never below a few seconds
    assert (
        matches.seconds_for(doc | {"mode": "friends"}, "p", 30) == 30
    )  # friends play on equal terms
    assert matches.seconds_for(doc | {"mode": "duo"}, "p", 30) == 20


def test_every_player_guesses_once_and_in_duo_the_teammate_explains():
    four = [{"player_id": x} for x in "abcd"]
    assert matches.turn_plan(four[:3], None) == [("a", "b"), ("b", "c"), ("c", "a")]
    assert matches.turn_plan(four, [["a", "b"], ["c", "d"]]) == [
        ("a", "b"),
        ("c", "d"),
        ("b", "a"),
        ("d", "c"),
    ]


# ---------------------------------------------------------------- making a match
def test_a_match_needs_a_challenge_of_your_own_and_a_valid_mode(client, models, store):
    cid = create(client, "mcq")["id"]
    assert (
        client.post(
            "/api/matches", headers=HEADERS[SAM], json={"challenge_id": cid, "mode": "solo"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/matches", json={"challenge_id": "nope", "mode": "solo"}).status_code
        == 404
    )
    assert (
        client.post("/api/matches", json={"challenge_id": cid, "mode": "chess"}).status_code == 422
    )
    friends = client.post("/api/matches", json={"challenge_id": cid, "mode": "friends"})
    assert (
        friends.status_code == 422 and "room_id" in friends.json()["detail"]
    )  # nobody to play with
    heads = create(client, "heads_up")["id"]
    solo = client.post("/api/matches", json={"challenge_id": heads, "mode": "solo"})
    assert solo.status_code == 422 and "at least two" in solo.json()["detail"]
    assert (
        client.post(
            "/api/matches", json={"challenge_id": cid, "mode": "solo", "teams": [["a", "b"]]}
        ).status_code
        == 422
    )


def test_parent_child_needs_one_parent_and_one_learner_with_profiles(client, models, store):
    store.forget_player(ALEX)
    store.forget_player(SAM)  # no memory profiles at all
    room = make_room(client, (HEADERS[SAM], "Sam"))
    cid = create(client, "mcq", room_id=room)["id"]
    body = {"challenge_id": cid, "mode": "parent_child"}
    r = client.post("/api/matches", json=body)
    assert r.status_code == 422 and "memory profile" in r.json()["detail"]
    profiles(store, alex="parent", sam="parent")
    assert client.post("/api/matches", json=body).status_code == 422  # two parents
    profiles(store, sam="learner")
    ok = client.post("/api/matches", json=body)
    assert ok.status_code == 201 and {p["role"] for p in ok.json()["players"]} == {
        "parent",
        "learner",
    }


def test_duo_needs_four_players_in_two_teams_of_two_and_is_not_for_the_diagram(
    client, models, store
):
    room = make_room(client, (HEADERS[SAM], "Sam"), (HEADERS[KIM], "Kim"), (HEADERS[LEE], "Lee"))
    mcq = create(client, "mcq", room_id=room)["id"]
    for teams in (
        None,
        [[ALEX, SAM]],
        [[ALEX, SAM, KIM], [LEE, "x"]],
        [[ALEX, SAM], [KIM, "stranger"]],
    ):
        body = {"challenge_id": mcq, "mode": "duo"} | ({"teams": teams} if teams else {})
        assert client.post("/api/matches", json=body).status_code == 422
    ok = client.post(
        "/api/matches",
        json={"challenge_id": mcq, "mode": "duo", "teams": [[ALEX, SAM], [KIM, LEE]]},
    )
    assert ok.status_code == 201 and [p["team"] for p in ok.json()["players"]] == [0, 0, 1, 1]
    diagram = create(client, "diagram", room_id=room)["id"]
    r = client.post(
        "/api/matches",
        json={"challenge_id": diagram, "mode": "duo", "teams": [[ALEX, SAM], [KIM, LEE]]},
    )
    assert r.status_code == 422 and "diagram" in r.json()["detail"]
    pair = make_room(client, (HEADERS[SAM], "Sam"))
    two = create(client, "mcq", room_id=pair)["id"]
    assert (
        client.post(
            "/api/matches",
            json={"challenge_id": two, "mode": "duo", "teams": [[ALEX, SAM], [KIM, LEE]]},
        ).status_code
        == 422
    )


def test_only_the_players_see_a_match_and_only_the_creator_starts_it(client, models, clock):
    mid, _ = setup(client, "mcq", "friends", others=[SAM])
    assert client.get(f"/api/matches/{mid}", headers=HEADERS[KIM]).status_code == 404
    assert client.get(f"/api/matches/{mid}", headers=HEADERS[SAM]).status_code == 200
    assert client.post(f"/api/matches/{mid}/start", headers=HEADERS[SAM]).status_code == 403
    assert client.post(f"/api/matches/{mid}/start", headers=HEADERS[KIM]).status_code == 404
    assert answer(client, mid, 0, 0, KIM).status_code == 404
    assert client.get(f"/api/matches/{mid}/results", headers=HEADERS[KIM]).status_code == 404
    assert client.get("/api/matches/nope").status_code == 404


def test_starting_twice_changes_nothing(client, models, clock):
    mid, _ = setup(client, "mcq", "solo")
    first = start(client, mid, clock)
    clock.now += 2 * SECOND
    assert start(client, mid, clock) == first == 1_000_000 + matches.COUNTDOWN_MS


# ---------------------------------------------------------------- the timed quiz
def test_a_solo_quiz_runs_on_the_servers_clock_from_countdown_to_results(client, models, clock):
    mid, _ = setup(client, "mcq", "solo", count=5, seconds=30)
    waiting = state(client, mid)
    assert (waiting["phase"], waiting["play_at_ms"]) == ("waiting", None)
    play = start(client, mid, clock)
    assert (
        state(client, mid)["phase"] == "countdown"
        and state(client, mid)["server_time_ms"] == clock.now
    )

    clock.now = play + 1 * SECOND
    view = state(client, mid)["view"]
    assert (
        view["phase"] == "question"
        and view["question"]["index"] == 0
        and view["total_questions"] == 5
    )
    assert view["question"]["closes_at_ms"] == play + QUESTION and view["reveals"] == []
    assert "answer_index" not in json.dumps(view) and "Because" not in json.dumps(
        view
    )  # no key while it is open

    assert answer(client, mid, 0, CORRECT).json() == {
        "question_index": 0,
        "answered": True,
        "elapsed_ms": 1000,
    }
    assert state(client, mid)["view"]["question"]["answered"] is True
    assert (
        answer(client, mid, 0, CORRECT).status_code == 200
    )  # the same answer again is a harmless replay
    assert answer(client, mid, 0, 0).status_code == 409  # a different one is not allowed
    assert answer(client, mid, 1, CORRECT).status_code == 409  # question 1 is not open yet

    clock.now = play + QUESTION + matches.GRACE_MS + 1  # question 0 is over for everyone
    view = state(client, mid)["view"]
    assert view["phase"] == "reveal" and view["next_question_opens_at_ms"] == play + CYCLE
    reveal = view["reveals"][0]
    assert (reveal["answer_index"], reveal["explanation"]) == (CORRECT, "Because 0.")
    assert reveal["answers"] == [
        {"name": "Alex", "choice": CORRECT, "correct": True, "elapsed_ms": 1000}
    ]
    assert view["scores"] == [{"name": "Alex", "correct": 1}]

    for i in range(1, 5):  # answer the rest: 1, 3 right; 2, 4 wrong
        clock.now = play + i * CYCLE + 2 * SECOND
        assert answer(client, mid, i, CORRECT if i % 2 else 0).status_code == 200
    assert client.get(f"/api/matches/{mid}/results").status_code == 409  # not over yet
    clock.now = play + 4 * CYCLE + QUESTION + matches.GRACE_MS + 1
    assert state(client, mid)["phase"] == "finished"
    result = client.get(f"/api/matches/{mid}/results").json()
    assert result["ranking"] == [
        {
            "id": ALEX,
            "name": "Alex",
            "members": ["Alex"],
            "correct": 3,
            "time_ms": 5000,
            "rank": 1,
            "total": 5,
            "winner": True,
        }
    ]
    assert len(state(client, mid)["view"]["reveals"]) == 5


def test_answer_timing_rules(client, models, clock):
    mid, _ = setup(client, "mcq", "solo", count=5, seconds=30)
    assert answer(client, mid, 0, 0).status_code == 409  # not started
    play = start(client, mid, clock)
    clock.now = play - 1
    assert answer(client, mid, 0, 0).status_code == 409  # still counting down
    clock.now = play + QUESTION + matches.GRACE_MS  # the network allowance
    assert answer(client, mid, 0, 0).status_code == 200
    clock.now = play + CYCLE + QUESTION + matches.GRACE_MS + 1
    late = answer(client, mid, 1, 0)
    assert late.status_code == 409 and "Time is up" in late.json()["detail"]
    assert answer(client, mid, 99, 0).status_code == 422
    assert (
        client.post(
            f"/api/matches/{mid}/answer", json={"question_index": 0, "choice": 4}
        ).status_code
        == 422
    )
    assert client.post(f"/api/matches/{mid}/answer", json={"question_index": 0}).status_code == 422
    assert (
        client.post(
            f"/api/matches/{mid}/cards", json={"action": "skip", "card_index": 0}
        ).status_code
        == 409
    )  # wrong game


def test_friends_play_the_same_questions_and_answers_stay_secret_until_the_question_closes(
    client, models, clock
):
    mid, _ = setup(client, "mcq", "friends", others=[SAM], count=5, seconds=30)
    play = start(client, mid, clock)
    clock.now = play + 3 * SECOND
    assert answer(client, mid, 0, CORRECT, ALEX).status_code == 200
    mine, theirs = state(client, mid, ALEX)["view"], state(client, mid, SAM)["view"]
    assert mine["question"]["question"] == theirs["question"]["question"]  # identical for both
    assert (
        theirs["question"]["answered"] is False
        and theirs["reveals"] == []
        and mine["reveals"] == []
    )
    clock.now = play + 5 * SECOND
    assert answer(client, mid, 0, 0, SAM).status_code == 200
    clock.now = play + QUESTION + matches.GRACE_MS + 1
    reveal = state(client, mid, SAM)["view"]["reveals"][0]
    assert [(a["name"], a["correct"]) for a in reveal["answers"]] == [
        ("Alex", True),
        ("Sam", False),
    ]
    assert [s["correct"] for s in state(client, mid)["view"]["scores"]] == [1, 0]


def test_the_ranking_is_most_correct_then_less_time_and_ties_share_a_rank(client, models, clock):
    mid, _ = setup(client, "mcq", "friends", others=[SAM], count=5, seconds=30)
    play = start(client, mid, clock)
    for i, (alex_at, sam_at) in enumerate([(2, 5), (2, 5), (9, 4), (3, 3), (3, 3)]):
        clock.now = play + i * CYCLE + alex_at * SECOND
        answer(client, mid, i, CORRECT, ALEX)
        clock.now = play + i * CYCLE + sam_at * SECOND
        answer(client, mid, i, CORRECT, SAM)
    clock.now = play + 4 * CYCLE + QUESTION + matches.GRACE_MS + 1
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert [(r["name"], r["correct"], r["time_ms"], r["rank"], r["winner"]) for r in ranking] == [
        ("Alex", 5, 19000, 1, True),  # same score, faster in total
        ("Sam", 5, 20000, 2, False),
    ]
    mid, _ = setup(client, "mcq", "friends", others=[SAM], count=5, seconds=30)
    play = start(client, mid, clock)
    for who in (ALEX, SAM):  # identical answers at identical times: a tie
        clock.now = play + 4 * SECOND
        answer(client, mid, 0, CORRECT, who)
    clock.now = play + 4 * CYCLE + QUESTION + matches.GRACE_MS + 1
    tied = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert [(r["rank"], r["winner"]) for r in tied] == [(1, True), (1, True)]


def test_nobody_wins_a_game_nobody_scored_in(client, models, clock):
    mid, _ = setup(client, "mcq", "solo", count=5, seconds=30)
    play = start(client, mid, clock)
    clock.now = play + 4 * CYCLE + QUESTION + matches.GRACE_MS + 1
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert ranking[0]["correct"] == 0 and ranking[0]["winner"] is False


def test_the_parent_has_less_time_to_answer(client, models, clock, store):
    profiles(store, alex="parent", sam="learner")
    mid, _ = setup(client, "mcq", "parent_child", others=[SAM], count=5, seconds=30)
    play = start(client, mid, clock)
    clock.now = play + SECOND
    parent, child = state(client, mid, ALEX)["view"], state(client, mid, SAM)["view"]
    assert (parent["your_seconds"], child["your_seconds"]) == (20, 30)
    assert (
        parent["question"]["your_closes_at_ms"] == play + 20 * SECOND
        and child["question"]["your_closes_at_ms"] == play + 30 * SECOND
    )
    assert (
        parent["question"]["closes_at_ms"] == child["question"]["closes_at_ms"]
    )  # the screen moves on together
    clock.now = play + 21 * SECOND  # after the parent's 20 s, still inside the child's 30
    late = answer(client, mid, 0, CORRECT, ALEX)
    assert late.status_code == 409 and "Time is up" in late.json()["detail"]
    assert answer(client, mid, 0, CORRECT, SAM).status_code == 200
    clock.now = play + CYCLE + 19 * SECOND  # the next question: 19 s is inside the parent's 20
    assert answer(client, mid, 1, CORRECT, ALEX).json()["elapsed_ms"] == 19 * SECOND


def test_in_duo_a_teams_points_are_its_members_added_together(client, models, clock, store):
    profiles(store, alex="parent", sam="learner", kim="parent", lee="learner")
    mid, _ = setup(
        client,
        "mcq",
        "duo",
        others=[SAM, KIM, LEE],
        teams=[[ALEX, SAM], [KIM, LEE]],
        count=5,
        seconds=30,
    )
    play = start(client, mid, clock)
    plan = {
        0: {ALEX: CORRECT, SAM: CORRECT, KIM: CORRECT, LEE: 0},
        1: {ALEX: CORRECT, SAM: 0, KIM: 0, LEE: 0},
    }
    for i, choices in plan.items():
        clock.now = play + i * CYCLE + 4 * SECOND
        for who, choice in choices.items():
            assert answer(client, mid, i, choice, who).status_code == 200
    clock.now = play + 4 * CYCLE + QUESTION + matches.GRACE_MS + 1
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert [(r["name"], r["correct"], r["rank"]) for r in ranking] == [
        ("Alex & Sam", 3, 1),
        ("Kim & Lee", 1, 2),
    ]
    assert ranking[0]["members"] == ["Alex", "Sam"] and ranking[0]["winner"] is True
    assert [r["total"] for r in ranking] == [
        10,
        10,
    ]  # two players answer all 5 questions, so a team can score 10
    assert [p["name"] for p in state(client, mid, LEE)["players"]] == ["Alex", "Sam", "Kim", "Lee"]


# ---------------------------------------------------------------- heads-up
TURN = 60 * SECOND
HANDOVER = matches.HEADS_UP_HANDOVER_MS
HU_CYCLE = TURN + HANDOVER


def card_of(client, mid, who):
    view = state(client, mid, who)["view"]["turn"]
    return view.get("card") if view else None


def mark(client, mid, action, index, who=ALEX):
    return client.post(
        f"/api/matches/{mid}/cards",
        headers=HEADERS[who],
        json={"action": action, "card_index": index},
    )


def test_heads_up_turns_alternate_and_only_the_two_playing_see_the_card(client, models, clock):
    mid, _ = setup(client, "heads_up", "friends", others=[SAM, KIM], count=10, seconds=60)
    play = start(client, mid, clock)
    assert state(client, mid)["view"]["phase"] == "countdown" and card_of(client, mid, ALEX) is None
    clock.now = play + SECOND
    view = state(client, mid, ALEX)["view"]
    assert view["phase"] == "turn"
    assert [(t["guesser"], t["explainer"]) for t in view["turns"]] == [
        ("Alex", "Sam"),
        ("Sam", "Kim"),
        ("Kim", "Alex"),
    ]
    turn = view["turn"]
    assert (turn["guesser"], turn["explainer"], turn["you_are"]) == ("Alex", "Sam", "guesser")
    assert state(client, mid, SAM)["view"]["turn"]["you_are"] == "explainer"
    watcher = state(client, mid, KIM)["view"]["turn"]
    assert (
        watcher["you_are"] == "watching" and "card" not in watcher
    )  # the third player sees no card
    first = card_of(client, mid, ALEX)
    assert (
        first["index"] == 0
        and 3 <= len(first["forbidden"]) <= 5
        and card_of(client, mid, SAM) == first
    )


def test_marking_cards_scores_the_guesser_and_each_card_is_dealt_once(client, models, clock):
    mid, _ = setup(client, "heads_up", "friends", others=[SAM], count=10, seconds=60)
    play = start(client, mid, clock)
    clock.now = play + 5 * SECOND
    shown = [card_of(client, mid, ALEX)["term"]]
    assert mark(client, mid, "correct", 0, ALEX).json() == {"replayed": False, "next_card_index": 1}
    assert mark(client, mid, "correct", 0, ALEX).json() == {
        "replayed": True,
        "next_card_index": 1,
    }  # a double tap
    assert mark(client, mid, "skip", 3).status_code == 409  # not dealt yet
    shown.append(card_of(client, mid, SAM)["term"])  # the explainer can see and mark too
    assert mark(client, mid, "skip", 1, SAM).status_code == 200
    assert mark(client, mid, "correct", 2, ALEX).status_code == 200
    assert mark(client, mid, "violation", 3, SAM).status_code == 200
    assert len(set(shown)) == 2 and state(client, mid)["view"]["cards_left"] == 10 - 4
    assert (
        client.post(
            f"/api/matches/{mid}/cards", json={"action": "dance", "card_index": 4}
        ).status_code
        == 422
    )


def test_only_the_two_players_of_the_turn_may_mark_and_only_while_it_lasts(client, models, clock):
    mid, _ = setup(client, "heads_up", "friends", others=[SAM, KIM], count=10, seconds=60)
    play = start(client, mid, clock)
    assert mark(client, mid, "correct", 0, ALEX).status_code == 409  # still counting down
    clock.now = play + SECOND
    assert mark(client, mid, "correct", 0, KIM).status_code == 403  # watching
    clock.now = play + TURN + matches.GRACE_MS
    assert mark(client, mid, "correct", 0, ALEX).status_code == 200  # the network allowance
    clock.now = play + TURN + matches.GRACE_MS + 1
    assert mark(client, mid, "correct", 1, ALEX).status_code == 409  # the turn is over
    clock.now = play + TURN + 5 * SECOND
    view = state(client, mid)["view"]
    assert view["phase"] == "handover" and view["next_turn_starts_at_ms"] == play + HU_CYCLE
    clock.now = play + HU_CYCLE + SECOND  # turn 1: SAM guesses, KIM explains
    assert mark(client, mid, "correct", 1, ALEX).status_code == 403
    assert mark(client, mid, "correct", 1, KIM).status_code == 200
    assert state(client, mid, SAM)["view"]["turn"]["you_are"] == "guesser"


def test_heads_up_scores_results_and_winner(client, models, clock):
    mid, _ = setup(client, "heads_up", "friends", others=[SAM], count=10, seconds=60)
    play = start(client, mid, clock)
    clock.now = play + SECOND  # turn 0: ALEX guesses 3 right, 1 violation, 1 skip
    for i, action in enumerate(["correct", "correct", "violation", "correct", "skip"]):
        assert mark(client, mid, action, i, ALEX).status_code == 200
    clock.now = (
        play + HU_CYCLE + SECOND
    )  # turn 1: SAM guesses 1 right and 2 violations (never below zero)
    for i, action in zip(range(5, 8), ["correct", "violation", "violation"]):
        assert mark(client, mid, action, i, SAM).status_code == 200
    assert [s["score"] for s in state(client, mid)["view"]["scores"]] == [2, 0]
    assert client.get(f"/api/matches/{mid}/results").status_code == 409
    clock.now = play + HU_CYCLE + TURN + matches.GRACE_MS + 1
    assert state(client, mid)["phase"] == "finished"
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert [(r["name"], r["score"], r["rank"], r["winner"]) for r in ranking] == [
        ("Alex", 2, 1, True),
        ("Sam", 0, 2, False),
    ]


def test_the_parent_has_a_shorter_turn_and_in_duo_the_teams_play_two_turns_each(
    client, models, clock, store
):
    profiles(store, alex="parent", sam="learner")
    mid, _ = setup(client, "heads_up", "parent_child", others=[SAM], count=10, seconds=60)
    play = start(client, mid, clock)
    turns = state(client, mid)["view"]["turns"]
    assert [(t["guesser"], t["seconds"]) for t in turns] == [("Alex", 40), ("Sam", 60)]
    clock.now = play + 41 * SECOND  # the parent's 40 s are over, the child's would not be
    assert mark(client, mid, "correct", 0, ALEX).status_code == 409

    profiles(store, kim="parent", lee="learner")
    duo, _ = setup(
        client,
        "heads_up",
        "duo",
        others=[SAM, KIM, LEE],
        teams=[[ALEX, SAM], [KIM, LEE]],
        count=30,
        seconds=60,
    )
    plan = state(client, duo)["view"]["turns"]
    assert [(t["guesser"], t["explainer"]) for t in plan] == [
        ("Alex", "Sam"),
        ("Kim", "Lee"),
        ("Sam", "Alex"),
        ("Lee", "Kim"),
    ]
    assert [t["seconds"] for t in plan] == [40, 40, 60, 60]


def test_cards_run_out_without_breaking_the_turn(client, models, clock):
    mid, _ = setup(client, "heads_up", "friends", others=[SAM], count=8, seconds=60)  # only 8 cards
    play = start(client, mid, clock)
    clock.now = play + SECOND
    for i in range(8):
        assert mark(client, mid, "correct", i).status_code == 200
    assert mark(client, mid, "correct", 8).status_code == 409
    view = state(client, mid)["view"]
    assert view["cards_left"] == 0 and "card" not in view["turn"]


# ---------------------------------------------------------------- the diagram race
DIAGRAM = 120 * SECOND


def answers_of(client, challenge_id):
    review = client.get(f"/api/challenges/{challenge_id}/review").json()["content"]
    return review["answers"], review["options"]


def arrangement(client, challenge_id, wrong=False):
    """The right placements (slot -> word index); with `wrong`, the words of the first two slots swapped."""
    answers, options = answers_of(client, challenge_id)
    placements = {slot: options.index(label) for slot, label in answers.items()}
    if wrong:
        a, b = list(placements)[:2]
        placements[a], placements[b] = placements[b], placements[a]
    return placements


def place(client, mid, placements, who=ALEX):
    return client.put(
        f"/api/matches/{mid}/placements", headers=HEADERS[who], json={"placements": placements}
    )


def finish(client, mid, who=ALEX):
    return client.post(f"/api/matches/{mid}/finish", headers=HEADERS[who])


def test_a_wrong_finish_adds_a_penalty_but_never_reveals_the_answers(client, models, clock):
    mid, cid = setup(client, "diagram", "solo", seconds=120)
    board = state(client, mid)["view"]["board"]
    assert (
        len(board["slots"]) == 4
        and "?" in board["mermaid"]
        and "answers" not in json.dumps(state(client, mid))
    )
    play = start(client, mid, clock)
    clock.now = play + 10 * SECOND
    assert finish(client, mid).status_code == 409  # blanks are empty
    right, wrong = arrangement(client, cid), arrangement(client, cid, wrong=True)
    assert place(client, mid, {list(right)[0]: 0}).json() == {
        "placed": 1,
        "slots": 4,
    }  # partial is fine
    assert finish(client, mid).status_code == 409
    assert place(client, mid, wrong).status_code == 200
    clock.now = play + 20 * SECOND
    miss = finish(client, mid).json()
    assert (
        miss["perfect"] is False
        and miss["correct"] == 2
        and len(miss["wrong_slots"]) == 2
        and miss["penalty_seconds"] == 10
    )
    assert set(miss) == {
        "perfect",
        "correct",
        "wrong_slots",
        "wrong_finishes",
        "penalty_seconds",
    }  # no right words in it
    assert state(client, mid)["view"]["you"]["wrong_finishes"] == 1
    assert place(client, mid, right).status_code == 200  # the arrangement is replaced
    clock.now = play + 35 * SECOND
    done = finish(client, mid).json()
    assert done == {
        "perfect": True,
        "elapsed_ms": 35000,
        "wrong_finishes": 1,
        "total_ms": 45000,
    }  # +10 s for the miss
    assert (
        place(client, mid, right).status_code == 409 and finish(client, mid).status_code == 409
    )  # already finished
    assert state(client, mid)["phase"] == "finished"
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert (
        ranking[0]["total_ms"] == 45000
        and ranking[0]["elapsed_ms"] == 35000
        and ranking[0]["winner"] is True
    )


def test_the_race_ranks_perfect_reconstructions_by_time_plus_penalties(client, models, clock):
    mid, cid = setup(client, "diagram", "friends", others=[SAM, KIM], seconds=120)
    play = start(client, mid, clock)
    right, wrong = arrangement(client, cid), arrangement(client, cid, wrong=True)
    clock.now = play + 20 * SECOND  # SAM: a miss at 20 s, then perfect at 25 s: 25 + 10 = 35 s
    place(client, mid, wrong, SAM)
    assert finish(client, mid, SAM).json()["perfect"] is False
    clock.now = play + 25 * SECOND
    place(client, mid, right, SAM)
    assert finish(client, mid, SAM).json()["total_ms"] == 35000
    clock.now = play + 30 * SECOND  # ALEX: perfect first time at 30 s
    place(client, mid, right, ALEX)
    assert finish(client, mid, ALEX).json()["total_ms"] == 30000
    assert client.get(f"/api/matches/{mid}/results").status_code == 409  # KIM is still playing
    clock.now = play + DIAGRAM + matches.GRACE_MS + 1  # time is up for KIM, who never finished
    ranking = client.get(f"/api/matches/{mid}/results").json()["ranking"]
    assert [(r["name"], r["rank"], r["winner"], r["total_ms"]) for r in ranking] == [
        ("Alex", 1, True, 30000),
        ("Sam", 2, False, 35000),
        ("Kim", None, False, None),  # unfinished cannot beat a perfect diagram
    ]
    assert (
        place(client, mid, right, KIM).status_code == 409
        and "up" in place(client, mid, right, KIM).json()["detail"]
    )


def test_the_parent_has_less_time_to_finish_the_diagram(client, models, clock, store):
    profiles(store, alex="parent", sam="learner")
    mid, cid = setup(client, "diagram", "parent_child", others=[SAM], seconds=120)
    play = start(client, mid, clock)
    assert (
        state(client, mid, ALEX)["view"]["your_seconds"],
        state(client, mid, SAM)["view"]["your_seconds"],
    ) == (80, 120)
    right = arrangement(client, cid)
    clock.now = play + 81 * SECOND
    assert place(client, mid, right, ALEX).status_code == 409  # the parent's 80 s are over
    assert (
        place(client, mid, right, SAM).status_code == 200
        and finish(client, mid, SAM).json()["perfect"] is True
    )


def test_bad_placements_are_refused(client, models, clock):
    mid, _ = setup(client, "diagram", "solo", seconds=120)
    assert place(client, mid, {}).status_code == 409  # not started
    clock.now = start(client, mid, clock) + SECOND
    slot = state(client, mid)["view"]["board"]["slots"][0]
    assert place(client, mid, {"nope": 0}).status_code == 422
    assert place(client, mid, {slot: 99}).status_code == 422
    other = state(client, mid)["view"]["board"]["slots"][1]
    assert place(client, mid, {slot: 0, other: 0}).status_code == 422  # one word, two places
    assert state(client, mid)["view"]["you"]["placements"] == {}  # nothing was saved


# ---------------------------------------------------------------- housekeeping
def test_an_expired_match_is_gone_and_deleting_my_data_removes_my_matches(
    client, models, clock, store
):
    mid, _ = setup(client, "mcq", "solo")
    with store._tx() as db:
        db.execute(
            "update matches set expires_at = '2000-01-01T00:00:00+00:00' where id = ?", (mid,)
        )
    assert client.get(f"/api/matches/{mid}").status_code == 404
    mine, _ = setup(client, "mcq", "solo")
    assert client.delete("/api/me/data").status_code == 204
    assert client.get(f"/api/matches/{mine}").status_code == 404


def test_a_refusal_never_changes_the_match(client, models, clock, store):
    mid, _ = setup(client, "mcq", "solo", count=5, seconds=30)
    play = start(client, mid, clock)
    clock.now = play + SECOND
    assert answer(client, mid, 0, CORRECT).status_code == 200
    assert answer(client, mid, 0, 0).status_code == 409
    saved = store.get_match(ALEX, mid)
    assert saved["answers"][ALEX] == {"0": {"choice": CORRECT, "elapsed_ms": 1000}}


def test_the_engine_refuses_with_a_status_and_a_message():
    with pytest.raises(MatchError) as error:
        matches.start({"creator_id": "a", "play_at_ms": None}, "b", 0)
    assert error.value.status == 403 and "created the match" in error.value.message
