import os
from pathlib import Path

import psycopg
import pytest

from app.repository import MemoryRepository, PostgresRepository


@pytest.fixture(autouse=True)
def _signed_in_test_client(monkeypatch):
    """The API serves only requests with a valid sign-in token. Tests trust a local test key (never the network)
    and the test client signs a token for each request, for the player named by its `X-Player-ID` header (or the
    default player), so a test says who is calling without building tokens. A request that already has an
    Authorization header is sent as it is, and `X-Test-Anonymous` sends none. The token is real: the app verifies
    its RS256 signature, issuer, expiry and `player_id` claim exactly as in production (tests/jwt_helpers.py)."""
    from starlette.testclient import TestClient

    from tests import jwt_helpers

    monkeypatch.setenv("JWT_ISSUER", jwt_helpers.ISSUER)
    monkeypatch.setenv("JWT_JWKS_JSON", jwt_helpers.jwks_json())
    send = TestClient.send

    def send_signed_in(self, request, *args, **kwargs):
        if "x-test-anonymous" in request.headers:
            del request.headers["x-test-anonymous"]
        elif "authorization" not in request.headers:
            player = request.headers.get("x-player-id", jwt_helpers.DEFAULT_PLAYER)
            request.headers["authorization"] = f"Bearer {jwt_helpers.token(player)}"
        return send(self, request, *args, **kwargs)

    monkeypatch.setattr(TestClient, "send", send_signed_in)


@pytest.fixture(autouse=True)
def _no_embedding_provider(monkeypatch):
    """No test may reach a real embedding provider, whatever keys the developer's .env holds. Tests that need
    vectors install a fake with the `embedder` fixture (tests/learning/test_embeddings.py)."""
    import learning_service.embeddings as embeddings

    monkeypatch.setattr(embeddings, "model_embedder", lambda: None)


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
