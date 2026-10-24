"""Simultaneous private-room diagram assembly. No AI calls or client-owned scoring."""

import hashlib
import json
import secrets
import time
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException

from app.diagram_store import DiagramStore

SNAPSHOT_VERSION = "mermaid-12.2.0-frozen-v1"
QUESTION = "How does this work?"
MAX_ACTIONS = 1000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DiagramGame:
    def __init__(self, repository, clock=None):
        self.store = DiagramStore(repository)
        self.clock = clock

    def now(self, tx):
        if self.clock:
            return int(self.clock())
        if tx.connection is not None:
            return tx.connection.execute(
                "select floor(extract(epoch from clock_timestamp())*1000)::bigint"
            ).fetchone()[0]
        return time.time_ns() // 1_000_000

    def source(self, tx, owner, lesson_id, visual_id, lock=False):
        lesson = tx.source(owner, lesson_id, lock=lock)
        if lesson is None:
            raise HTTPException(404, "Lesson not found.")
        if lesson.get("status") != "approved":
            raise HTTPException(409, "Review and approve the source lesson first.")
        visual = next((v for v in lesson.get("visuals", []) if v["id"] == visual_id), None)
        if visual is None:
            raise HTTPException(404, "Saved visual not found.")
        if visual.get("kind") != "diagram":
            raise HTTPException(422, "Select a saved Mermaid flowchart visual.")
        fingerprint = digest(
            {
                "visual": visual,
                "lesson_revision": lesson.get("revision", 1),
                "snapshot": SNAPSHOT_VERSION,
            }
        )
        return visual, fingerprint

    def template_info(self, t):
        return {k: t[k] for k in ("id", "lesson_id", "visual_id", "source_hash", "created_at")} | {
            "slots": len(t["snapshot"]["slots"]),
            "renderer": t["snapshot"]["renderer"],
            "snapshot_version": 1,
        }

    def prepare_template(self, owner, body, key):
        fingerprint = digest(body)
        with self.store.transaction() as tx:
            visual, source_hash = self.source(tx, owner, body["lesson_id"], body["visual_id"])
            saved = tx.receipt(owner, "template", digest(key))
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Conflicting template request retry.")
                t = tx.get("templates", saved["id"])
                if not t or t["source_hash"] != source_hash:
                    raise HTTPException(409, "Source changed; create a new template request.")
                return {"ready": self.template_info(t)}
            cached = tx.template_for_source(
                owner, body["lesson_id"], body["visual_id"], source_hash
            )
            return {
                "visual": visual,
                "source_hash": source_hash,
                "fingerprint": fingerprint,
                "cached": cached,
            }

    def commit_template(self, owner, body, key, prepared, snapshot):
        with self.store.transaction(write=True, serial="diagram-template:" + owner) as tx:
            _, source_hash = self.source(tx, owner, body["lesson_id"], body["visual_id"], lock=True)
            if source_hash != prepared["source_hash"]:
                raise HTTPException(409, "Source changed while rendering; no snapshot was saved.")
            saved = tx.receipt(owner, "template", digest(key))
            if saved:
                if saved["fingerprint"] != prepared["fingerprint"]:
                    raise HTTPException(409, "Conflicting template request retry.")
                return self.template_info(tx.get("templates", saved["id"]))
            template = tx.template_for_source(
                owner, body["lesson_id"], body["visual_id"], source_hash
            )
            if template is None:
                template = {
                    "id": uuid4().hex,
                    "owner_id": owner,
                    **body,
                    "source_hash": source_hash,
                    "created_at": self.now(tx),
                    "snapshot": snapshot,
                }
                tx.put("templates", template["id"], template)
            tx.save_receipt(
                owner,
                "template",
                digest(key),
                template["id"],
                {"fingerprint": prepared["fingerprint"], "id": template["id"]},
            )
            return self.template_info(template)

    def template(self, owner, template_id):
        with self.store.transaction() as tx:
            t = tx.get("templates", template_id)
            if not t or t["owner_id"] != owner:
                raise HTTPException(404, "Diagram template not found.")
            return self.template_info(t)

    def room(self, tx, room_id, actor, now, lock=False):
        room = tx.room(room_id, lock=lock)
        if not room or datetime.fromisoformat(room["expires_at"]).timestamp() * 1000 <= now:
            raise HTTPException(404, "Room not found.")
        if not any(m["user_id"] == actor for m in room["members"]):
            raise HTTPException(404, "Room not found.")
        return room

    def create_race(self, owner, body, key):
        with self.store.transaction(write=True, serial="diagram-room:" + body["room_id"]) as tx:
            now = self.now(tx)
            t = tx.get("templates", body["template_id"], light=True)
            if not t or t["owner_id"] != owner:
                raise HTTPException(404, "Diagram template not found.")
            room = self.room(tx, body["room_id"], owner, now, lock=True)
            _, current_source = self.source(tx, owner, t["lesson_id"], t["visual_id"], lock=True)
            if current_source != t["source_hash"]:
                raise HTTPException(409, "Source changed; create a new template.")
            if room["host_id"] != owner:
                raise HTTPException(403, "Only the host can share their diagram into a race.")
            saved = tx.receipt(owner, "race", digest(key))
            if saved:
                if saved["fingerprint"] != digest(body):
                    raise HTTPException(409, "Conflicting race creation retry.")
                r = tx.get("races", saved["id"])
                if not r:
                    raise HTTPException(404, "Race no longer exists.")
                return {
                    "id": r["id"],
                    "room_id": r["room_id"],
                    "version": r["version"],
                    "status": r["status"],
                }
            if room["closed"] or room["version"] != body["expected_room_version"]:
                raise HTTPException(409, "Room changed or closed; read its current snapshot.")
            for existing in tx.active_races(room["id"]):
                state = self.status(existing, tx.players(existing["id"]), now)
                if state == "waiting" and existing["owner_id"] != room["host_id"]:
                    state = "cancelled"
                if state in ("waiting", "countdown", "running"):
                    raise HTTPException(
                        409, "Finish or cancel the room's current diagram race first."
                    )
                existing["status"] = state
                existing["version"] += 1
                tx.put("races", existing["id"], existing)
            if not 2 <= len(room["members"]) <= 8:
                raise HTTPException(409, "A simultaneous race needs 2–8 room members.")
            r = {
                "id": uuid4().hex,
                "owner_id": owner,
                **body,
                "created_at": now,
                "version": 1,
                "status": "waiting",
                "started_at": None,
                "deadline_at": None,
                "actions": {},
            }
            tx.put("races", r["id"], r)
            labels = [slot["text"] for slot in t["snapshot"]["slots"]]
            secrets.SystemRandom().shuffle(labels)
            for m in room["members"]:
                cards = [{"id": uuid4().hex, "text": text} for text in labels]
                tx.put_player(
                    r["id"],
                    m["user_id"],
                    {
                        "user_id": m["user_id"],
                        "id": uuid4().hex,
                        "display_name": m["display_name"],
                        "cards": cards,
                        "placements": {},
                        "version": 1,
                        "completed_at": None,
                        "elapsed_ms": None,
                        "forfeited": False,
                        "submissions": 0,
                        "receipts": {},
                    },
                )
            tx.save_receipt(
                owner, "race", digest(key), t["id"], {"fingerprint": digest(body), "id": r["id"]}
            )
            return {"id": r["id"], "room_id": r["room_id"], "version": 1, "status": "waiting"}

    def load(self, tx, race_id, actor, write=False, private_actor=False, full_snapshot=False):
        r = tx.get("races", race_id)
        if not r:
            raise HTTPException(404, "Race not found.")
        now = self.now(tx)
        room = self.room(tx, r["room_id"], actor, now, lock=write)
        if write:
            r = tx.get("races", race_id, lock=True)
            if not r:
                raise HTTPException(404, "Race not found.")
        players = tx.players(race_id, private_for=actor if private_actor else None)
        player = next((p for p in players if p["user_id"] == actor), None)
        if not player:
            raise HTTPException(404, "This account is not a participant in this race.")
        template = tx.get("templates", r["template_id"], light=not full_snapshot)
        if not template:
            raise HTTPException(404, "Source diagram is unavailable.")
        _, current = self.source(tx, r["owner_id"], template["lesson_id"], template["visual_id"])
        if current != template["source_hash"]:
            raise HTTPException(409, "Source visual changed; create a new race.")
        return r, template, room, players, player, now

    def status(self, r, players, now):
        if r["status"] in ("cancelled", "waiting", "finished", "expired"):
            return r["status"]
        if all(p["completed_at"] is not None or p["forfeited"] for p in players):
            return "finished"
        if now < r["started_at"]:
            return "countdown"
        if now >= r["deadline_at"]:
            return "expired"
        return "running"

    def summary(self, r, t, room, players, me, now):
        current = {m["user_id"] for m in room["members"]}
        return {
            "id": r["id"],
            "room_id": r["room_id"],
            "version": r["version"],
            "status": self.status(r, players, now),
            "question": QUESTION,
            "total": len(t["snapshot"]["slots"]),
            "started_at": r["started_at"],
            "deadline_at": r["deadline_at"],
            "server_now": now,
            "my_player_id": me["id"],
            "players": [
                {
                    "id": p["id"],
                    "display_name": p["display_name"] if p["user_id"] in current else "Player left",
                    "placed": p.get("placed_count", len(p.get("placements", {}))),
                    "completed": p["completed_at"] is not None,
                    "forfeited": p["forfeited"],
                }
                for p in players
            ],
        }

    def snapshot(self, race_id, actor):
        with self.store.transaction() as tx:
            r, t, room, players, me, now = self.load(tx, race_id, actor)
            return self.summary(r, t, room, players, me, now)

    def current(self, room_id, actor):
        with self.store.transaction() as tx:
            self.room(tx, room_id, actor, self.now(tx))
            r = tx.latest_race(room_id)
            if not r:
                raise HTTPException(404, "No diagram race has been created in this room.")
            r, t, room, players, me, now = self.load(tx, r["id"], actor)
            return self.summary(r, t, room, players, me, now)

    def action(self, receipts, actor, operation, key, body):
        address = digest([actor, operation, key])
        saved = receipts.get(address)
        if saved and saved["fingerprint"] != digest(body):
            raise HTTPException(409, "Conflicting action retry.")
        if not saved and len(receipts) >= MAX_ACTIONS:
            raise HTTPException(409, "Action receipt limit reached.")
        return address, saved

    def start(self, race_id, actor, body, key):
        with self.store.transaction(write=True) as tx:
            r, t, room, players, me, now = self.load(tx, race_id, actor, write=True)
            if r["owner_id"] != actor or room["host_id"] != actor:
                raise HTTPException(403, "Only the creating host can start the race.")
            address, saved = self.action(r["actions"], actor, "start", key, body)
            if saved:
                return saved["response"]
            if (
                self.status(r, players, now) != "waiting"
                or r["version"] != body["expected_version"]
            ):
                raise HTTPException(409, "Race changed or already started.")
            current = {m["user_id"]: m for m in room["members"]}
            if room["closed"] or any(
                p["user_id"] not in current or not current[p["user_id"]]["ready"] for p in players
            ):
                raise HTTPException(
                    409, "Every captured participant must still be present and ready."
                )
            if len(players) < 2:
                raise HTTPException(409, "At least two participants are required.")
            starts = now + 5000
            deadline = starts + 15 * 60 * 1000
            if datetime.fromisoformat(room["expires_at"]).timestamp() * 1000 < deadline:
                raise HTTPException(409, "Room is expiring; create a new lobby.")
            r.update(
                status="running", version=r["version"] + 1, started_at=starts, deadline_at=deadline
            )
            out = {
                "id": r["id"],
                "version": r["version"],
                "status": "countdown",
                "started_at": starts,
                "deadline_at": deadline,
            }
            r["actions"][address] = {"fingerprint": digest(body), "response": out}
            tx.put("races", r["id"], r)
            return out

    def board(self, race_id, actor):
        with self.store.transaction() as tx:
            r, t, _, players, me, now = self.load(
                tx, race_id, actor, private_actor=True, full_snapshot=True
            )
            if self.status(r, players, now) == "waiting":
                raise HTTPException(409, "The host has not started the shared clock.")
            if r["status"] == "cancelled":
                raise HTTPException(409, "Race was cancelled.")
            return {
                "id": race_id,
                "question": QUESTION,
                "puzzle_svg": t["snapshot"]["puzzle_svg"],
                "view_box": t["snapshot"]["view_box"],
                "slots": [
                    {k: v for k, v in s.items() if k != "text"} for s in t["snapshot"]["slots"]
                ],
                "cards": me["cards"],
                "placements": me["placements"],
                "version": me["version"],
                "started_at": r["started_at"],
                "deadline_at": r["deadline_at"],
                "server_now": now,
            }

    def study(self, race_id, actor):
        with self.store.transaction() as tx:
            r, template, _, players, _, now = self.load(tx, race_id, actor, full_snapshot=True)
            if self.status(r, players, now) != "waiting":
                raise HTTPException(409, "Study view closes when the common race clock starts.")
            return {
                "id": race_id,
                "question": QUESTION,
                "reference_svg": template["snapshot"]["reference_svg"],
                "view_box": template["snapshot"]["view_box"],
            }

    def validate_placements(self, t, me, placements):
        slots = {s["id"] for s in t["snapshot"]["slots"]}
        cards = {c["id"] for c in me["cards"]}
        if not set(placements) <= slots or not set(placements.values()) <= cards:
            raise HTTPException(422, "Unknown slot or another player's card.")
        if len(set(placements.values())) != len(placements):
            raise HTTPException(422, "A card can only occupy one slot.")

    def update_player(self, race_id, actor, operation, body, key):
        with self.store.transaction(write=True) as tx:
            r, t, _, players, me, now = self.load(
                tx, race_id, actor, write=True, private_actor=True
            )
            address, saved = self.action(me["receipts"], actor, operation, key, body)
            if saved:
                return saved["response"]
            if me["completed_at"] is not None:
                raise HTTPException(409, "The first correct finish is final.")
            if self.status(r, players, now) != "running" or me["forfeited"]:
                raise HTTPException(409, "This attempt is not running.")
            if now < r["started_at"]:
                raise HTTPException(503, "Server timing is temporarily unavailable.")
            if me["version"] != body["expected_version"]:
                raise HTTPException(409, "Placements changed; reload your board.")
            correct = None
            incorrect = None
            if operation == "placements":
                self.validate_placements(t, me, body["placements"])
                me["placements"] = body["placements"]
            elif operation == "finish":
                if len(me["placements"]) != len(t["snapshot"]["slots"]):
                    raise HTTPException(409, "Place every label before submitting.")
                labels = {c["id"]: c["text"] for c in me["cards"]}
                correct = sum(
                    labels[me["placements"][s["id"]]] == s["text"] for s in t["snapshot"]["slots"]
                )
                incorrect = [
                    s["id"]
                    for s in t["snapshot"]["slots"]
                    if labels[me["placements"][s["id"]]] != s["text"]
                ]
                me["submissions"] += 1
                if correct == len(t["snapshot"]["slots"]):
                    me["completed_at"] = now
                    me["elapsed_ms"] = max(0, now - r["started_at"])
            elif operation == "forfeit":
                me["forfeited"] = True
                me["placements"] = {}
            else:
                raise ValueError("Unknown internal player operation")
            me["version"] += 1
            out = {
                "id": race_id,
                "version": me["version"],
                "placed": len(me["placements"]),
                "total": len(t["snapshot"]["slots"]),
                "completed": me["completed_at"] is not None,
                "elapsed_ms": me["elapsed_ms"],
                "submissions": me["submissions"],
                "correct_count": correct,
                "incorrect_slot_ids": incorrect,
                "forfeited": me["forfeited"],
            }
            me["receipts"][address] = {"fingerprint": digest(body), "response": out}
            if not tx.update_player(race_id, actor, me):
                raise HTTPException(409, "Player participation was removed; no result was saved.")
            updated = [me if p["user_id"] == actor else p for p in players]
            if self.status(r, updated, now) == "finished":
                r["status"] = "finished"
                r["version"] += 1
                tx.put("races", race_id, r)
            return out

    def results(self, race_id, actor, review=False):
        with self.store.transaction() as tx:
            r, t, room, players, me, now = self.load(tx, race_id, actor, full_snapshot=review)
            status = self.status(r, players, now)
            if status not in ("finished", "expired"):
                raise HTTPException(409, "Results and the solution unlock after all attempts end.")
            current = {m["user_id"] for m in room["members"]}
            times = sorted(p["elapsed_ms"] for p in players if p["completed_at"] is not None)
            entries = [
                {
                    "id": p["id"],
                    "display_name": p["display_name"] if p["user_id"] in current else "Player left",
                    "completed": p["completed_at"] is not None,
                    "elapsed_ms": p["elapsed_ms"],
                    "rank": 1 + sum(v < p["elapsed_ms"] for v in times)
                    if p["completed_at"] is not None
                    else None,
                    "forfeited": p["forfeited"],
                    "submissions": p["submissions"],
                }
                for p in players
            ]
            out = {
                "id": race_id,
                "status": status,
                "players": sorted(
                    entries, key=lambda p: (p["rank"] is None, p["rank"] or 0, p["id"])
                ),
                "winner_ids": [p["id"] for p in entries if p["rank"] == 1],
                "scoring": "fastest_correct_server_elapsed",
            }
            if review:
                out |= {
                    "reference_svg": t["snapshot"]["reference_svg"],
                    "solution": [
                        {"slot_id": s["id"], "text": s["text"]} for s in t["snapshot"]["slots"]
                    ],
                }
            return out

    def cancel(self, race_id, actor, body, key):
        with self.store.transaction(write=True) as tx:
            r, _, _, _, _, _ = self.load(tx, race_id, actor, write=True)
            room = tx.room(r["room_id"])
            if r["owner_id"] != actor and room["host_id"] != actor:
                raise HTTPException(
                    403, "Only the creator or current room host can cancel this race."
                )
            address, saved = self.action(r["actions"], actor, "cancel", key, body)
            if saved:
                return saved["response"]
            if r["version"] != body["expected_version"]:
                raise HTTPException(409, "Race changed.")
            r.update(status="cancelled", version=r["version"] + 1)
            out = {
                "id": race_id,
                "status": "cancelled",
                "version": r["version"],
                "started_at": r["started_at"],
                "deadline_at": r["deadline_at"],
            }
            r["actions"][address] = {"fingerprint": digest(body), "response": out}
            tx.put("races", race_id, r)
            return out
