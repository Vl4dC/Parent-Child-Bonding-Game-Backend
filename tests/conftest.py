import os
from pathlib import Path

import psycopg
import pytest

from app.repository import MemoryRepository, PostgresRepository


@pytest.fixture(scope="session")
def postgres_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL database ending in _test")
    with psycopg.connect(url, autocommit=True) as connection:
        if not connection.info.dbname.endswith("_test"):
            pytest.fail("Refusing to reset a database whose name does not end in _test")
        connection.execute("drop schema if exists api_private cascade")
        # Compatibility fixture only: Supabase provides auth.uid() and these roles itself.
        connection.execute("create schema if not exists auth")
        connection.execute("""do $$ begin
          if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
          if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
        end $$""")
        connection.execute("""create or replace function auth.uid() returns uuid language sql stable as $$
          select (nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub')::uuid
        $$""")
        connection.execute("grant usage on schema auth to authenticated")
        for migration in sorted(Path("supabase/migrations").glob("*.sql")):
            connection.execute(migration.read_text())
    return url


@pytest.fixture(params=["memory", "postgres"])
def repository(request):
    if request.param == "memory":
        yield MemoryRepository()
        return
    url = request.getfixturevalue("postgres_url")
    with psycopg.connect(url) as connection:
        connection.execute(
            "truncate api_private.records, api_private.rooms, api_private.generation_usage, api_private.request_usage"
            " cascade"
        )
    repo = PostgresRepository(url)
    try:
        yield repo
    finally:
        repo.close()
