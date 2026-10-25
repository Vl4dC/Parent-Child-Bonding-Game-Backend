"""Invitation state machine. PostgreSQL acceptance and lobby join share a transaction."""

import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from app.repository import current_session
from app.rooms import command
from learning_service import presence

COLUMNS = (
    "id",
    "sender_id",
    "recipient_id",
    "room_id",
    "game",
    "mode",
    "status",
    "created_at",
    "expires_at",
    "updated_at",
    "request_key",
    "fingerprint",
)
SELECT = ",".join(COLUMNS)
MAX_PENDING = 20


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def state(invite: dict, now: int) -> str:
    return (
        "expired"
        if invite["status"] == "pending" and invite["expires_at"] <= now
        else invite["status"]
    )


def public(invite: dict) -> dict:
    return {
        key: value for key, value in invite.items() if key not in ("request_key", "fingerprint")
    } | {
        "status": state(invite, presence.clock()),
        **{key: presence.stamp(invite[key]) for key in ("created_at", "expires_at", "updated_at")},
    }


def row(values) -> dict | None:
    if values is None:
        return None
    result = dict(zip(COLUMNS, values))
    for key in ("id", "sender_id", "recipient_id"):
        result[key] = str(result[key])
    return result


class Store:
    def __init__(self):
        self.unit = current_session.get()
        self.db = self.unit.connection
        self.owner = self.unit.owner
        self.repo = self.unit.repository

    @contextmanager
    def transaction(self):
        if self.db:
            yield self  # api_context commits/rolls back this request's PostgreSQL transaction
        else:
            with self.repo.lock:
                if not hasattr(self.repo, "game_invites"):
                    self.repo.game_invites = {}
                if self.unit.readonly:
                    yield self
                    return
                old = copy.deepcopy(self.repo.game_invites)
                rooms = copy.deepcopy(self.repo.rooms)
                try:
                    yield self
                except BaseException:
                    self.repo.game_invites, self.repo.rooms = old, rooms
                    raise

    def get(self, id: str, *, lock=False) -> dict:
        if self.db:
            found = row(
                self.db.execute(
                    f"select {SELECT} from api_private.game_invites where id=%s"
                    + (" for update" if lock else ""),
                    (id,),
                ).fetchone()
            )
        else:
            found = copy.deepcopy(getattr(self.repo, "game_invites", {}).get(id))
        if found is None or self.owner not in (found["sender_id"], found["recipient_id"]):
            raise HTTPException(404, "Invitation not found.")
        return found

    def replay(self, key: str) -> dict | None:
        if self.db:
            return row(
                self.db.execute(
                    f"select {SELECT} from api_private.game_invites where sender_id=%s and request_key=%s",
                    (self.owner, key),
                ).fetchone()
            )
        return next(
            (
                copy.deepcopy(i)
                for i in self.repo.game_invites.values()
                if i["sender_id"] == self.owner and i["request_key"] == key
            ),
            None,
        )

    def insert(self, invite: dict):
        if self.db:
            self.db.execute(
                f"insert into api_private.game_invites ({SELECT}) values ({','.join(['%s'] * len(COLUMNS))})",
                tuple(invite[key] for key in COLUMNS),
            )
        else:
            self.repo.game_invites[invite["id"]] = copy.deepcopy(invite)

    def update(self, invite: dict, status: str):
        invite.update(status=status, updated_at=presence.clock())
        if self.db:
            self.db.execute(
                "update api_private.game_invites set status=%s,updated_at=%s where id=%s",
                (status, invite["updated_at"], invite["id"]),
            )
        else:
            self.repo.game_invites[invite["id"]] = copy.deepcopy(invite)

    def outgoing(self) -> list[dict]:
        if self.db:
            return [
                row(r)
                for r in self.db.execute(
                    f"select {SELECT} from api_private.game_invites where sender_id=%s "
                    "and (status='pending' or created_at>=%s)",
                    (self.owner, presence.clock() - 3600),
                ).fetchall()
            ]
        return [
            copy.deepcopy(i)
            for i in self.repo.game_invites.values()
            if i["sender_id"] == self.owner
        ]

    def page(self, direction: str, status: str | None, after: str | None, limit: int) -> dict:
        anchor = self.get(after) if after else None
        now = presence.clock()
        if self.db:
            clauses, args = [], []
            if direction != "all":
                column = "recipient_id" if direction == "incoming" else "sender_id"
                clauses.append(f"{column}=%s")
                args.append(self.owner)
            if status:
                clauses.append(
                    "(case when status='pending' and expires_at<=%s then 'expired' else status end)=%s"
                )
                args += [now, status]
            if anchor:
                clauses.append("(created_at,id)<(%s,%s::uuid)")
                args += [anchor["created_at"], anchor["id"]]
            args.append(limit + 1)
            where = " and ".join(clauses) or "true"
            found = [
                row(r)
                for r in self.db.execute(
                    f"select {SELECT} from api_private.game_invites where {where} "
                    "order by created_at desc,id desc limit %s",
                    args,
                ).fetchall()
            ]
        else:
            found = [
                copy.deepcopy(i)
                for i in getattr(self.repo, "game_invites", {}).values()
                if self.owner in (i["sender_id"], i["recipient_id"])
                and (
                    direction == "all"
                    or i["recipient_id" if direction == "incoming" else "sender_id"] == self.owner
                )
                and (status is None or state(i, now) == status)
                and (
                    anchor is None
                    or (i["created_at"], i["id"]) < (anchor["created_at"], anchor["id"])
                )
            ]
            found.sort(key=lambda i: (i["created_at"], i["id"]), reverse=True)
        return {
            "items": [public(i) for i in found[:limit]],
            "next_cursor": found[limit - 1]["id"] if len(found) > limit else None,
            "server_now": presence.stamp(now),
            "poll_after_seconds": 15,
        }

    def room(self, invite: dict) -> dict:
        if self.db:
            found = self.db.execute(
                "select api_private.game_invite_room(%s)", (invite["id"],)
            ).fetchone()[0]
        else:
            found = copy.deepcopy(self.repo.rooms.get(invite["room_id"]))
        if found is None:
            raise HTTPException(409, "The invited lobby is no longer available.")
        valid_room(found, invite["sender_id"])
        return found


def valid_room(room: dict, sender: str):
    if room["closed"] or datetime.fromisoformat(room["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(409, "The invited lobby is closed or expired.")
    if room["host_id"] != sender:
        raise HTTPException(409, "The inviter is no longer the lobby host.")


def create(home, body: dict, key: str, settings) -> dict:
    store = Store()
    fingerprint = digest(json.dumps(body, sort_keys=True))
    with store.transaction():
        previous = store.replay(digest(key))
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "Idempotency key already used for another invitation.")
            return public(previous)
        friend = body["friend_id"]
        presence.require_friend(home, store.owner, friend)
        if not presence.settings(home, friend)["allow_game_invites"]:
            raise HTTPException(409, "This friend is not accepting game invitations.")
        room = command("snapshot", body["room_id"])
        valid_room(room, store.owner)
        if any(m["user_id"] == friend for m in room["members"]):
            raise HTTPException(409, "This friend is already in the lobby.")
        if len(room["members"]) >= room["capacity"]:
            raise HTTPException(409, "The lobby is full.")
        if body["mode"] == "parent_child" and room["capacity"] != 2:
            raise HTTPException(422, "Use a two-player lobby for a parent-child duel.")
        if body["mode"] == "duo" and (room["capacity"] != 4 or body["game"] == "diagram"):
            raise HTTPException(422, "Duo needs a four-player lobby and an MCQ or heads-up game.")
        now = presence.clock()
        outgoing = store.outgoing()
        pending = []
        for invitation in outgoing:
            if state(invitation, now) == "expired" and invitation["status"] == "pending":
                store.update(invitation, "expired")
            if invitation["status"] == "pending":
                pending.append(invitation)
        if any(i["recipient_id"] == friend and i["room_id"] == room["id"] for i in pending):
            raise HTTPException(
                409, "A pending invitation to this friend and lobby already exists."
            )
        if (
            len(pending) >= MAX_PENDING
            or sum(i["created_at"] >= now - 3600 for i in outgoing)
            >= settings.game_invites_per_hour
        ):
            raise HTTPException(429, "Game invitation limit reached. Wait before inviting again.")
        invite = {
            "id": str(uuid4()),
            "sender_id": store.owner,
            "recipient_id": friend,
            "room_id": room["id"],
            "game": body["game"],
            "mode": body["mode"],
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "expires_at": min(
                now + settings.game_invite_ttl_seconds,
                int(datetime.fromisoformat(room["expires_at"]).timestamp()),
            ),
            "request_key": digest(key),
            "fingerprint": fingerprint,
        }
        if invite["expires_at"] <= now:
            raise HTTPException(409, "The lobby is expiring; create a new one.")
        store.insert(invite)
        return public(invite)


def act(home, id: str, action: str) -> dict:
    store = Store()
    with store.transaction():
        invite = store.get(id, lock=True)
        actor = invite["sender_id"] if action == "cancel" else invite["recipient_id"]
        if store.owner != actor:
            raise HTTPException(
                403, "Only the invitation's recipient can respond, or its sender cancel."
            )
        desired = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}[action]
        status = state(invite, presence.clock())
        room = None
        if status == desired:
            if desired == "accepted":
                try:
                    room = command("snapshot", invite["room_id"])
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            return {"invite": public(invite), "room": room}
        if status != "pending":
            raise HTTPException(409, f"This invitation is {status}.")
        if action == "accept":
            presence.require_friend(home, store.owner, invite["sender_id"])
            profile = home.get_home(store.owner).owner
            target = store.room(invite)
            if state(invite, presence.clock()) != "pending":
                raise HTTPException(409, "This invitation expired while waiting for the lobby.")
            room = command(
                "join", payload={"code": target["code"], "display_name": profile.display_name}
            )
        store.update(invite, desired)
        return {"invite": public(invite), "room": room}


def forget_player(unit):
    if unit.connection:
        unit.connection.execute(
            "delete from api_private.game_invites where sender_id=%s or recipient_id=%s",
            (unit.owner, unit.owner),
        )
    else:
        with unit.repository.lock:
            invites = getattr(unit.repository, "game_invites", {})
            for id, invite in list(invites.items()):
                if unit.owner in (invite["sender_id"], invite["recipient_id"]):
                    del invites[id]
