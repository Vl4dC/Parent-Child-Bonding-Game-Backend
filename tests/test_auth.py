"""Public API regression checks after removing the application's login/auth layer."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REMOVED_ROUTES = [
    ("GET", "/auth/demo-accounts"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/verify"),
    ("POST", "/auth/resend"),
    ("POST", "/auth/refresh"),
    ("GET", "/auth/me"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/forgot-password"),
    ("PUT", "/auth/password"),
]
PROFILE = "/api/me/profile"


@pytest.fixture
def public_client(monkeypatch):
    def no_auth_client(*args, **kwargs):
        pytest.fail("The API must not create an outbound authentication client")

    monkeypatch.setattr(httpx, "AsyncClient", no_auth_client)
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as client:
        yield client


def test_login_routes_stay_absent_and_only_social_has_security(public_client):
    schema = public_client.get("/openapi.json").json()
    for method, path in REMOVED_ROUTES:
        assert (
            public_client.request(method, path, json={"password": "private-password"}).status_code
            == 404
        )
        assert path not in schema["paths"]
    assert not schema.get("security")
    assert set(schema["components"]["securitySchemes"]) == {"ClerkSession"}
    for url, path in schema["paths"].items():
        if url.startswith("/api/social/"):
            assert all(op.get("security") == [{"ClerkSession": []}] for op in path.values())
            continue
        for operation in path.values():
            if isinstance(operation, dict):
                assert not operation.get("security")
                assert "401" not in operation.get("responses", {})
                assert not any(
                    p.get("name", "").lower() == "authorization"
                    for p in operation.get("parameters", [])
                )
    assert "/api/registration/learners" in schema["paths"]
    assert not any(path.startswith("/auth/") for path in schema["paths"])


@pytest.mark.parametrize(
    "authorization", [None, "Basic invalid", "Bearer expired", "Bearer demo_parent"]
)
def test_profile_works_without_credentials_and_ignores_old_auth_headers(
    public_client, authorization
):
    headers = {"Authorization": authorization} if authorization else {}
    assert (
        public_client.put(
            PROFILE, headers=headers, json={"display_name": "Public player"}
        ).status_code
        == 200
    )
    response = public_client.get(PROFILE)
    assert response.status_code == 200
    assert response.json()["display_name"] == "Public player"
    assert response.headers["cache-control"] == "no-store"
    assert public_client.get("/health").json() == {"status": "ok"}


def test_player_ids_select_state_without_authentication(public_client):
    first = {"X-Player-ID": "00000000-0000-4000-8000-000000000001"}
    second = {"X-Player-ID": "00000000-0000-4000-8000-000000000002"}
    assert (
        public_client.put(PROFILE, headers=first, json={"display_name": "First"}).status_code == 200
    )
    assert public_client.get(PROFILE, headers=second).json()["display_name"] == "Learner"
    assert public_client.get(PROFILE).json()["display_name"] == "First"
    # The caller can freely choose another partition: player IDs confer no identity or privacy.
    assert public_client.get(PROFILE, headers=first).json()["display_name"] == "First"
    assert public_client.get(PROFILE, headers={"X-Player-ID": "invalid-id"}).status_code == 422


def test_public_rooms_work_with_no_tokens(public_client):
    room = public_client.post("/api/rooms", json={"display_name": "Alex"})
    assert room.status_code == 201, room.text
    room = room.json()
    other = {"X-Player-ID": "00000000-0000-4000-8000-000000000002"}
    joined = public_client.post(
        "/api/rooms/join", headers=other, json={"code": room["code"], "display_name": "Sam"}
    )
    assert joined.status_code == 200, joined.text
    assert len(joined.json()["members"]) == 2
    assert public_client.get(f"/api/rooms/{room['id']}").status_code == 200


def test_public_api_still_redacts_invalid_values(public_client):
    response = public_client.put(
        PROFILE, json={"display_name": "Alex", "password": "private-password"}
    )
    assert response.status_code == 422
    assert "private-password" not in response.text
    assert '"input"' not in response.text


def test_cors_allows_player_header_for_exact_origins(public_client):
    for origin, allowed in [("http://localhost:3000", True), ("https://evil.example", False)]:
        response = public_client.options(
            PROFILE,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Player-ID",
            },
        )
        assert (response.headers.get("access-control-allow-origin") == origin) is allowed
    with pytest.raises(ValueError):
        Settings(cors_origins=["*"], _env_file=None)
