import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.generation import reserve

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"


def as_user(connection, owner):
    connection.execute("set local role authenticated")
    connection.execute(
        "select set_config('request.jwt.claims',%s,true)", (json.dumps({"sub": owner}),)
    )


def test_database_policies_enforce_ownership_even_without_api(repository):
    if not repository.durable:
        pytest.skip("Real RLS test requires PostgreSQL")
    with repository.session(A) as unit:
        unit.put("profile", "self", {"display_name": "private"})
    with psycopg.connect(repository.url) as connection:
        as_user(connection, B)
        assert connection.execute("select * from api_private.records").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "insert into api_private.records(owner_id,kind,id,data) values (%s,'profile','forged','{}')",
                (A,),
            )
    with psycopg.connect(repository.url) as connection:
        as_user(connection, None)
        assert connection.execute("select * from api_private.records").fetchall() == []


def test_concurrent_budget_reservations_cannot_exceed_limit(repository):
    def attempt(_):
        try:
            reserve(repository, A, 3)
            return True
        except Exception as error:
            assert error.status_code == 429
            return False

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(attempt, range(12))) == 3


def test_transaction_rolls_back_partial_writes(repository):
    with pytest.raises(RuntimeError):
        with repository.session(A) as unit:
            unit.put("profile", "self", {"display_name": "not committed"})
            raise RuntimeError("failure")
    with repository.session(A) as unit:
        assert unit.get("profile", "self") is None


def test_worker_functions_cannot_be_called_by_browser_role(repository):
    if not repository.durable:
        pytest.skip("Real SQL permission test requires PostgreSQL")
    for query in [
        "select api_private.claim_job('forged',2,2)",
        f"select api_private.reserve_generation('{A}', '2026-10-24', 100)",
    ]:
        with psycopg.connect(repository.url) as connection:
            as_user(connection, A)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(query)


def test_database_room_function_rejects_invalid_settings(repository):
    if not repository.durable:
        pytest.skip("Real SQL function test requires PostgreSQL")
    from psycopg.types.json import Jsonb

    with psycopg.connect(repository.url) as connection:
        as_user(connection, A)
        for payload in [
            {"capacity": None},
            {"capacity": 2, "display_name": []},
            {"capacity": 2.5, "display_name": "A"},
        ]:
            result = connection.execute(
                "select api_private.room_command('create',null,%s)", (Jsonb(payload),)
            ).fetchone()[0]
            assert result["status"] == 422
