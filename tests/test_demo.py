import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import ACCESS_TTL, DEMO_ACCOUNTS, REFRESH_TTL
from app.main import create_app


def demo_app():
    def no_network(request):
        pytest.fail("Demo mode must never call Supabase")

    settings = Settings(app_env="test", demo_mode=True, _env_file=None)
    return create_app(settings, httpx.MockTransport(no_network))


def login(client, account="parent"):
    email = f"{account}@demo.example.com"
    return client.post(
        "/auth/login", json={"email": email, "password": DEMO_ACCOUNTS[email]["password"]}
    )


def auth(session):
    return {"Authorization": f"Bearer {session['access_token']}"}


@pytest.mark.parametrize("account,role", [("parent", "parent"), ("child", "learner")])
def test_seeded_accounts_login_and_identity_without_network(account, role):
    with TestClient(demo_app()) as client:
        assert client.get("/health").json()["auth_mode"] == "demo"
        response = login(client, account)
        assert response.status_code == 200
        session = response.json()
        assert session["user"]["user_metadata"]["account_type"] == role
        assert session["user"]["user_metadata"]["demo"] is True
        assert session["user"]["email_confirmed_at"] is None
        assert client.get("/auth/me", headers=auth(session)).json() == session["user"]
        assert response.headers["cache-control"] == "no-store"


def test_accounts_are_distinct_and_do_not_expose_passwords():
    with TestClient(demo_app()) as client:
        accounts = client.get("/auth/demo-accounts")
        assert len(accounts.json()) == 2
        assert "password" not in accounts.text
        parent, child = login(client).json(), login(client, "child").json()
        assert parent["user"]["id"] != child["user"]["id"]
        assert parent["access_token"] != child["access_token"]
        assert client.get("/auth/me", headers=auth(parent)).json()["id"] == parent["user"]["id"]


@pytest.mark.parametrize(
    "email,password",
    [
        ("parent@demo.example.com", "wrong"),
        ("someone@example.com", "DemoParent123!"),
    ],
)
def test_unknown_or_incorrect_credentials_are_rejected(email, password):
    with TestClient(demo_app()) as client:
        response = client.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401
        assert password not in response.text


def test_forged_tokens_and_missing_credentials_are_rejected():
    with TestClient(demo_app()) as client:
        assert client.get("/auth/me").status_code == 401
        assert (
            client.get(
                "/auth/me", headers={"Authorization": "Bearer demo_access_forged"}
            ).status_code
            == 401
        )
        assert (
            client.get("/auth/me", headers={"Authorization": "Bearer supabase-token"}).status_code
            == 401
        )
        assert (
            client.post("/auth/refresh", json={"refresh_token": "demo_refresh_forged"}).status_code
            == 401
        )


def test_refresh_rotates_both_tokens_and_blocks_replay():
    with TestClient(demo_app()) as client:
        old = login(client).json()
        response = client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]})
        assert response.status_code == 200
        new = response.json()
        assert old["access_token"] != new["access_token"]
        assert old["refresh_token"] != new["refresh_token"]
        assert old["user"] == new["user"]
        assert client.get("/auth/me", headers=auth(old)).status_code == 401
        assert client.get("/auth/me", headers=auth(new)).status_code == 200
        assert (
            client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code
            == 401
        )


def test_logout_invalidates_access_and_refresh_but_not_other_account():
    with TestClient(demo_app()) as client:
        parent, child = login(client).json(), login(client, "child").json()
        assert client.post("/auth/logout", headers=auth(parent)).status_code == 204
        assert client.get("/auth/me", headers=auth(parent)).status_code == 401
        assert (
            client.post(
                "/auth/refresh", json={"refresh_token": parent["refresh_token"]}
            ).status_code
            == 401
        )
        assert client.get("/auth/me", headers=auth(child)).status_code == 200


def test_access_expiry_still_allows_refresh(monkeypatch):
    now = [1000]
    monkeypatch.setattr("app.demo.time.time", lambda: now[0])
    with TestClient(demo_app()) as client:
        old = login(client).json()
        now[0] += ACCESS_TTL
        assert client.get("/auth/me", headers=auth(old)).status_code == 401
        assert (
            client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code
            == 200
        )


def test_refresh_expiry_and_server_restart_invalidate_sessions(monkeypatch):
    now = [1000]
    monkeypatch.setattr("app.demo.time.time", lambda: now[0])
    with TestClient(demo_app()) as client:
        old = login(client).json()
        now[0] += REFRESH_TTL
        assert (
            client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code
            == 401
        )
        active = login(client).json()
    with TestClient(demo_app()) as client:
        assert client.get("/auth/me", headers=auth(active)).status_code == 401


def test_email_and_password_mutations_are_unavailable_in_demo():
    with TestClient(demo_app()) as client:
        session = login(client).json()
        cases = [
            (
                "post",
                "/auth/register",
                {
                    "email": "new@example.com",
                    "password": "password123",
                    "display_name": "New",
                    "account_type": "learner",
                },
            ),
            ("post", "/auth/forgot-password", {"email": "parent@demo.example.com"}),
            ("post", "/auth/resend", {"email": "parent@demo.example.com"}),
            ("post", "/auth/verify", {"type": "signup", "token_hash": "fake-hash"}),
            ("put", "/auth/password", {"password": "password123"}),
        ]
        for method, path, payload in cases:
            assert (
                getattr(client, method)(path, json=payload, headers=auth(session)).status_code
                == 409
            )


def test_demo_cannot_start_in_production_or_without_explicit_environment():
    for env in ("production", None):
        kwargs = {"app_env": env} if env else {}
        with pytest.raises(ValueError, match="DEMO_MODE is allowed only"):
            Settings(demo_mode=True, _env_file=None, **kwargs)


def test_supabase_mode_still_requires_configuration():
    with pytest.raises(ValueError, match="required when DEMO_MODE is off"):
        Settings(_env_file=None)


def test_supabase_mode_excludes_demo_routes_and_tokens():
    def no_network(request):
        pytest.fail("A demo token must be rejected before contacting Supabase")

    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        _env_file=None,
    )
    with TestClient(create_app(settings, httpx.MockTransport(no_network))) as client:
        assert client.get("/health").json()["auth_mode"] == "supabase"
        assert client.get("/auth/demo-accounts").status_code == 404
        assert (
            client.get("/auth/me", headers={"Authorization": "Bearer demo_access_any"}).status_code
            == 401
        )
        assert (
            client.post("/auth/refresh", json={"refresh_token": "demo_refresh_any"}).status_code
            == 401
        )
