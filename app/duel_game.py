"""Two-player matches over approved saved questions. Scoring and transitions are deterministic."""

import hashlib
import json
import time
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException

from app.duel_store import DuelStore
from learning_service.store import StoredQuiz, grade
from learning_service.visual_recall import IllustratedQuestion

TERMINAL = {"finished", "expired", "cancelled"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_hash(source):
    return digest({k: source[k] for k in ("question_set_id", "lesson_id", "title", "questions")})


def source_reason(tx, match):
    source = tx.source(match["owner_id"], match["source_kind"], match["source_id"])
    lesson = tx.source(match["owner_id"], "lessons", match["lesson_id"])
    if source is None or lesson is None:
        return "source_unavailable"
    if source.get("approved") is not True or lesson.get("status") != "approved":
        return "source_unapproved"
    if (
        source_hash(source) != match["source_hash"]
        or lesson.get("revision", 1) != match["lesson_revision"]
    ):
        return "source_changed"
    return None


def phase(match, now):
    if match["status"] in TERMINAL:
        return match["status"]
    if now >= match["expires_at"]:
        return "expired"
    if match["status"] == "waiting":
        return "waiting"
    return "countdown" if now < match["started_at"] else "running"


def invalidate(match, now, reason):
    if match["status"] in TERMINAL:
        return
    expired = now >= match["expires_at"]
    match.update(
        status="expired" if expired else "cancelled",
        reason="deadline_elapsed" if expired else reason,
        version=match["version"] + 1,
    )


class DuelGame:
    def __init__(self, repository, clock=None, match_limit=200):
        self.store, self.clock, self.match_limit = DuelStore(repository), clock, match_limit
        if not repository.durable and clock is not None:
            repository.duel_clock_ms = clock

    def now(self, tx):
        if self.clock:
            return int(self.clock())
        if tx.connection is not None:
            return tx.connection.execute(
                "select floor(extract(epoch from clock_timestamp())*1000)::bigint"
            ).fetchone()[0]
        return time.time_ns() // 1_000_000

    def source(self, tx, owner, kind, key, lock=False):
        source = tx.source(owner, kind, key, lock)
        if source is None:
            raise HTTPException(404, "Saved question source not found.")
        lesson = tx.source(owner, "lessons", source["lesson_id"], lock)
        if lesson is None:
            raise HTTPException(404, "Source lesson not found.")
        try:
            quiz = StoredQuiz.model_validate(source)
            if not 1 <= len(quiz.questions) <= 50 or len(json.dumps(source["questions"])) > 512_000:
                raise ValueError()
            for q in quiz.questions:
                if (
                    q.type not in ("multiple_choice", "true_false")
                    or len(q.options) not in (2, 4)
                    or len(set(q.options)) != len(q.options)
                    or q.options.count(q.answer) != 1
                ):
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(
                422, "Saved questions are not a supported reviewed choice-based snapshot."
            ) from None
        return source, lesson, quiz

    def source_info(self, actor, kind, key):
        with self.store.transaction(actor) as tx:
            source, lesson, quiz = self.source(tx, actor, kind, key)
            return {
                "id": key,
                "source_kind": "quiz" if kind == "quizzes" else "tournament",
                "question_set_id": source["question_set_id"],
                "title": quiz.title,
                "total": len(quiz.questions),
                "approved": source.get("approved") is True,
                "lesson_approved": lesson.get("status") == "approved",
            }

    def room(self, tx, room_id, actor, now, lock=False):
        room = tx.room(room_id, lock)
        if (
            not room
            or datetime.fromisoformat(room["expires_at"]).timestamp() * 1000 <= now
            or not any(m["user_id"] == actor for m in room["members"])
        ):
            raise HTTPException(404, "Room not found.")
        return room

    def load(self, tx, match_id, actor, *, write=False, management=False):
        match = tx.match(match_id)
        if match is None:
            raise HTTPException(404, "Duel not found.")
        now = self.now(tx)
        room = None
        if match["mode"] == "local":
            if match["owner_id"] != actor:
                raise HTTPException(404, "Duel not found.")
        else:
            room = self.room(tx, match["room_id"], actor, now, lock=write)
        if write:
            match = tx.match(match_id, lock=True)
            if match is None:
                raise HTTPException(404, "Duel not found.")
            now = self.now(tx)
        players = tx.players(match_id)
        if (
            room
            and not any(p["user_id"] == actor for p in players)
            and not (management and actor == room["host_id"])
        ):
            raise HTTPException(404, "This player did not join this duel's roster.")
        return match, room, players, now

    def available(self, tx, match):
        reason = source_reason(tx, match)
        return {"available": reason is None, "reason": reason}

    def require_source(self, tx, match):
        if source_reason(tx, match):
            raise HTTPException(409, "Duel source is withdrawn or changed.")

    def summary(self, tx, match, room, players, actor, now):
        current = {m["user_id"] for m in room["members"]} if room else None
        return {
            k: match[k]
            for k in (
                "id",
                "mode",
                "room_id",
                "title",
                "version",
                "total",
                "created_at",
                "started_at",
                "expires_at",
                "reason",
            )
        } | {
            "status": phase(match, now),
            "server_now": now,
            "my_player_id": next((p["id"] for p in players if p["user_id"] == actor), None),
            "players": [
                {
                    "id": p["id"],
                    "display_name": p["display_name"]
                    if current is None or p["user_id"] in current
                    else "Player left",
                    "ready": p["ready"],
                    "submitted": p["submitted_at"] is not None,
                    "forfeited": p["forfeited"],
                    "version": p["version"],
                }
                for p in players
            ],
            "source": self.available(tx, match),
        }

    def create(self, actor, body, key, operation="create"):
        kind = {"quiz": "quizzes", "tournament": "tournaments"}[body["source_kind"]]
        with self.store.transaction(actor, write=True, room_id=body.get("room_id")) as tx:
            source, lesson, quiz = self.source(tx, actor, kind, body["source_id"], lock=True)
            if source.get("approved") is not True or lesson.get("status") != "approved":
                raise HTTPException(
                    409, "Approve the source lesson and exact question snapshot first."
                )
            if source["question_set_id"] != body["expected_question_set_id"]:
                raise HTTPException(409, "The selected question snapshot changed.")
            indices = body.get("question_indices") or list(range(len(quiz.questions)))
            if len(set(indices)) != len(indices) or any(i >= len(quiz.questions) for i in indices):
                raise HTTPException(
                    422, "Question indices must be unique positions in the saved snapshot."
                )
            now, room = self.now(tx), None
            if body["mode"] == "room":
                room = self.room(tx, body["room_id"], actor, now, lock=True)
                if room["host_id"] != actor:
                    raise HTTPException(
                        403, "Only the source-owning room host can create this duel."
                    )
            saved = tx.receipt(actor, operation, digest(key))
            if saved:
                if saved["fingerprint"] != digest(body):
                    raise HTTPException(409, "Conflicting duel creation retry.")
                match = tx.match(saved["match_id"])
                if match is None:
                    raise HTTPException(404, "Duel no longer exists.")
                return self.summary(tx, match, room, tx.players(match["id"]), actor, now)
            if tx.count(actor) >= self.match_limit:
                raise HTTPException(429, "Saved duel limit reached for this player.")
            if room:
                if (
                    room["closed"]
                    or room["version"] != body["expected_room_version"]
                    or len(room["members"]) != 2
                ):
                    raise HTTPException(
                        409, "A duel needs a current open room with exactly two members."
                    )
                if tx.active(room["id"], now):
                    raise HTTPException(409, "Finish or cancel the room's current duel first.")
                from app.diagram_store import DiagramTransaction

                diagram = DiagramTransaction(
                    tx.repository,
                    connection=tx.connection,
                    state=getattr(tx.repository, "diagram_state", None),
                )
                if tx.connection is not None:
                    active_diagram = tx.connection.execute(
                        "select exists(select 1 from api_private.diagram_races where room_id=%s and data->>'status' in ('waiting','running') and (data->>'status'='waiting' or (data->>'deadline_at')::bigint>%s))",
                        (room["id"], now),
                    ).fetchone()[0]
                else:
                    active_diagram = diagram.state and any(
                        r["room_id"] == room["id"]
                        and r["status"] in ("waiting", "running")
                        and (r["status"] == "waiting" or r["deadline_at"] > now)
                        for r in diagram.state["races"].values()
                    )
                if active_diagram:
                    raise HTTPException(409, "Finish or cancel the room's diagram race first.")
            match_id = uuid4().hex
            selected = [
                quiz.questions[i].model_copy(update={"index": j}) for j, i in enumerate(indices)
            ]
            frozen = quiz.model_copy(update={"id": match_id, "questions": selected})
            match = {
                "id": match_id,
                "owner_id": actor,
                "mode": body["mode"],
                "room_id": room["id"] if room else None,
                "source_kind": kind,
                "source_id": body["source_id"],
                "question_set_id": source["question_set_id"],
                "source_hash": source_hash(source),
                "lesson_id": source["lesson_id"],
                "lesson_revision": lesson.get("revision", 1),
                "source_indices": indices,
                "title": quiz.title,
                "questions": [q.model_dump(mode="json") for q in selected],
                "public_questions": [q.model_dump(mode="json") for q in frozen.public().questions],
                "status": "waiting",
                "reason": None,
                "version": 1,
                "total": len(selected),
                "created_at": now,
                "started_at": None,
                "expires_at": min(
                    now + 86_400_000,
                    int(datetime.fromisoformat(room["expires_at"]).timestamp() * 1000),
                )
                if room
                else now + 86_400_000,
                "actions": {},
                "action_actors": [],
            }
            match["solution"] = [
                {
                    "index": i,
                    **{
                        k: v
                        for k, v in grade(q, q.options.index(q.answer))
                        .model_dump(mode="json")
                        .items()
                        if k != "correct"
                    },
                }
                for i, q in enumerate(selected)
            ]
            if len(json.dumps(match).encode()) > 2_097_152:
                raise HTTPException(422, "The frozen duel exceeds the match snapshot size limit.")
            tx.put_match(match)
            roster = (
                [(p["display_name"], p["user_id"]) for p in room["members"]]
                if room
                else [(name, None) for name in body["players"]]
            )
            players = []
            for name, user_id in roster:
                player = {
                    "id": uuid4().hex,
                    "user_id": user_id,
                    "display_name": name.strip(),
                    "ready": False,
                    "version": 1,
                    "answers": None,
                    "score": None,
                    "results": None,
                    "submitted_at": None,
                    "forfeited": False,
                    "receipts": {},
                }
                tx.put_player(match_id, player, insert=True)
                players.append(player)
            tx.save_receipt(
                actor,
                operation,
                digest(key),
                match_id,
                {"match_id": match_id, "fingerprint": digest(body)},
            )
            return self.summary(tx, match, room, players, actor, now)

    def snapshot(self, match_id, actor):
        with self.store.transaction(actor) as tx:
            match, room, players, now = self.load(tx, match_id, actor)
            return self.summary(tx, match, room, players, actor, now)

    def current(self, room_id, actor, management=False):
        with self.store.transaction(actor) as tx:
            room = self.room(tx, room_id, actor, self.now(tx))
            if management and room["host_id"] != actor:
                raise HTTPException(403, "Only the current host may manage this duel.")
            match = tx.latest(room_id)
            if match is None:
                raise HTTPException(404, "No duel exists in this room.")
            match, room, players, now = self.load(tx, match["id"], actor, management=management)
            if management:
                return {
                    "room_id": room_id,
                    "match_id": match["id"],
                    "status": phase(match, now),
                    "version": match["version"],
                    "reason": match["reason"],
                }
            return self.summary(tx, match, room, players, actor, now)

    def history(self, actor, after, limit):
        with self.store.transaction(actor) as tx:
            now = self.now(tx)
            rows = tx.history(actor, after, limit + 1, now)
            return {
                "items": [
                    self.summary(
                        tx,
                        m,
                        tx.room(m["room_id"]) if m["room_id"] else None,
                        tx.players(m["id"]),
                        actor,
                        now,
                    )
                    for m in rows[:limit]
                ],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def own_player(self, match, players, actor, player_id):
        player = next((p for p in players if p["id"] == player_id), None)
        if player is None or (match["mode"] == "room" and player["user_id"] != actor):
            raise HTTPException(404, "This duel attempt is unavailable to this player.")
        return player

    def player_state(self, match_id, player_id, actor):
        with self.store.transaction(actor) as tx:
            match, _, players, now = self.load(tx, match_id, actor)
            player = self.own_player(match, players, actor, player_id)
            return {
                "id": match_id,
                "player_id": player_id,
                "version": player["version"],
                "answers": player["answers"],
                "ready": player["ready"],
                "submitted": player["submitted_at"] is not None,
                "forfeited": player["forfeited"],
                "status": phase(match, now),
            }

    def questions(self, match_id, actor):
        with self.store.transaction(actor) as tx:
            match, _, _, now = self.load(tx, match_id, actor)
            if phase(match, now) not in ("countdown", "running"):
                raise HTTPException(
                    409, "Questions are available only after start and before the duel ends."
                )
            self.require_source(tx, match)
            return {
                "id": match_id,
                "title": match["title"],
                "questions": match["public_questions"],
                "started_at": match["started_at"],
                "expires_at": match["expires_at"],
                "server_now": now,
            }

    def receipt(self, receipts, actor, operation, key, body):
        address = digest([actor, operation, key])
        saved = receipts.get(address)
        if saved and saved["fingerprint"] != digest(body):
            raise HTTPException(409, "Conflicting duel action retry.")
        if not saved and len(receipts) >= 1000:
            raise HTTPException(429, "Duel action limit reached.")
        return address, saved

    def start_or_cancel(self, match_id, actor, body, key, operation):
        with self.store.transaction(actor, write=True) as tx:
            match, room, players, now = self.load(
                tx, match_id, actor, write=True, management=operation == "cancel"
            )
            if room and (
                room["host_id"] != actor and (operation != "cancel" or match["owner_id"] != actor)
            ):
                raise HTTPException(
                    403, "Only the room host or permitted creating owner can manage this duel."
                )
            address, saved = self.receipt(match["actions"], actor, operation, key, body)
            if saved:
                return saved["response"]
            status = phase(match, now)
            if operation == "cancel" and status in ("finished", "expired"):
                raise HTTPException(409, "Finished or expired results cannot be cancelled.")
            if match["version"] != body["expected_version"]:
                raise HTTPException(409, "Duel changed; reload its snapshot.")
            if operation == "start":
                if status != "waiting" or len(players) != 2:
                    raise HTTPException(409, "Duel is not waiting with two participants.")
                self.require_source(tx, match)
                if room:
                    members = {m["user_id"] for m in room["members"]}
                    if room["closed"] or any(
                        not p["ready"] or p["user_id"] not in members for p in players
                    ):
                        raise HTTPException(
                            409, "Both captured players must be present and freshly ready."
                        )
                started_at = now + (5000 if room else 0)
                if started_at >= match["expires_at"]:
                    raise HTTPException(
                        409, "Not enough room lifetime remains for the shared start."
                    )
                match.update(
                    status="running",
                    started_at=started_at,
                    version=match["version"] + 1,
                )
            elif match["status"] != "cancelled":
                match.update(
                    status="cancelled", reason="host_cancelled", version=match["version"] + 1
                )
            out = {
                "id": match_id,
                "status": phase(match, now),
                "version": match["version"],
                "reason": match["reason"],
                "started_at": match["started_at"],
                "expires_at": match["expires_at"],
            }
            match["actions"][address] = {
                "actor": actor,
                "fingerprint": digest(body),
                "response": out,
            }
            if actor not in match["action_actors"]:
                match["action_actors"].append(actor)
            tx.put_match(match)
            return out

    def player_action(self, match_id, player_id, actor, body, key, operation):
        with self.store.transaction(actor, write=True) as tx:
            match, _, players, now = self.load(tx, match_id, actor, write=True)
            me = self.own_player(match, players, actor, player_id)
            address, saved = self.receipt(me["receipts"], actor, operation, key, body)
            if saved:
                return saved["response"]
            self.require_source(tx, match)
            status = phase(match, now)
            if me["version"] != body["expected_version"]:
                raise HTTPException(409, "Player attempt changed; reload it.")
            if operation == "ready":
                if status != "waiting":
                    raise HTTPException(409, "Readiness changes are allowed only while waiting.")
                me["ready"] = body["ready"]
            else:
                if status != "running" or me["submitted_at"] is not None or me["forfeited"]:
                    raise HTTPException(409, "This player attempt is not accepting answers.")
                if operation == "submit":
                    answers = body["answers"]
                    if len(answers) != match["total"]:
                        raise HTTPException(422, "Submit one choice for every question.")
                    results = [
                        grade(IllustratedQuestion.model_validate(q), choice).model_dump(mode="json")
                        | {"index": i, "choice": choice}
                        for i, (q, choice) in enumerate(zip(match["questions"], answers))
                    ]
                    me.update(
                        answers=answers,
                        score=sum(r["correct"] for r in results),
                        results=results,
                        submitted_at=now,
                    )
                else:
                    me["forfeited"] = True
            me["version"] += 1
            match["version"] += 1
            if operation != "ready" and all(
                p["submitted_at"] is not None or p["forfeited"] for p in players
            ):
                match["status"] = "finished"
            out = {
                "id": match_id,
                "player_id": player_id,
                "version": me["version"],
                "match_version": match["version"],
                "ready": me["ready"],
                "submitted": me["submitted_at"] is not None,
                "forfeited": me["forfeited"],
                "status": phase(match, now),
            }
            me["receipts"][address] = {"actor": actor, "fingerprint": digest(body), "response": out}
            if not tx.put_player(match_id, me):
                raise HTTPException(404, "Player participation was deleted.")
            tx.put_match(match)
            return out

    def results(self, match_id, actor, review=False):
        with self.store.transaction(actor) as tx:
            match, room, players, now = self.load(tx, match_id, actor)
            status = phase(match, now)
            if status not in ("finished", "expired"):
                raise HTTPException(409, "Results unlock after both attempts end or the deadline.")
            if review:
                self.require_source(tx, match)
            scores = [p["score"] for p in players if p["submitted_at"] is not None]
            current = {p["user_id"] for p in room["members"]} if room else None
            entries = [
                {
                    "id": p["id"],
                    "display_name": p["display_name"]
                    if current is None or p["user_id"] in current
                    else "Player left",
                    "submitted": p["submitted_at"] is not None,
                    "forfeited": p["forfeited"],
                    "score": p["score"],
                    "total": match["total"],
                    "rank": 1 + sum(s > p["score"] for s in scores)
                    if p["score"] is not None
                    else None,
                }
                for p in players
            ]
            out = {
                "id": match_id,
                "status": status,
                "players": entries,
                "winner_ids": [p["id"] for p in entries if p["rank"] == 1],
                "scoring": "correct_answers_only",
                "source": self.available(tx, match),
            }
            if review:
                out |= {
                    "questions": match["public_questions"],
                    "solution": match["solution"],
                    "attempts": [
                        {"player_id": p["id"], "answers": p["answers"], "results": p["results"]}
                        for p in players
                    ],
                }
            return out

    def rematch(self, match_id, actor, body, key):
        with self.store.transaction(actor) as tx:
            match, room, players, now = self.load(tx, match_id, actor)
            if match["owner_id"] != actor:
                raise HTTPException(
                    403, "Only the source-owning creator may share it into a new duel."
                )
            if phase(match, now) not in TERMINAL:
                raise HTTPException(409, "Finish or cancel the current duel before a rematch.")
            params = {
                "source_kind": "quiz" if match["source_kind"] == "quizzes" else "tournament",
                "source_id": match["source_id"],
                "expected_question_set_id": match["question_set_id"],
                "mode": match["mode"],
                "room_id": match["room_id"],
                "expected_room_version": body["expected_room_version"],
                "players": [p["display_name"] for p in players] if not room else None,
                "question_indices": match["source_indices"],
            }
            if not room and body["expected_room_version"] is not None:
                raise HTTPException(422, "Local rematches have no room version.")
            if room and body["expected_room_version"] is None:
                raise HTTPException(422, "Room rematches require the current room version.")
        return self.create(actor, params, key, operation="rematch:" + match_id)
