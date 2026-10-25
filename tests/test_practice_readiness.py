import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clerk_auth import ClerkVerifier
from app.config import Settings
from app.main import create_app
from app.practice_readiness import check
from app.release_check import inspect
from app.repository import MemoryRepository
from tests.test_clerk_auth import config
from tests.test_clerk_auth import rsa_pair as rsa_pair


def test_flags_are_off_and_anonymous_local_routes_remain_explicit_test_only():
    config = Settings(app_env="test", _env_file=None)
    assert not config.duels_enabled and not config.flashcard_attempts_enabled
    with TestClient(create_app(config)) as client:
        assert client.get("/api/duels/history").status_code == 503
        assert client.get("/api/flashcards/nonexistent/attempts").status_code == 503
        assert client.get("/ready").status_code == 200
    assert check(None, config, "duels")["state"] == "disabled"


def test_ready_combines_clerk_schema_and_storage_without_writing_data(repository, rsa_pair):
    settings = config(rsa_pair[1], duels_enabled=True, flashcard_attempts_enabled=True)
    before = copy.deepcopy(repository.data) if not repository.durable else None
    with TestClient(create_app(settings, repository=repository)) as client:
        response = client.get("/ready")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        features = response.json()["features"]
        assert (
            features["clerk"]["ready"]
            and features["duels"]["ready"]
            and features["flashcard_attempts"]["ready"]
        )
    if not repository.durable:
        assert repository.data == before and not hasattr(repository, "duel_state")


@pytest.mark.parametrize(
    "feature,trigger",
    [("duels", "duel_source_changed"), ("flashcard_attempts", "card_attempt_link_insert")],
)
def test_disabled_or_replica_only_required_trigger_blocks_readiness(
    repository, rsa_pair, feature, trigger
):
    if not repository.durable:
        pytest.skip("Trigger and migration validation require PostgreSQL")
    settings = config(rsa_pair[1], duels_enabled=True, flashcard_attempts_enabled=True)
    try:
        for mode in ["disable", "enable replica"]:
            with repository.control() as c:
                c.execute(f"alter table api_private.records {mode} trigger {trigger}")
            out = check(repository, settings, feature)
            assert not out["ready"] and out["reason"] == "migration_required"
    finally:
        with repository.control() as c:
            c.execute(f"alter table api_private.records enable trigger {trigger}")
    assert check(repository, settings, feature)["ready"]


def test_key_outage_prevents_readiness_and_fails_requests_closed(rsa_pair):
    from tests.test_clerk_auth import signed

    settings = config()
    app = create_app(settings)
    app.state.clerk_verifier.close()
    app.state.clerk_verifier = ClerkVerifier(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(503, text="secret-path"))
    )
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["features"]["clerk"]["reason"] == "verification_keys_unavailable"
        assert "secret-path" not in response.text
        assert (
            client.get(
                "/api/me/profile", headers={"Authorization": signed(rsa_pair[0])}
            ).status_code
            == 503
        )


def test_release_gate_requires_clerk_and_durability_and_does_not_enable_flags(repository, rsa_pair):
    local = Settings(app_env="test", _env_file=None)
    assert (
        inspect(local, repository, ClerkVerifier(local))["reason"] == "clerk_configuration_required"
    )
    settings = config(rsa_pair[1])
    verifier = ClerkVerifier(settings)
    assert inspect(settings, MemoryRepository(), verifier)["reason"] == "durable_database_required"
    if repository.durable:
        result = inspect(settings, repository, verifier)
        assert (
            result["ready"]
            and not result["configuration_changed"]
            and not result["migrations_applied"]
        )
        assert not settings.duels_enabled and not settings.flashcard_attempts_enabled


def test_configured_clerk_rejects_no_token_even_when_new_features_disabled(rsa_pair):
    with TestClient(create_app(config(rsa_pair[1]))) as client:
        for path in [
            "/api/duels/history",
            "/api/flashcards/deck/attempts",
            "/api/me/player",
            "/api/rooms/id",
        ]:
            assert client.get(path).status_code == 401
