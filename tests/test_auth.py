"""The backend consumes verified identities but no longer hosts account/session endpoints."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

USER = {
    "id": "b6371062-4a7b-4b1f-9c9d-e4189a21e325",
    "email": "player@example.com",
    "user_metadata": {"account_type": "learner"},
    "email_confirmed_at": "2026-10-24T12:00:00Z",
}
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


def make_client(handler):
    settings = Settings(
        app_env="test",
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        _env_file=None,
    )
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


@pytest.mark.parametrize("demo", [False, True])
def test_auth_routes_and_models_are_removed_without_calling_provider(demo):
    def no_network(request):
        pytest.fail("Removed endpoints must never call Supabase")

    client = (
        TestClient(
            create_app(
                Settings(app_env="test", demo_mode=True, _env_file=None),
                httpx.MockTransport(no_network),
            )
        )
        if demo
        else make_client(no_network)
    )
    with client:
        schema = client.get("/openapi.json").json()
        for method, path in REMOVED_ROUTES:
            response = client.request(method, path, json={"password": "private-password"})
            assert response.status_code == 404
            assert "private-password" not in response.text
            assert path not in schema["paths"]
        assert (
            not {
                "Credentials",
                "Register",
                "EmailRequest",
                "RefreshRequest",
                "VerifyRequest",
                "PasswordRequest",
                "Session",
                "Message",
            }
            & schema["components"]["schemas"].keys()
        )
        # The separate guardian-approval workflow is preserved.
        assert "/auth/registration/learners" in schema["paths"]


def test_data_identity_is_verified_per_request_and_owners_remain_isolated():
    calls = []
    revoked = set()
    other_user = USER | {"id": "00000000-0000-4000-8000-000000000002"}

    def upstream(req):
        assert req.method == "GET" and req.url.path == "/auth/v1/user"
        assert req.headers["apikey"] == "sb_publishable_test"
        assert not req.content
        token = req.headers["authorization"]
        calls.append(token)
        if token in revoked or token not in ("Bearer first", "Bearer second"):
            return httpx.Response(401, json={"message": "raw sensitive error"})
        return httpx.Response(200, json=USER if token == "Bearer first" else other_user)

    with make_client(upstream) as client:
        first = {"Authorization": "Bearer first"}
        second = {"Authorization": "Bearer second"}
        assert client.put(PROFILE, headers=first, json={"display_name": "First"}).status_code == 200
        assert client.get(PROFILE, headers=second).json()["display_name"] == "Learner"
        assert client.get(PROFILE, headers=first).json()["display_name"] == "First"
        revoked.add("Bearer first")
        assert client.get(PROFILE, headers=first).status_code == 401
        assert client.get(PROFILE, headers=second).status_code == 200
        assert client.get(PROFILE).status_code == 401
        assert calls == [
            "Bearer first",
            "Bearer second",
            "Bearer first",
            "Bearer first",
            "Bearer second",
        ]


@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer demo_parent"])
def test_missing_wrong_scheme_and_demo_identity_do_not_reach_supabase(authorization):
    with make_client(lambda req: pytest.fail("Should not call Supabase")) as client:
        response = client.get(
            PROFILE, headers={"Authorization": authorization} if authorization else {}
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    "status,expected", [(400, 401), (401, 401), (403, 401), (429, 429), (500, 503)]
)
def test_identity_errors_are_sanitized(status, expected):
    with make_client(
        lambda req: httpx.Response(status, json={"message": "private-debug-details"})
    ) as client:
        response = client.get(PROFILE, headers={"Authorization": "Bearer valid"})
        assert response.status_code == expected
        assert "private-debug-details" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_network_failure_becomes_retryable_error():
    def upstream(req):
        raise httpx.ConnectError("secret-host-info")

    with make_client(upstream) as client:
        response = client.get(PROFILE, headers={"Authorization": "Bearer valid"})
        assert response.status_code == 503
        assert "secret-host-info" not in response.text


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"id":"invalid-private-id"}'])
def test_invalid_provider_identity_is_rejected(body):
    with make_client(lambda req: httpx.Response(200, content=body)) as client:
        response = client.get(PROFILE, headers={"Authorization": "Bearer valid"})
        assert response.status_code == 502
        assert "invalid-private-id" not in response.text


def test_validation_redaction_remains_on_data_routes():
    with make_client(lambda req: httpx.Response(200, json=USER)) as client:
        response = client.put(
            PROFILE,
            headers={"Authorization": "Bearer valid"},
            json={"display_name": "Alex", "password": "private-password"},
        )
        assert response.status_code == 422
        assert "private-password" not in response.text
        assert '"input"' not in response.text


def test_cors_only_allows_configured_origin():
    with make_client(lambda req: httpx.Response(200, json=USER)) as client:
        for origin, allowed in [("http://localhost:3000", True), ("https://evil.example", False)]:
            response = client.options(
                PROFILE,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            assert (response.headers.get("access-control-allow-origin") == origin) is allowed


def test_secret_key_and_wildcard_origins_are_rejected():
    with pytest.raises(ValueError):
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_publishable_key="sb_secret_no",
            _env_file=None,
        )
    with pytest.raises(ValueError):
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_publishable_key="sb_publishable_test",
            cors_origins=["*"],
            _env_file=None,
        )
