"""
Playing a challenge: a match is run by the server's clock.

A match is one played instance of a challenge (challenges.py) by its players. There are no background timers: the
server stores when play begins and works out everything else from the time of each request (the phase, which
question or turn is current, whether an answer came in time). Clients only send actions; they never send times.

  modes     solo, friends (individual scores), parent_child (the parent gets less time), duo (two teams of two)
  mcq       questions open in lockstep for everyone, one at a time against a timer, with a short reveal in between
  heads_up  turns in which one player guesses (phone on the forehead) while another explains; the server deals cards
  diagram   everyone races to drag the word bank into the blanks; a wrong finish adds a time penalty

The shared timeline, handicap and ranking rules live here and are pure functions of (match document, now), so the
tests drive them with a fake clock. Storage and access checks are in memory_store.py, the HTTP layer in match_api.py.
Casual play only: times include network delay, and nothing here is an anti-cheat boundary.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from learning_service.memory_store import Profile, StoredChallenge

Mode = Literal["solo", "friends", "parent_child", "duo"]
MODES = ("solo", "friends", "parent_child", "duo")

COUNTDOWN_MS = 5_000  # from "start" to the first question or turn
MCQ_GAP_MS = 3_000  # the pause after each question, in which its answer is shown
HEADS_UP_HANDOVER_MS = 15_000  # between turns, to pass the phone and swap roles
GRACE_MS = 800  # network allowance: an answer may arrive this long after its window
PARENT_FACTOR = 2 / 3  # a parent gets this share of the time (the agreed handicap)
MIN_SECONDS = 5
MATCH_HOURS = 24


class MatchError(Exception):
    """Something the player did is not allowed right now. `status` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def clock() -> int:
    """Server time in epoch milliseconds. A seam: tests replace it."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Creating a match
# --------------------------------------------------------------------------
def seconds_for(doc: dict, player_id: str, base: int) -> int:
    """Time a player gets. The parent in parent_child and duo matches gets two thirds of it."""
    player = next(p for p in doc["players"] if p["player_id"] == player_id)
    if doc["mode"] in ("parent_child", "duo") and player["role"] == "parent":
        return max(MIN_SECONDS, round(base * PARENT_FACTOR))
    return base


def build_players(
    challenge: StoredChallenge,
    creator: str,
    mode: Mode,
    teams: list[list[str]] | None,
    profiles: dict[str, Profile | None],
) -> list[dict]:
    """Who plays, with name, role and team, after checking the mode's rules."""
    if mode == "solo":
        ids = [creator]
        if challenge.game == "heads_up":
            raise MatchError(422, "Heads-up needs at least two players.")
    else:
        ids = list(challenge.players)
        if len(ids) < 2:
            raise MatchError(
                422, "This mode needs more than one player: make the challenge with a room_id."
            )
    if mode == "parent_child":
        roles = sorted((profiles.get(i).role if profiles.get(i) else "") for i in ids)
        if len(ids) != 2 or roles != ["learner", "parent"]:
            raise MatchError(
                422,
                "Parent-child needs exactly one parent and one learner, each with a memory profile "
                "(PUT /api/memory/profile).",
            )
    if mode == "duo":
        if challenge.game == "diagram":
            raise MatchError(422, "Duo is not available for the diagram game yet.")
        flat = [i for team in (teams or []) for i in team]
        if (
            len(ids) != 4
            or not teams
            or len(teams) != 2
            or any(len(team) != 2 for team in teams)
            or sorted(flat) != sorted(ids)
        ):
            raise MatchError(
                422,
                "Duo needs four players split into two teams of two: send teams as [[a, b], [c, d]].",
            )
    elif teams:
        raise MatchError(422, "Teams are only used in duo matches.")
    team_of = {pid: n for n, team in enumerate(teams or []) for pid in team}
    return [
        {
            "player_id": pid,
            "name": (profiles.get(pid).display_name if profiles.get(pid) else f"Player {n}"),
            "role": profiles.get(pid).role if profiles.get(pid) else None,
            "team": team_of.get(pid),
        }
        for n, pid in enumerate(ids, 1)
    ]


def turn_plan(players: list[dict], teams: list[list[str]] | None) -> list[tuple[str, str]]:
    """(guesser, explainer) for each heads-up turn. Everyone guesses once; in duo the teammate explains and the
    teams alternate, so each team plays two turns."""
    ids = [p["player_id"] for p in players]
    if teams:
        (a1, a2), (b1, b2) = teams
        return [(a1, a2), (b1, b2), (a2, a1), (b2, b1)]
    return [(ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))]


def new_match(
    challenge: StoredChallenge,
    creator: str,
    mode: Mode,
    teams: list[list[str]] | None,
    profiles: dict[str, Profile | None],
) -> dict:
    players = build_players(challenge, creator, mode, teams, profiles)
    match_id = uuid.uuid4().hex
    content = challenge.content
    doc: dict[str, Any] = {
        "id": match_id,
        "challenge_id": challenge.id,
        "game": challenge.game,
        "mode": mode,
        "creator_id": creator,
        "topic": challenge.topic,
        "players": players,
        "teams": teams,
        "play_at_ms": None,
        "created_at_ms": clock(),
    }
    if challenge.game == "mcq":
        doc |= {
            "questions": content["questions"],
            "seconds": content["seconds_per_question"],
            "answers": {},
        }
    elif challenge.game == "heads_up":
        terms = list(content["terms"])
        random.Random(int(match_id[:8], 16)).shuffle(terms)
        doc |= {
            "terms": terms,
            "seconds": content["seconds_per_round"],
            "turns": [
                {
                    "index": k,
                    "guesser": g,
                    "explainer": e,
                    "seconds": seconds_for(
                        {"players": players, "mode": mode}, g, content["seconds_per_round"]
                    ),
                }
                for k, (g, e) in enumerate(turn_plan(players, teams))
            ],
            "pointer": 0,
            "log": [],
        }
    else:
        doc |= {
            "title": content["title"],
            "mermaid": content["mermaid_blank"],
            "slots": content["blank_ids"],
            "options": content["options"],
            "answers": content["answers"],
            "seconds": content["seconds"],
            "penalty_seconds": content["wrong_finish_penalty_seconds"],
            "progress": {
                p["player_id"]: {"placements": {}, "wrong_finishes": 0, "finished_ms": None}
                for p in players
            },
        }
    return doc


def expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=MATCH_HOURS)).isoformat(
        timespec="microseconds"
    )


def start(doc: dict, player_id: str, now: int) -> dict:
    """The creator starts the shared countdown. Starting twice changes nothing."""
    if player_id != doc["creator_id"]:
        raise MatchError(403, "Only the player who created the match can start it.")
    if doc["play_at_ms"] is None:
        doc["play_at_ms"] = now + COUNTDOWN_MS
    return {"play_at_ms": doc["play_at_ms"]}


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
def ranked(rows: list[dict], key) -> list[dict]:
    """Best first. Equal results share a rank (1, 1, 3)."""
    rows = sorted(rows, key=key)
    last, rank = None, 0
    for position, row in enumerate(rows, 1):
        if key(row) != last:
            rank, last = position, key(row)
        row["rank"] = rank
    return rows


def unit_of(doc: dict, player_id: str) -> str:
    """Who a player's points count for: their team in duo, themselves otherwise."""
    team = next(p["team"] for p in doc["players"] if p["player_id"] == player_id)
    return f"team{team}" if team is not None else player_id


def units(doc: dict) -> list[dict]:
    """The scoring units with their members: two teams in duo, each player otherwise."""
    found: dict[str, dict] = {}
    for p in doc["players"]:
        unit = found.setdefault(
            unit_of(doc, p["player_id"]), {"id": unit_of(doc, p["player_id"]), "members": []}
        )
        unit["members"].append(p)
    for unit in found.values():
        unit["name"] = " & ".join(m["name"] for m in unit["members"])
    return list(found.values())


# --------------------------------------------------------------------------
# Game 1: timed multiple choice
# --------------------------------------------------------------------------
def mcq_start(doc: dict, index: int) -> int:
    return doc["play_at_ms"] + index * (doc["seconds"] * 1000 + MCQ_GAP_MS)


def mcq_end(doc: dict, index: int) -> int:
    return mcq_start(doc, index) + doc["seconds"] * 1000


def mcq_phase(doc: dict, now: int) -> tuple[str, int | None]:
    """waiting, countdown, question (index), reveal (index: the question just closed) or finished."""
    if doc["play_at_ms"] is None:
        return "waiting", None
    if now < doc["play_at_ms"]:
        return "countdown", None
    window = doc["seconds"] * 1000
    count = len(doc["questions"])
    index, within = divmod(now - doc["play_at_ms"], window + MCQ_GAP_MS)
    if index >= count or (index == count - 1 and within >= window):
        return "finished", None
    return ("question" if within < window else "reveal"), int(index)


def mcq_answer(doc: dict, player_id: str, index: int, choice: int, now: int) -> dict:
    """Record a player's first answer to the open question. It is never graded here: correctness is shown to
    everyone once the question closes, so a fast answer tells the others nothing."""
    if doc["play_at_ms"] is None or now < doc["play_at_ms"]:
        raise MatchError(409, "The game has not started yet.")
    if not 0 <= index < len(doc["questions"]):
        raise MatchError(422, "There is no such question.")
    start = mcq_start(doc, index)
    yours = seconds_for(doc, player_id, doc["seconds"]) * 1000
    if now < start:
        raise MatchError(409, "That question is not open yet.")
    if now > start + yours + GRACE_MS:
        raise MatchError(409, "Time is up for that question.")
    if choice >= len(doc["questions"][index]["options"]):
        raise MatchError(422, "There is no such option.")
    mine = doc["answers"].setdefault(player_id, {})
    saved = mine.get(str(index))
    if saved is not None:
        if saved["choice"] != choice:
            raise MatchError(409, "You already answered that question.")
    else:
        mine[str(index)] = saved = {"choice": choice, "elapsed_ms": min(now - start, yours)}
    return {"question_index": index, "answered": True, "elapsed_ms": saved["elapsed_ms"]}


def mcq_revealed(doc: dict, now: int) -> list[int]:
    """The questions whose time is over for everyone, so their answers may be shown."""
    if doc["play_at_ms"] is None:
        return []
    return [i for i in range(len(doc["questions"])) if now > mcq_end(doc, i) + GRACE_MS]


def mcq_tally(doc: dict, indices: list[int]) -> list[dict]:
    """Correct answers and the time spent on them, per scoring unit, over the given questions."""
    rows = []
    for unit in units(doc):
        correct, time_ms = 0, 0
        for member in unit["members"]:
            for i in indices:
                saved = doc["answers"].get(member["player_id"], {}).get(str(i))
                if saved and saved["choice"] == doc["questions"][i]["answer_index"]:
                    correct += 1
                    time_ms += saved["elapsed_ms"]
        rows.append(
            {
                "id": unit["id"],
                "name": unit["name"],
                "members": [m["name"] for m in unit["members"]],
                "correct": correct,
                "time_ms": time_ms,
            }
        )
    return rows


def mcq_view(doc: dict, player_id: str, now: int) -> dict:
    phase, index = mcq_phase(doc, now)
    window = doc["seconds"] * 1000
    yours = seconds_for(doc, player_id, doc["seconds"])
    view: dict[str, Any] = {
        "phase": phase,
        "total_questions": len(doc["questions"]),
        "seconds_per_question": doc["seconds"],
        "your_seconds": yours,
        "question": None,
        "reveals": [],
        "scores": [
            {"name": r["name"], "correct": r["correct"]}
            for r in mcq_tally(doc, mcq_revealed(doc, now))
        ],
    }
    if phase == "question":
        q, start = doc["questions"][index], mcq_start(doc, index)
        view["question"] = {
            "index": index,
            "question": q["question"],
            "options": q["options"],
            "opens_at_ms": start,
            "closes_at_ms": start + window,
            "your_closes_at_ms": start + yours * 1000,
            "answered": str(index) in doc["answers"].get(player_id, {}),
        }
    elif phase == "reveal":
        view["next_question_opens_at_ms"] = mcq_start(doc, index + 1)
    for i in mcq_revealed(doc, now):
        q = doc["questions"][i]
        view["reveals"].append(
            {
                "index": i,
                "question": q["question"],
                "options": q["options"],
                "answer_index": q["answer_index"],
                "explanation": q["explanation"],
                "answers": [
                    {
                        "name": p["name"],
                        "choice": doc["answers"]
                        .get(p["player_id"], {})
                        .get(str(i), {})
                        .get("choice"),
                        "correct": doc["answers"]
                        .get(p["player_id"], {})
                        .get(str(i), {})
                        .get("choice")
                        == q["answer_index"],
                        "elapsed_ms": doc["answers"]
                        .get(p["player_id"], {})
                        .get(str(i), {})
                        .get("elapsed_ms"),
                    }
                    for p in doc["players"]
                ],
            }
        )
    return view


def mcq_results(doc: dict, now: int) -> list[dict]:
    last = len(doc["questions"]) - 1
    if doc["play_at_ms"] is None or now <= mcq_end(doc, last) + GRACE_MS:
        raise MatchError(409, "The results are not ready yet.")
    rows = ranked(
        mcq_tally(doc, list(range(last + 1))), key=lambda r: (-r["correct"], r["time_ms"])
    )
    for row in rows:
        row["total"] = (last + 1) * len(row["members"])  # a team answers every question twice over
        row["winner"] = row["rank"] == 1 and row["correct"] > 0
    return rows


# --------------------------------------------------------------------------
# Game 2: heads-up
# --------------------------------------------------------------------------
def hu_turn_start(doc: dict, index: int) -> int:
    return doc["play_at_ms"] + index * (doc["seconds"] * 1000 + HEADS_UP_HANDOVER_MS)


def hu_turn_end(doc: dict, index: int) -> int:
    return hu_turn_start(doc, index) + doc["turns"][index]["seconds"] * 1000


def hu_phase(doc: dict, now: int) -> tuple[str, int | None]:
    """waiting, countdown, turn (index), handover (index: the turn just over) or finished."""
    if doc["play_at_ms"] is None:
        return "waiting", None
    if now < doc["play_at_ms"]:
        return "countdown", None
    cycle = doc["seconds"] * 1000 + HEADS_UP_HANDOVER_MS
    index, within = divmod(now - doc["play_at_ms"], cycle)
    count = len(doc["turns"])
    if index >= count:
        return "finished", None
    if within < doc["turns"][int(index)]["seconds"] * 1000:
        return "turn", int(index)
    return ("finished", None) if index == count - 1 else ("handover", int(index))


def hu_action(doc: dict, player_id: str, action: str, card_index: int, now: int) -> dict:
    """The guesser or the explainer of the current turn marks the card: correct (+1), skip (0) or violation (-1,
    the explainer said a forbidden word). A card is dealt once: repeating a press for a card already marked is a
    harmless replay, and a press for a card not yet dealt is refused."""
    if doc["play_at_ms"] is None or now < doc["play_at_ms"]:
        raise MatchError(409, "The game has not started yet.")
    turn = next(
        (
            t
            for t in doc["turns"]
            if hu_turn_start(doc, t["index"]) <= now <= hu_turn_end(doc, t["index"]) + GRACE_MS
        ),
        None,
    )
    if turn is None:
        raise MatchError(409, "No turn is being played right now.")
    if player_id not in (turn["guesser"], turn["explainer"]):
        raise MatchError(403, "Only the two players in this turn can mark cards.")
    pointer = doc["pointer"]
    if card_index < pointer:
        return {"replayed": True, "next_card_index": pointer}
    if card_index > pointer:
        raise MatchError(409, "That card has not been dealt yet.")
    if pointer >= len(doc["terms"]):
        raise MatchError(409, "There are no cards left.")
    doc["log"].append({"turn": turn["index"], "card": pointer, "action": action, "at_ms": now})
    doc["pointer"] = pointer + 1
    return {"replayed": False, "next_card_index": doc["pointer"]}


def hu_scores(doc: dict, through_turn: int | None = None) -> list[dict]:
    """Each turn's points (correct minus violations, never below zero) count for the guesser's unit."""
    rows = []
    for unit in units(doc):
        member_ids = {m["player_id"] for m in unit["members"]}
        points, per_turn = 0, []
        for turn in doc["turns"]:
            if turn["guesser"] not in member_ids or (
                through_turn is not None and turn["index"] > through_turn
            ):
                continue
            events = [e for e in doc["log"] if e["turn"] == turn["index"]]
            got = max(0, sum({"correct": 1, "violation": -1}.get(e["action"], 0) for e in events))
            points += got
            per_turn.append(got)
        rows.append(
            {
                "id": unit["id"],
                "name": unit["name"],
                "members": [m["name"] for m in unit["members"]],
                "score": points,
                "turns": per_turn,
            }
        )
    return rows


def hu_view(doc: dict, player_id: str, now: int) -> dict:
    phase, k = hu_phase(doc, now)
    names = {p["player_id"]: p["name"] for p in doc["players"]}
    view: dict[str, Any] = {
        "phase": phase,
        "seconds_per_turn": doc["seconds"],
        "cards_left": max(0, len(doc["terms"]) - doc["pointer"]),
        "turns": [
            {
                "index": t["index"],
                "guesser": names[t["guesser"]],
                "explainer": names[t["explainer"]],
                "seconds": t["seconds"],
                "starts_at_ms": hu_turn_start(doc, t["index"]) if doc["play_at_ms"] else None,
            }
            for t in doc["turns"]
        ],
        "scores": [
            {"name": r["name"], "score": r["score"]}
            for r in hu_scores(doc, k if phase == "handover" else None)
        ],
        "turn": None,
    }
    if phase == "turn":
        t = doc["turns"][k]
        role = (
            "guesser"
            if player_id == t["guesser"]
            else "explainer"
            if player_id == t["explainer"]
            else "watching"
        )
        view["turn"] = {
            "index": k,
            "guesser": names[t["guesser"]],
            "explainer": names[t["explainer"]],
            "you_are": role,
            "ends_at_ms": hu_turn_end(doc, k),
        }
        if role != "watching" and doc["pointer"] < len(doc["terms"]):
            card = doc["terms"][doc["pointer"]]
            view["turn"]["card"] = {
                "index": doc["pointer"],
                "term": card["term"],
                "forbidden": card["forbidden"],
            }
    if phase == "handover":
        view["next_turn_starts_at_ms"] = hu_turn_start(doc, k + 1)
    return view


def hu_results(doc: dict, now: int) -> list[dict]:
    last = len(doc["turns"]) - 1
    if doc["play_at_ms"] is None or now <= hu_turn_end(doc, last) + GRACE_MS:
        raise MatchError(409, "The results are not ready yet.")
    rows = ranked(hu_scores(doc), key=lambda r: -r["score"])
    for row in rows:
        row["winner"] = row["rank"] == 1 and row["score"] > 0
    return rows


# --------------------------------------------------------------------------
# Game 3: fill in the diagram
# --------------------------------------------------------------------------
def dg_deadline(doc: dict, player_id: str) -> int:
    return doc["play_at_ms"] + seconds_for(doc, player_id, doc["seconds"]) * 1000


def dg_open(doc: dict, player_id: str, now: int) -> dict:
    """The player's progress, if they may still place cards: the game has started, their time is not up and
    they have not already finished perfectly."""
    if doc["play_at_ms"] is None or now < doc["play_at_ms"]:
        raise MatchError(409, "The game has not started yet.")
    if now > dg_deadline(doc, player_id) + GRACE_MS:
        raise MatchError(409, "Your time is up.")
    mine = doc["progress"][player_id]
    if mine["finished_ms"] is not None:
        raise MatchError(409, "You already finished.")
    return mine


def dg_place(doc: dict, player_id: str, placements: dict[str, int], now: int) -> dict:
    """Replace the player's whole arrangement: slot id -> index into the word bank. Partial is fine; a word can
    only be in one place."""
    mine = dg_open(doc, player_id, now)
    if any(slot not in doc["slots"] for slot in placements):
        raise MatchError(422, "Unknown slot.")
    if any(not 0 <= word < len(doc["options"]) for word in placements.values()):
        raise MatchError(422, "Unknown word.")
    if len(set(placements.values())) != len(placements):
        raise MatchError(422, "A word can only be placed once.")
    mine["placements"] = dict(placements)
    return {"placed": len(placements), "slots": len(doc["slots"])}


def dg_finish(doc: dict, player_id: str, now: int) -> dict:
    """Check the arrangement. A wrong finish tells which slots are wrong (not the right words), adds to the
    player's penalty and leaves the clock running; a perfect one records the time."""
    mine = dg_open(doc, player_id, now)
    if len(mine["placements"]) != len(doc["slots"]):
        raise MatchError(409, "Fill every blank before you finish.")
    wrong = [s for s in doc["slots"] if doc["options"][mine["placements"][s]] != doc["answers"][s]]
    if wrong:
        mine["wrong_finishes"] += 1
        return {
            "perfect": False,
            "correct": len(doc["slots"]) - len(wrong),
            "wrong_slots": wrong,
            "wrong_finishes": mine["wrong_finishes"],
            "penalty_seconds": doc["penalty_seconds"],
        }
    mine["finished_ms"] = now - doc["play_at_ms"]
    total = mine["finished_ms"] + mine["wrong_finishes"] * doc["penalty_seconds"] * 1000
    return {
        "perfect": True,
        "elapsed_ms": mine["finished_ms"],
        "wrong_finishes": mine["wrong_finishes"],
        "total_ms": total,
    }


def dg_over(doc: dict, now: int) -> bool:
    """Everyone has finished, or everyone's time has run out."""
    if doc["play_at_ms"] is None:
        return False
    return all(
        doc["progress"][p["player_id"]]["finished_ms"] is not None
        or now > dg_deadline(doc, p["player_id"]) + GRACE_MS
        for p in doc["players"]
    )


def dg_view(doc: dict, player_id: str, now: int) -> dict:
    started = doc["play_at_ms"] is not None
    phase = "waiting" if not started else "countdown" if now < doc["play_at_ms"] else "playing"
    if dg_over(doc, now):
        phase = "finished"
    mine = doc["progress"][player_id]
    return {
        "phase": phase,
        "board": {
            "title": doc["title"],
            "mermaid": doc["mermaid"],
            "slots": doc["slots"],
            "options": doc["options"],
        },
        "your_seconds": seconds_for(doc, player_id, doc["seconds"]),
        "your_deadline_ms": dg_deadline(doc, player_id) if started else None,
        "penalty_seconds": doc["penalty_seconds"],
        "you": {
            "placements": mine["placements"],
            "wrong_finishes": mine["wrong_finishes"],
            "finished": mine["finished_ms"] is not None,
        },
        "players": [
            {
                "name": p["name"],
                "finished": doc["progress"][p["player_id"]]["finished_ms"] is not None,
            }
            for p in doc["players"]
        ],
    }


def dg_results(doc: dict, now: int) -> list[dict]:
    """Only perfect reconstructions are ranked, by time plus the penalty for each wrong finish."""
    if not dg_over(doc, now):
        raise MatchError(409, "The results are not ready yet.")
    perfect, others = [], []
    for p in doc["players"]:
        mine = doc["progress"][p["player_id"]]
        row = {
            "id": p["player_id"],
            "name": p["name"],
            "members": [p["name"]],
            "wrong_finishes": mine["wrong_finishes"],
        }
        if mine["finished_ms"] is None:
            others.append(
                row | {"rank": None, "winner": False, "elapsed_ms": None, "total_ms": None}
            )
        else:
            row |= {
                "elapsed_ms": mine["finished_ms"],
                "total_ms": mine["finished_ms"]
                + mine["wrong_finishes"] * doc["penalty_seconds"] * 1000,
            }
            perfect.append(row)
    rows = ranked(perfect, key=lambda r: r["total_ms"])
    for row in rows:
        row["winner"] = row["rank"] == 1
    return rows + others


# --------------------------------------------------------------------------
# What a player is shown
# --------------------------------------------------------------------------
VIEWS = {"mcq": mcq_view, "heads_up": hu_view, "diagram": dg_view}
RESULTS = {"mcq": mcq_results, "heads_up": hu_results, "diagram": dg_results}


def state(doc: dict, player_id: str, now: int) -> dict:
    view = VIEWS[doc["game"]](doc, player_id, now)
    return {
        "id": doc["id"],
        "challenge_id": doc["challenge_id"],
        "can_start": doc["creator_id"] == player_id and doc["play_at_ms"] is None,
        "game": doc["game"],
        "mode": doc["mode"],
        "topic": doc["topic"],
        "phase": view["phase"],
        "server_time_ms": now,
        "play_at_ms": doc["play_at_ms"],
        "you": player_id,
        "players": [
            {"player_id": p["player_id"], "name": p["name"], "role": p["role"], "team": p["team"]}
            for p in doc["players"]
        ],
        "view": view,
    }


def results(doc: dict, now: int) -> dict:
    return {
        "id": doc["id"],
        "game": doc["game"],
        "mode": doc["mode"],
        "topic": doc["topic"],
        "ranking": RESULTS[doc["game"]](doc, now),
    }
