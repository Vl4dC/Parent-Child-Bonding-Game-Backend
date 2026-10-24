"""Request-scoped units of work. PostgreSQL is the durable Supabase implementation."""

import copy
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

current_session = ContextVar("api_session")


def encode(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


class UnitOfWork:
    def __init__(self, repository, owner, connection=None, readonly=False):
        self.repository = repository
        self.owner = owner
        self.connection = connection
        self.readonly = readonly
        self.versions = {}
        self.cache = {}
        self.original = {}
        self.deleted = set()

    def get(self, kind, key, model=None):
        address = (kind, key)
        if address in self.deleted:
            return None
        if address not in self.cache:
            if self.connection:
                lock = " for update" if not self.readonly and kind != "question_sets" else ""
                row = self.connection.execute(
                    "select data,version from api_private.records where kind=%s and id=%s" + lock,
                    (kind, key),
                ).fetchone()
                value = row[0] if row else None
                if row:
                    self.versions[address] = row[1]
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
            self.original[address] = value if self.readonly else copy.deepcopy(value)
            self.cache[address] = (
                model.model_validate(value) if value is not None and model else value
            )
        value = self.cache[address]
        if value is not None and model is not None and not isinstance(value, model):
            value = model.model_validate(value)
            self.cache[address] = value
        return value

    def put(self, kind, key, value):
        if self.readonly:
            raise RuntimeError("Cannot write in a read-only unit of work")
        address = (kind, key)
        if address not in self.original:
            self.get(kind, key)
        self.deleted.discard(address)
        self.cache[address] = value

    def delete(self, kind, key):
        if self.readonly:
            raise RuntimeError("Cannot delete in a read-only unit of work")
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

    def summaries(self, kind, after="", limit=100):
        fields = ["title", "name", "status", "revision", "lesson_id"]
        if self.connection:
            return self.connection.execute(
                "select id, (select coalesce(jsonb_object_agg(field,data->field),'{}') "
                "from unnest(%s::text[]) field where data ? field) "
                "from api_private.records where kind=%s and id>%s order by id limit %s",
                (fields, kind, after, limit),
            ).fetchall()
        with self.repository.lock:
            keys = sorted(
                key
                for owner, k, key in self.repository.data
                if owner == self.owner and k == kind and key > after
            )[:limit]
            return [
                (key, {f: copy.deepcopy(data[f]) for f in fields if f in data})
                for key in keys
                for data in [self.repository.data[(self.owner, kind, key)]]
            ]

    def pending_jobs(self):
        if self.connection:
            return self.connection.execute(
                "select count(*) from api_private.records where kind='jobs' "
                "and data->>'status' in ('queued','running')"
            ).fetchone()[0]
        with self.repository.lock:
            return sum(
                data["status"] in ("queued", "running")
                for (owner, kind, _), data in self.repository.data.items()
                if owner == self.owner and kind == "jobs"
            )

    def progress(self):
        keys = [
            "lessons_completed",
            "quiz_answers",
            "correct_answers",
            "flashcard_reviews",
            "tournaments_submitted",
        ]
        if self.connection:
            row = self.connection.execute("""select
                count(*) filter (where kind='completions'),
                coalesce(sum(q.answered),0), coalesce(sum(q.correct),0),
                coalesce(sum(case when kind='decks' then (data#>>'{progress,reviews}')::bigint else 0 end),0),
                count(*) filter (where kind='tournaments' and data->'submission' <> 'null'::jsonb)
                from api_private.records
                left join lateral (select count(*) answered,
                    count(*) filter (where value->>'correct'='true') correct
                    from jsonb_each(case when kind='quizzes' then data->'attempts' else '{}'::jsonb end)) q on true
                where kind in ('completions','quizzes','decks','tournaments')""").fetchone()
            return dict(zip(keys, map(int, row)))
        totals = dict.fromkeys(keys, 0)
        with self.repository.lock:
            for (owner, kind, _), data in self.repository.data.items():
                if owner != self.owner:
                    continue
                if kind == "completions":
                    totals["lessons_completed"] += 1
                elif kind == "quizzes":
                    totals["quiz_answers"] += len(data["attempts"])
                    totals["correct_answers"] += sum(
                        a["correct"] for a in data["attempts"].values()
                    )
                elif kind == "decks":
                    totals["flashcard_reviews"] += data["progress"]["reviews"]
                elif kind == "tournaments" and data.get("submission"):
                    totals["tournaments_submitted"] += 1
        return totals

    def flush(self):
        if self.readonly:
            return
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
        if self.connection and getattr(self, "forget_diagram_player", False):
            self.connection.execute("select api_private.diagram_forget_player()")
        if not self.connection and hasattr(self.repository, "diagram_state"):
            from app.diagram_store import cleanup_memory

            cleanup_memory(
                self.repository,
                self.owner if getattr(self, "forget_diagram_player", False) else None,
            )


class MemoryRepository:
    """Explicit development/test adapter. Never selected for production application data."""

    durable = False

    def __init__(self):
        self.data = {}
        self.rooms = {}
        self.lock = threading.RLock()

    @contextmanager
    def session(self, owner, serialize=True):
        unit = UnitOfWork(self, owner, readonly=not serialize)
        yield unit
        if unit.readonly:
            return
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

    def __init__(
        self,
        url,
        *,
        pool_size=8,
        control_pool_size=2,
        pool_timeout=5,
        lock_timeout_ms=3000,
        statement_timeout_ms=30000,
    ):
        self.url = url
        self.lock_timeout_ms = lock_timeout_ms
        self.statement_timeout_ms = statement_timeout_ms
        self._pool_lock = threading.Lock()
        self._opened = False
        self._closed = False
        options = {
            "min_size": 0,
            "timeout": pool_timeout,
            "max_waiting": 64,
            "open": False,
            "num_workers": 1,
            "reconnect_timeout": 30,
            "kwargs": {"connect_timeout": 5, "prepare_threshold": None},
        }
        self.pool = ConnectionPool(url, max_size=pool_size, name="api-data", **options)
        # Independent short control transactions cannot deadlock behind data connections
        # held by requests which need to reserve a quota before proceeding.
        self.control_pool = ConnectionPool(
            url, max_size=control_pool_size, name="api-control", **options
        )

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            control_pool_size=settings.database_control_pool_size,
            pool_timeout=settings.database_pool_timeout,
            lock_timeout_ms=settings.database_lock_timeout_ms,
            statement_timeout_ms=settings.database_statement_timeout_ms,
        )

    def open(self):
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("Repository has been closed")
            if not self._opened:
                self.pool.open()
                self.control_pool.open()
                self._opened = True

    def close(self):
        with self._pool_lock:
            self._closed = True
            self.pool.close()
            self.control_pool.close()

    @contextmanager
    def control(self):
        self.open()
        with self.control_pool.connection() as connection:
            connection.execute(
                "select set_config('lock_timeout',%s,true), set_config('statement_timeout',%s,true)",
                (str(self.lock_timeout_ms), str(self.statement_timeout_ms)),
            )
            yield connection

    @contextmanager
    def session(self, owner, serialize=True):
        self.open()
        with self.pool.connection() as connection:
            if not serialize:
                connection.execute("set transaction read only")
            connection.execute("set local role authenticated")
            connection.execute(
                "select set_config('request.jwt.claims',%s,true), "
                "set_config('lock_timeout',%s,true), set_config('statement_timeout',%s,true)",
                (
                    json.dumps({"sub": owner, "role": "authenticated"}),
                    str(self.lock_timeout_ms),
                    str(self.statement_timeout_ms),
                ),
            )
            # Serialize one owner's state updates across API processes, including missing-row inserts.
            if serialize:
                connection.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner,)
                )
            unit = UnitOfWork(self, owner, connection, readonly=not serialize)
            yield unit
            unit.flush()

    def ready(self):
        with self.control() as connection:
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
