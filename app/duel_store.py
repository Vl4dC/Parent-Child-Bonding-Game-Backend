"""Private shared duel state with short transactions and source deletion cascades."""

import copy
import threading
from contextlib import contextmanager

from psycopg.types.json import Jsonb


class DuelTransaction:
    def __init__(self, repository, connection=None, state=None):
        self.repository, self.connection, self.state = repository, connection, state

    def source(self, owner, kind, key, lock=False):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.records where owner_id=%s and kind=%s and id=%s"
                + (" for share" if lock else ""),
                (owner, kind, key),
            ).fetchone()
            data = row[0] if row else None
        else:
            data = copy.deepcopy(self.repository.data.get((owner, kind, key)))
        if data is not None and kind in ("quizzes", "tournaments") and "questions" not in data:
            snapshot = self.source(owner, "question_sets", data["question_set_id"], lock=lock)
            if snapshot is None:
                return None
            data["questions"] = snapshot["questions"]
        return data

    def room(self, room_id, lock=False):
        if self.connection is not None:
            row = self.connection.execute(
                "select state from api_private.rooms where id=%s" + (" for share" if lock else ""),
                (room_id,),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.repository.rooms.get(room_id))

    def match(self, match_id, lock=False):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.duel_matches where id=%s"
                + (" for update" if lock else ""),
                (match_id,),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.state["matches"].get(match_id))

    def put_match(self, match):
        if self.connection is not None:
            self.connection.execute(
                "insert into api_private.duel_matches(id,owner_id,source_kind,source_id,lesson_id,question_set_id,room_id,data) "
                "values(%s,%s,%s,%s,%s,%s,%s,%s) on conflict(id) do update set data=excluded.data",
                (
                    match["id"],
                    match["owner_id"],
                    match["source_kind"],
                    match["source_id"],
                    match["lesson_id"],
                    match["question_set_id"],
                    match["room_id"],
                    Jsonb(match),
                ),
            )
        else:
            self.state["matches"][match["id"]] = copy.deepcopy(match)

    def players(self, match_id):
        if self.connection is not None:
            return [
                row[0]
                for row in self.connection.execute(
                    "select data from api_private.duel_players where match_id=%s order by slot_id",
                    (match_id,),
                ).fetchall()
            ]
        return [
            copy.deepcopy(p)
            for (mid, _), p in sorted(self.state["players"].items())
            if mid == match_id
        ]

    def put_player(self, match_id, player, *, insert=False):
        if self.connection is not None:
            if insert:
                self.connection.execute(
                    "insert into api_private.duel_players(match_id,slot_id,user_id,data) values(%s,%s,%s,%s)",
                    (match_id, player["id"], player["user_id"], Jsonb(player)),
                )
                return True
            return (
                self.connection.execute(
                    "update api_private.duel_players set data=%s where match_id=%s and slot_id=%s",
                    (Jsonb(player), match_id, player["id"]),
                ).rowcount
                == 1
            )
        address = (match_id, player["id"])
        if not insert and address not in self.state["players"]:
            return False
        self.state["players"][address] = copy.deepcopy(player)
        return True

    def receipt(self, owner, operation, key):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.duel_requests where owner_id=%s and operation=%s and key_hash=%s",
                (owner, operation, key),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.state["requests"].get((owner, operation, key)))

    def save_receipt(self, owner, operation, key, match_id, data):
        if self.connection is not None:
            self.connection.execute(
                "insert into api_private.duel_requests(owner_id,operation,key_hash,match_id,data) values(%s,%s,%s,%s,%s)",
                (owner, operation, key, match_id, Jsonb(data)),
            )
        else:
            self.state["requests"][(owner, operation, key)] = copy.deepcopy(
                data | {"match_id": match_id}
            )

    def count(self, owner):
        if self.connection is not None:
            return self.connection.execute(
                "select count(*) from api_private.duel_matches where owner_id=%s", (owner,)
            ).fetchone()[0]
        return sum(m["owner_id"] == owner for m in self.state["matches"].values())

    def latest(self, room_id):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.duel_matches where room_id=%s order by creation_order desc limit 1",
                (room_id,),
            ).fetchone()
            return row[0] if row else None
        matches = [m for m in self.state["matches"].values() if m["room_id"] == room_id]
        return copy.deepcopy(matches[-1]) if matches else None

    def active(self, room_id, now):
        if self.connection is not None:
            return [
                r[0]
                for r in self.connection.execute(
                    "select data from api_private.duel_matches where room_id=%s and data->>'status' in ('waiting','running') and (data->>'expires_at')::bigint>%s order by id for update",
                    (room_id, now),
                ).fetchall()
            ]
        return [
            copy.deepcopy(m)
            for m in self.state["matches"].values()
            if m["room_id"] == room_id
            and m["status"] in ("waiting", "running")
            and m["expires_at"] > now
        ]

    def history(self, actor, after, limit, now):
        if self.connection is not None:
            return [
                r[0]
                for r in self.connection.execute(
                    """select m.data from api_private.duel_matches m
              where m.id>%s and ((m.room_id is null and m.owner_id=%s) or (
                exists(select 1 from api_private.duel_players p where p.match_id=m.id and p.user_id=%s)
                and exists(select 1 from api_private.rooms r where r.id=m.room_id
                  and (r.state->>'expires_at')::timestamptz>clock_timestamp()
                  and exists(select 1 from jsonb_array_elements(r.state->'members') v where v->>'user_id'=%s))
              )) order by m.id limit %s""",
                    (after, actor, actor, actor, limit),
                ).fetchall()
            ]
        from datetime import datetime

        out = []
        for match in self.state["matches"].values():
            if match["id"] <= after:
                continue
            if match["mode"] == "local" and match["owner_id"] == actor:
                out.append(match)
            elif match["mode"] == "room":
                room = self.room(match["room_id"])
                if (
                    room
                    and datetime.fromisoformat(room["expires_at"]).timestamp() * 1000 > now
                    and any(p["user_id"] == actor for p in self.players(match["id"]))
                    and any(p["user_id"] == actor for p in room["members"])
                ):
                    out.append(match)
        return copy.deepcopy(sorted(out, key=lambda m: m["id"])[:limit])


class DuelStore:
    initialization = threading.Lock()

    def __init__(self, repository):
        self.repository = repository
        if not repository.durable:
            with self.initialization:
                if not hasattr(repository, "duel_state"):
                    repository.duel_state = {"matches": {}, "players": {}, "requests": {}}

    @contextmanager
    def transaction(self, actor, *, write=False, room_id=None):
        if self.repository.durable:
            self.repository.open()
            with self.repository.control_pool.connection() as connection:
                if not write:
                    connection.execute("set transaction read only")
                connection.execute(
                    "select set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true)",
                    (
                        str(self.repository.lock_timeout_ms),
                        str(self.repository.statement_timeout_ms),
                    ),
                )
                if write:
                    connection.execute(
                        "select pg_advisory_xact_lock(hashtextextended(%s,0))", (actor,)
                    )
                    if room_id:
                        connection.execute(
                            "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                            ("diagram-room:" + room_id,),
                        )
                yield DuelTransaction(self.repository, connection=connection)
        else:
            with self.repository.lock:
                state = copy.deepcopy(self.repository.duel_state)
                yield DuelTransaction(self.repository, state=state)
                if write:
                    self.repository.duel_state = state


def cleanup_memory(repository, deleted_owner=None):
    from app.duel_game import invalidate, source_reason

    state = repository.duel_state
    clock = getattr(repository, "duel_clock_ms", None)
    import time

    now = int(clock()) if clock else time.time_ns() // 1_000_000
    for mid, m in list(state["matches"].items()):
        if (
            (m["owner_id"], m["source_kind"], m["source_id"]) not in repository.data
            or (m["owner_id"], "lessons", m["lesson_id"]) not in repository.data
            or (m["owner_id"], "question_sets", m["question_set_id"]) not in repository.data
            or (m["room_id"] is not None and m["room_id"] not in repository.rooms)
        ):
            del state["matches"][mid]
            continue
        if deleted_owner:
            removed = [
                k
                for k, p in state["players"].items()
                if k[0] == mid and p["user_id"] == deleted_owner
            ]
            for key in removed:
                del state["players"][key]
            m["actions"] = {k: v for k, v in m["actions"].items() if v["actor"] != deleted_owner}
            m["action_actors"] = [v for v in m["action_actors"] if v != deleted_owner]
            if removed:
                invalidate(m, now, "participant_removed")
        tx = DuelTransaction(repository, state=state)
        reason = source_reason(tx, m)
        if reason:
            invalidate(m, now, reason)
    for key in list(state["players"]):
        if key[0] not in state["matches"]:
            del state["players"][key]
    for key, value in list(state["requests"].items()):
        if value["match_id"] not in state["matches"]:
            del state["requests"][key]
