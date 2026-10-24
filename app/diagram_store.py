"""Private diagram snapshots and match state, isolated from browser/Data API roles."""

import copy
import threading
from contextlib import contextmanager

from psycopg.types.json import Jsonb


class DiagramTransaction:
    def __init__(self, repository, connection=None, state=None):
        self.repository, self.connection, self.state = repository, connection, state

    def source(self, owner, lesson_id, lock=False):
        if self.connection is not None:
            row = self.connection.execute(
                "select data,version from api_private.records "
                "where owner_id=%s and kind='lessons' and id=%s" + (" for share" if lock else ""),
                (owner, lesson_id),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.repository.data.get((owner, "lessons", lesson_id)))

    def room(self, room_id, lock=False):
        if self.connection is not None:
            row = self.connection.execute(
                "select state from api_private.rooms where id=%s" + (" for share" if lock else ""),
                (room_id,),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.repository.rooms.get(room_id))

    def get(self, kind, key, lock=False, light=False):
        if self.connection is not None:
            # Table names come exclusively from this fixed map, never from a request.
            table = {"templates": "diagram_templates", "races": "diagram_races"}[kind]
            projection = "data"
            if kind == "templates" and light:
                projection = "(data - 'snapshot') || jsonb_build_object('snapshot',jsonb_build_object('slots',data#>'{snapshot,slots}','renderer',data#>'{snapshot,renderer}'))"
            row = self.connection.execute(
                f"select {projection} from api_private.{table} where id=%s"
                + (" for update" if lock else ""),
                (key,),
            ).fetchone()
            return row[0] if row else None
        value = self.state[kind].get(key)
        if value and kind == "templates" and light:
            return copy.deepcopy(
                {k: v for k, v in value.items() if k != "snapshot"}
                | {
                    "snapshot": {
                        "slots": value["snapshot"]["slots"],
                        "renderer": value["snapshot"]["renderer"],
                    }
                }
            )
        return copy.deepcopy(value)

    def put(self, kind, key, data):
        if self.connection is not None:
            if kind == "templates":
                self.connection.execute(
                    "insert into api_private.diagram_templates(id,owner_id,lesson_id,visual_id,source_hash,data) "
                    "values (%s,%s,%s,%s,%s,%s)",
                    (
                        key,
                        data["owner_id"],
                        data["lesson_id"],
                        data["visual_id"],
                        data["source_hash"],
                        Jsonb(data),
                    ),
                )
            elif kind == "races":
                self.connection.execute(
                    "insert into api_private.diagram_races(id,owner_id,template_id,room_id,data) "
                    "values (%s,%s,%s,%s,%s) on conflict(id) do update set data=excluded.data",
                    (key, data["owner_id"], data["template_id"], data["room_id"], Jsonb(data)),
                )
        else:
            if kind == "races" and key not in self.state["races"]:
                self.state["sequence"] += 1
                self.state["race_order"][key] = self.state["sequence"]
            self.state[kind][key] = copy.deepcopy(data)

    def latest_race(self, room_id):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.diagram_races where room_id=%s order by creation_order desc limit 1",
                (room_id,),
            ).fetchone()
            return row[0] if row else None
        races = [r for r in self.state["races"].values() if r["room_id"] == room_id]
        if not races:
            return None
        return copy.deepcopy(max(races, key=lambda r: self.state["race_order"][r["id"]]))

    def template_for_source(self, owner, lesson_id, visual_id, fingerprint):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.diagram_templates where owner_id=%s and lesson_id=%s "
                "and visual_id=%s and source_hash=%s",
                (owner, lesson_id, visual_id, fingerprint),
            ).fetchone()
            return row[0] if row else None
        return next(
            (
                copy.deepcopy(v)
                for v in self.state["templates"].values()
                if v["owner_id"] == owner
                and v["lesson_id"] == lesson_id
                and v["visual_id"] == visual_id
                and v["source_hash"] == fingerprint
            ),
            None,
        )

    def receipt(self, owner, operation, key):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.diagram_requests where owner_id=%s and operation=%s and key_hash=%s",
                (owner, operation, key),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.state["requests"].get((owner, operation, key)))

    def save_receipt(self, owner, operation, key, template_id, data):
        if self.connection is not None:
            self.connection.execute(
                "insert into api_private.diagram_requests(owner_id,operation,key_hash,template_id,data) "
                "values(%s,%s,%s,%s,%s)",
                (owner, operation, key, template_id, Jsonb(data)),
            )
        else:
            self.state["requests"][(owner, operation, key)] = copy.deepcopy(
                data | {"template_id": template_id}
            )

    def players(self, race_id, private_for=None):
        if self.connection is not None:
            return [
                row[0]
                for row in self.connection.execute(
                    "select case when user_id=%s::uuid then data else "
                    "(data - 'cards' - 'placements' - 'receipts') || jsonb_build_object('placed_count', "
                    "(select count(*) from jsonb_each(data->'placements'))) end "
                    "from api_private.diagram_players where race_id=%s order by user_id",
                    (private_for, race_id),
                ).fetchall()
            ]
        return [
            copy.deepcopy(
                p
                if p["user_id"] == private_for
                else {k: v for k, v in p.items() if k not in ("cards", "placements", "receipts")}
                | {"placed_count": len(p["placements"])}
            )
            for (rid, _), p in sorted(self.state["players"].items())
            if rid == race_id
        ]

    def active_races(self, room_id):
        if self.connection is not None:
            return [
                row[0]
                for row in self.connection.execute(
                    "select data from api_private.diagram_races where room_id=%s "
                    "and data->>'status' in ('waiting','running') order by id limit 100 for update",
                    (room_id,),
                ).fetchall()
            ]
        return [
            copy.deepcopy(r)
            for r in self.state["races"].values()
            if r["room_id"] == room_id and r["status"] in ("waiting", "running")
        ]

    def put_player(self, race_id, user_id, data):
        if self.connection is not None:
            self.connection.execute(
                "insert into api_private.diagram_players(race_id,user_id,data) values(%s,%s,%s) "
                "on conflict(race_id,user_id) do update set data=excluded.data",
                (race_id, user_id, Jsonb(data)),
            )
        else:
            self.state["players"][(race_id, user_id)] = copy.deepcopy(data)

    def update_player(self, race_id, user_id, data):
        if self.connection is not None:
            return (
                self.connection.execute(
                    "update api_private.diagram_players set data=%s where race_id=%s and user_id=%s",
                    (Jsonb(data), race_id, user_id),
                ).rowcount
                == 1
            )
        if (race_id, user_id) not in self.state["players"]:
            return False
        self.state["players"][(race_id, user_id)] = copy.deepcopy(data)
        return True


class DiagramStore:
    initialization = threading.Lock()

    def __init__(self, repository):
        self.repository = repository
        if not repository.durable:
            with self.initialization:
                if not hasattr(repository, "diagram_state"):
                    repository.diagram_state = {
                        "templates": {},
                        "races": {},
                        "players": {},
                        "requests": {},
                        "sequence": 0,
                        "race_order": {},
                    }

    @contextmanager
    def transaction(self, *, write=False, serial=None):
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
                if serial is not None:
                    connection.execute(
                        "select pg_advisory_xact_lock(hashtextextended(%s,0))", (serial,)
                    )
                yield DiagramTransaction(self.repository, connection=connection)
        else:
            with self.repository.lock:
                state = copy.deepcopy(self.repository.diagram_state)
                yield DiagramTransaction(self.repository, state=state)
                if write:
                    self.repository.diagram_state = state


def cleanup_memory(repository, deleted_owner=None):
    """Called after a successful memory UnitOfWork flush, mirroring SQL cascades/deletion."""
    state = getattr(repository, "diagram_state", None)
    if state is None:
        return
    for tid, t in list(state["templates"].items()):
        if (t["owner_id"], "lessons", t["lesson_id"]) not in repository.data:
            del state["templates"][tid]
    for rid, r in list(state["races"].items()):
        if r["template_id"] not in state["templates"]:
            del state["races"][rid]
            state["race_order"].pop(rid, None)
    for address in list(state["players"]):
        if address[0] not in state["races"] or address[1] == deleted_owner:
            del state["players"][address]
    for address, r in list(state["requests"].items()):
        if r["template_id"] not in state["templates"]:
            del state["requests"][address]
