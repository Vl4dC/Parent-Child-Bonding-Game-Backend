"""Request-scoped units of work. PostgreSQL is the durable Supabase implementation."""

import copy
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

current_session = ContextVar("api_session")


def encode(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


class UnitOfWork:
    def __init__(self, repository, owner, connection=None):
        self.repository = repository
        self.owner = owner
        self.connection = connection
        self.cache = {}
        self.original = {}
        self.deleted = set()

    def get(self, kind, key, model=None):
        address = (kind, key)
        if address in self.deleted:
            return None
        if address not in self.cache:
            if self.connection:
                row = self.connection.execute(
                    "select data from api_private.records where kind=%s and id=%s for update",
                    (kind, key),
                ).fetchone()
                value = row[0] if row else None
            else:
                value = copy.deepcopy(self.repository.data.get((self.owner, kind, key)))
            if (
                value is not None
                and kind in ("quizzes", "tournaments")
                and "questions" not in value
            ):
                questions = self.get("question_sets", value["question_set_id"])
                if questions is None:
                    raise HTTPException(409, "Question set is unavailable.")
                value["questions"] = questions["questions"]
            self.original[address] = copy.deepcopy(value)
            self.cache[address] = (
                model.model_validate(value) if value is not None and model else value
            )
        value = self.cache[address]
        if value is not None and model is not None and not isinstance(value, model):
            value = model.model_validate(value)
            self.cache[address] = value
        return value

    def put(self, kind, key, value):
        address = (kind, key)
        if address not in self.original:
            self.get(kind, key)
        self.deleted.discard(address)
        self.cache[address] = value

    def delete(self, kind, key):
        self.deleted.add((kind, key))

    def list(self, kind, after="", limit=100):
        if self.connection:
            rows = self.connection.execute(
                "select id, data from api_private.records where kind=%s and id>%s "
                "order by id limit %s",
                (kind, after, limit),
            ).fetchall()
            return [(key, value) for key, value in rows]
        rows = [
            (key, copy.deepcopy(value))
            for (owner, k, key), value in self.repository.data.items()
            if owner == self.owner and k == kind and key > after
        ]
        return sorted(rows)[:limit]

    def flush(self):
        for kind, key in self.deleted:
            if self.connection:
                self.connection.execute(
                    "delete from api_private.records where kind=%s and id=%s", (kind, key)
                )
            else:
                self.repository.data.pop((self.owner, kind, key), None)
        for (kind, key), value in list(self.cache.items()):
            if (kind, key) in self.deleted or value is None:
                continue
            data = encode(value)
            if data == self.original[(kind, key)]:
                continue
            data = copy.deepcopy(data)
            if kind in ("quizzes", "tournaments"):
                question_set = {"questions": data.pop("questions")}
                question_id = data["question_set_id"]
                existing = self.get("question_sets", question_id)
                if existing is not None and existing != question_set:
                    raise HTTPException(409, "Published question snapshots are immutable.")
                if self.connection:
                    self.connection.execute(
                        "insert into api_private.records(owner_id,kind,id,data) values (%s,'question_sets',%s,%s) "
                        "on conflict do nothing",
                        (self.owner, question_id, Jsonb(question_set)),
                    )
                else:
                    self.repository.data[(self.owner, "question_sets", question_id)] = question_set
            if self.connection:
                self.connection.execute(
                    "insert into api_private.records(owner_id,kind,id,data) values (%s,%s,%s,%s) "
                    "on conflict(owner_id,kind,id) do update set data=excluded.data, "
                    "version=api_private.records.version+1, updated_at=now()",
                    (self.owner, kind, key, Jsonb(data)),
                )
            else:
                self.repository.data[(self.owner, kind, key)] = copy.deepcopy(data)


class MemoryRepository:
    """Explicit development/test adapter. Never selected for production application data."""

    durable = False

    def __init__(self):
        self.data = {}
        self.rooms = {}
        self.lock = threading.RLock()

    @contextmanager
    def session(self, owner, serialize=True):
        unit = UnitOfWork(self, owner)
        yield unit
        with self.lock:
            for address, value in unit.cache.items():
                actual = self.data.get((owner, *address))
                proposed = encode(value)
                original = unit.original[address]
                if address[0] in ("quizzes", "tournaments"):
                    original = {
                        k: v for k, v in (original or {}).items() if k != "questions"
                    } or None
                    proposed = {
                        k: v for k, v in (proposed or {}).items() if k != "questions"
                    } or None
                if actual != original and proposed != actual:
                    raise HTTPException(409, "Concurrent update. Read the resource again.")
            unit.flush()

    def ready(self):
        return True


class PostgresRepository:
    durable = True

    def __init__(self, url):
        self.url = url

    @contextmanager
    def session(self, owner, serialize=True):
        with psycopg.connect(self.url, connect_timeout=5) as connection:
            connection.execute("set local role authenticated")
            connection.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": owner, "role": "authenticated"}),),
            )
            # Serialize one owner's state updates across API processes, including missing-row inserts.
            if serialize:
                connection.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner,)
                )
            unit = UnitOfWork(self, owner, connection)
            yield unit
            unit.flush()

    def ready(self):
        with psycopg.connect(self.url, connect_timeout=3) as connection:
            return connection.execute("""select
                to_regclass('api_private.records') is not null
                and to_regclass('api_private.rooms') is not null
                and to_regprocedure('api_private.claim_job(text,integer,integer)') is not null
                and to_regprocedure('api_private.reserve_request(uuid,bigint,integer)') is not null
                and to_regprocedure('api_private.leave_all_rooms()') is not null""").fetchone()[0]


class OwnedStore:
    """Dictionary-compatible facade for the existing generation engine's typed records."""

    def __init__(self, kind, model):
        self.kind, self.model = kind, model

    def get(self, key, default=None):
        value = current_session.get().get(self.kind, key, self.model)
        return default if value is None else value

    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        current_session.get().put(self.kind, key, value)

    def __delitem__(self, key):
        current_session.get().delete(self.kind, key)

    def __iter__(self):
        return iter(key for key, _ in current_session.get().list(self.kind, limit=10000))

    def __len__(self):
        return len(list(iter(self)))

    def clear(self):
        for key in list(self):
            del self[key]


def require_record(kind, key):
    value = current_session.get().get(kind, key)
    if value is None:
        raise HTTPException(404, "Record not found.")
    return value
