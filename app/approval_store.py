"""Private control-plane storage; never exposed through owned-resource APIs."""

import copy
import threading
from contextlib import contextmanager

from psycopg.types.json import Jsonb


class ApprovalTransaction:
    def __init__(self, data=None, connection=None):
        self.data = data
        self.connection = connection

    def get(self, namespace, key):
        if self.connection is not None:
            row = self.connection.execute(
                "select data from api_private.approval_records where namespace=%s and id=%s",
                (namespace, key),
            ).fetchone()
            return row[0] if row else None
        return copy.deepcopy(self.data.get((namespace, key)))

    def put(self, namespace, key, data):
        if self.connection is not None:
            self.connection.execute(
                "insert into api_private.approval_records(namespace,id,data) values (%s,%s,%s) "
                "on conflict(namespace,id) do update set data=excluded.data",
                (namespace, key, Jsonb(data)),
            )
        else:
            self.data[(namespace, key)] = copy.deepcopy(data)

    def delete(self, namespace, key):
        if self.connection is not None:
            self.connection.execute(
                "delete from api_private.approval_records where namespace=%s and id=%s",
                (namespace, key),
            )
        else:
            self.data.pop((namespace, key), None)

    def list(self, namespace, *, after="", limit=51, email=None):
        if self.connection is not None:
            return self.connection.execute(
                "select id,data from api_private.approval_records "
                "where namespace=%s and id>%s "
                "and (%s::text is null or data->>'guardian_email'=%s) order by id limit %s",
                (namespace, after, email, email, limit),
            ).fetchall()
        return sorted(
            (key, copy.deepcopy(value))
            for (ns, key), value in self.data.items()
            if ns == namespace
            and key > after
            and (email is None or value.get("guardian_email") == email)
        )[:limit]

    def due_mail(self, now):
        if self.connection is not None:
            return self.connection.execute(
                "select id,data from api_private.approval_records where namespace='outbox' "
                "and data->>'status' in ('queued','running') "
                "and (data->>'lease_until')::bigint<=%s "
                "and (data->>'next_attempt_at')::bigint<=%s "
                "order by (data->>'next_attempt_at')::bigint,id limit 100",
                (now, now),
            ).fetchall()
        return sorted(
            (
                (key, copy.deepcopy(value))
                for (ns, key), value in self.data.items()
                if ns == "outbox"
                and value["status"] in ("queued", "running")
                and value["lease_until"] <= now
                and value["next_attempt_at"] <= now
            ),
            key=lambda row: (row[1]["next_attempt_at"], row[0]),
        )[:100]


class ApprovalStore:
    """One short lock per transaction serializes the small approval control plane.

    PostgreSQL is durable across processes; the explicit memory adapter shares state per
    repository instance only. Neither adapter exposes this data through UnitOfWork.
    """

    _initialization_lock = threading.Lock()

    def __init__(self, repository):
        self.repository = repository
        if not repository.durable:
            with self._initialization_lock:
                if not hasattr(repository, "approval_data"):
                    repository.approval_data = {}

    @contextmanager
    def transaction(self):
        if self.repository.durable:
            with self.repository.control() as connection:
                connection.execute(
                    "select pg_advisory_xact_lock(hashtextextended('learner-approval-v1',0))"
                )
                yield ApprovalTransaction(connection=connection)
        else:
            with self.repository.lock:
                data = copy.deepcopy(self.repository.approval_data)
                yield ApprovalTransaction(data=data)
                self.repository.approval_data = data
