import json

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
SESSION = {
    "access_token": "access",
    "refresh_token": "refresh",
    "expires_in": 3600,
    "token_type": "bearer",
    "user": USER,
}


def make_client(handler):
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        _env_file=None,
    )
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def test_register_sends_metadata_without_returning_session():
    def upstream(req):
        assert req.url.path == "/auth/v1/signup"
        body = json.loads(req.content)
        assert body["data"] == {"display_name": "Sam", "account_type": "learner"}
        assert body["password"] == "test-password"
        return httpx.Response(200, json={"user": USER})

    with make_client(upstream) as client:
        response = client.post(
            "/auth/register",
            json={
                "email": USER["email"],
                "password": "test-password",
                "display_name": "Sam",
                "account_type": "learner",
            },
        )
        assert response.status_code == 202
        assert "access_token" not in response.text


@pytest.mark.parametrize(
    "grant,payload,path",
    [
        ("password", {"email": USER["email"], "password": "test-password"}, "/auth/login"),
        ("refresh_token", {"refresh_token": "refresh"}, "/auth/refresh"),
    ],
)
def test_login_and_refresh_use_correct_grants(grant, payload, path):
    def upstream(req):
        assert req.url.params["grant_type"] == grant
        assert json.loads(req.content) == payload
        assert req.headers["apikey"] == "sb_publishable_test"
        return httpx.Response(200, json=SESSION)

    with make_client(upstream) as client:
        response = client.post(path, json=payload)
        assert response.status_code == 200
        assert response.json()["access_token"] == "access"
        assert response.headers["cache-control"] == "no-store"


def test_me_requires_supabase_verified_identity_and_does_not_mix_sessions():
    def upstream(req):
        token = req.headers["authorization"]
        if token != "Bearer valid":
            return httpx.Response(401, json={"message": "raw sensitive error"})
        return httpx.Response(200, json=USER)

    with make_client(upstream) as client:
        assert client.get("/auth/me").status_code == 401
        assert (
            client.get("/auth/me", headers={"Authorization": "Bearer valid"}).json()["id"]
            == USER["id"]
        )
        invalid = client.get("/auth/me", headers={"Authorization": "Bearer forged"})
        assert invalid.status_code == 401
        assert "raw sensitive" not in invalid.text
        assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"email": USER["email"], "token": "123456", "type": "signup"},
        {"token_hash": "hashed-code", "type": "recovery"},
    ],
)
def test_verify_code_or_hash(payload):
    def upstream(req):
        assert req.url.path.endswith("/verify")
        assert json.loads(req.content) == payload
        return httpx.Response(200, json=SESSION)

    with make_client(upstream) as client:
        assert client.post("/auth/verify", json=payload).status_code == 200


def test_ambiguous_verification_rejected_without_secret_echo():
    with make_client(lambda req: pytest.fail("Should not call Supabase")) as client:
        response = client.post(
            "/auth/verify",
            json={
                "email": USER["email"],
                "token": "123456",
                "token_hash": "secret-hash",
                "type": "signup",
            },
        )
        assert response.status_code == 422
        assert "secret-hash" not in response.text


@pytest.mark.parametrize("path", ["/auth/forgot-password", "/auth/resend"])
def test_email_requests_return_generic_messages(path):
    with make_client(lambda req: httpx.Response(200, json={})) as client:
        response = client.post(path, json={"email": USER["email"]})
        assert response.status_code == 202
        assert USER["email"] not in response.text


def test_password_update_requires_user_and_preserves_password_bytes():
    requests = []

    def upstream(req):
        requests.append(req.method)
        assert req.headers["authorization"] == "Bearer valid"
        if req.method == "PUT":
            assert json.loads(req.content)["password"] == " long-password "
        return httpx.Response(200, json=USER)

    with make_client(upstream) as client:
        assert client.put("/auth/password", json={"password": "new-password"}).status_code == 401
        response = client.put(
            "/auth/password",
            json={"password": " long-password "},
            headers={"Authorization": "Bearer valid"},
        )
        assert response.status_code == 200
        assert requests == ["GET", "PUT"]


def test_logout_revokes_current_session():
    def upstream(req):
        assert req.url.params["scope"] == "local"
        assert req.headers["authorization"] == "Bearer valid"
        return httpx.Response(204)

    with make_client(upstream) as client:
        response = client.post("/auth/logout", headers={"Authorization": "Bearer valid"})
        assert response.status_code == 204
        assert not response.content


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (400, "invalid_credentials", 401),
        (400, "email_not_confirmed", 403),
        (429, "over_request_rate_limit", 429),
        (500, "unexpected_failure", 503),
    ],
)
def test_upstream_errors_are_sanitized(status, code, expected):
    with make_client(
        lambda req: httpx.Response(
            status, json={"error_code": code, "message": "private-debug-details"}
        )
    ) as client:
        response = client.post("/auth/login", json={"email": USER["email"], "password": "password"})
        assert response.status_code == expected
        assert "private-debug-details" not in response.text


def test_network_failure_becomes_retryable_error():
    def upstream(req):
        raise httpx.ConnectError("secret-host-info")

    with make_client(upstream) as client:
        response = client.get("/auth/me", headers={"Authorization": "Bearer valid"})
        assert response.status_code == 503
        assert "secret-host-info" not in response.text


def test_validation_does_not_echo_password_or_accept_privilege_fields():
    with make_client(lambda req: pytest.fail("Should not call Supabase")) as client:
        response = client.post(
            "/auth/register",
            json={
                "email": USER["email"],
                "password": "xyz",
                "display_name": "Sam",
                "account_type": "admin",
            },
        )
        assert response.status_code == 422
        assert '"input"' not in response.text
        assert "xyz" not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_cors_only_allows_configured_origin():
    with make_client(lambda req: httpx.Response(200, json=USER)) as client:
        for origin, allowed in [("http://localhost:3000", True), ("https://evil.example", False)]:
            response = client.options(
                "/auth/me",
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


@pytest.mark.parametrize(
    "path,payload,code",
    [
        (
            "/auth/register",
            {
                "email": USER["email"],
                "password": "test-password",
                "display_name": "Sam",
                "account_type": "learner",
            },
            "user_already_exists",
        ),
        ("/auth/forgot-password", {"email": USER["email"]}, "user_not_found"),
    ],
)
def test_existing_or_missing_account_mail_requests_do_not_disclose_identity(path, payload, code):
    with make_client(lambda req: httpx.Response(400, json={"error_code": code})) as client:
        assert client.post(path, json=payload).status_code == 202


def test_expired_session_cannot_update_password():
    methods = []

    def upstream(req):
        methods.append(req.method)
        return httpx.Response(401, json={"error_code": "bad_jwt"})

    with make_client(upstream) as client:
        response = client.put(
            "/auth/password",
            json={"password": "test-password"},
            headers={"Authorization": "Bearer expired"},
        )
        assert response.status_code == 401
        assert methods == ["GET"]
