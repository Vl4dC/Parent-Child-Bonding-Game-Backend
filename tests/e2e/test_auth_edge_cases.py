"""Authentication and session edge cases, in demo mode and against a mocked Supabase upstream."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import ACCESS_TTL, MAX_SESSIONS
from app.main import create_app
from app.repository import MemoryRepository
from tests.e2e.helpers import CANARY
from tests.test_auth import USER, make_client
from tests.test_demo import auth, demo_app, login

PARENT_EMAIL = "parent@demo.example.com"
PARENT_PASSWORD = "DemoParent123!"


def raw_login(client, email=PARENT_EMAIL, password=PARENT_PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------- demo login
@pytest.mark.parametrize("email", ["PARENT@DEMO.EXAMPLE.COM", " parent@demo.example.com "])
def test_demo_login_normalises_the_email_address(email):
    with TestClient(demo_app()) as client:
        r = raw_login(client, email)
        assert r.status_code == 200 and r.json()["user"]["email"] == PARENT_EMAIL


@pytest.mark.parametrize(
    "email",
    [
        "parent+extra@demo.example.com",
        "parent@demo.example.com.evil.example",
        "pаrent@demo.example.com",  # Cyrillic а
        "parent@demo.example.co",
        "child@demo.example.com ".replace("child", "parentt"),
    ],
)
def test_lookalike_addresses_are_not_the_demo_accounts(email):
    with TestClient(demo_app()) as client:
        assert raw_login(client, email).status_code == 401


@pytest.mark.parametrize(
    "email", ["not-an-email", "", "a@b", "@demo.example.com", "x" * 300 + "@example.com"]
)
def test_malformed_addresses_are_422(email):
    with TestClient(demo_app()) as client:
        assert raw_login(client, email).status_code == 422


@pytest.mark.parametrize(
    "password",
    [
        "demoparent123!",
        "DemoParent123! ",
        " DemoParent123!",
        "DemoParent123",
        "DemoParent123!\u0000",
        "DemoParent123!" + "x" * 100,
        "x" * 128,
    ],
)
def test_passwords_must_match_exactly(password):
    with TestClient(demo_app()) as client:
        r = raw_login(client, password=password)
        assert r.status_code == 401 and password not in r.text


def test_password_length_bounds_are_validation_errors():
    with TestClient(demo_app()) as client:
        assert raw_login(client, password="").status_code == 422
        assert raw_login(client, password="x" * 129).status_code == 422


def test_unknown_user_and_wrong_password_are_indistinguishable():
    with TestClient(demo_app()) as client:
        wrong = raw_login(client, password="nope-nope")
        unknown = raw_login(client, email="nobody@example.com", password="nope-nope")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()
        assert wrong.headers["cache-control"] == unknown.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {"email": PARENT_EMAIL, "password": PARENT_PASSWORD, "remember": True},
        {"email": PARENT_EMAIL, "password": None},
        {"email": PARENT_EMAIL},
        {"password": PARENT_PASSWORD},
        {"email": [PARENT_EMAIL], "password": PARENT_PASSWORD},
        {"email": PARENT_EMAIL, "password": 12345678},
        [],
    ],
)
def test_login_bodies_must_be_exactly_email_and_password(body):
    with TestClient(demo_app()) as client:
        r = client.post("/auth/login", json=body)
        assert r.status_code == 422 and PARENT_PASSWORD not in r.text


# ---------------------------------------------------------------- demo tokens
def test_tokens_are_not_interchangeable_or_tamperable():
    with TestClient(demo_app()) as client:
        session = login(client).json()
        access, refresh = session["access_token"], session["refresh_token"]
        for bad in [
            refresh,
            access[:-1],
            access + "x",
            access.upper(),
            access.replace("demo_", ""),
        ]:
            assert (
                client.get("/auth/me", headers={"Authorization": f"Bearer {bad}"}).status_code
                == 401
            )
        for bad in [access, " " + refresh, refresh + " ", refresh[:-1], refresh.upper()]:
            assert client.post("/auth/refresh", json={"refresh_token": bad}).status_code == 401
        assert (
            client.get("/auth/me", headers=auth(session)).status_code == 200
        )  # nothing above revoked it


def test_refresh_token_length_bounds():
    with TestClient(demo_app()) as client:
        assert client.post("/auth/refresh", json={"refresh_token": "a" * 4096}).status_code == 401
        assert client.post("/auth/refresh", json={"refresh_token": "a" * 4097}).status_code == 422
        assert client.post("/auth/refresh", json={"refresh_token": ""}).status_code == 422


def test_every_login_is_an_independent_session():
    with TestClient(demo_app()) as client:
        a, b, c = (login(client).json() for _ in range(3))
        assert len({a["access_token"], b["access_token"], c["access_token"]}) == 3
        assert client.post("/auth/logout", headers=auth(a)).status_code == 204
        assert client.get("/auth/me", headers=auth(a)).status_code == 401
        assert client.get("/auth/me", headers=auth(b)).status_code == 200
        assert (
            client.post("/auth/refresh", json={"refresh_token": c["refresh_token"]}).status_code
            == 200
        )


def test_concurrent_refreshes_of_one_token_have_a_single_winner():
    with TestClient(demo_app()) as client:
        refresh = login(client).json()["refresh_token"]

        def call(_):
            return client.post("/auth/refresh", json={"refresh_token": refresh}).status_code

        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(call, range(8)))
        assert sorted(codes) == [200] + [401] * 7


def test_logout_is_not_repeatable_and_kills_the_refresh_token():
    with TestClient(demo_app()) as client:
        session = login(client).json()
        assert client.post("/auth/logout", headers=auth(session)).status_code == 204
        assert client.post("/auth/logout", headers=auth(session)).status_code == 401
        assert (
            client.post(
                "/auth/refresh", json={"refresh_token": session["refresh_token"]}
            ).status_code
            == 401
        )


def test_logout_needs_a_live_access_token_so_an_expired_one_cannot_revoke(monkeypatch):
    """Known limit (documented in the report): a refresh token outlives a lapsed access token's logout."""
    now = [1000]
    monkeypatch.setattr("app.demo.time.time", lambda: now[0])
    with TestClient(demo_app()) as client:
        session = login(client).json()
        now[0] += ACCESS_TTL
        assert client.post("/auth/logout", headers=auth(session)).status_code == 401
        assert (
            client.post(
                "/auth/refresh", json={"refresh_token": session["refresh_token"]}
            ).status_code
            == 200
        )


def test_session_cap_returns_429_and_logout_frees_a_slot(monkeypatch):
    with TestClient(demo_app()) as client:
        demo = client.app.state.demo_auth
        sessions = [demo.login(PARENT_EMAIL, PARENT_PASSWORD) for _ in range(MAX_SESSIONS)]
        assert raw_login(client).status_code == 429
        assert (
            client.post(
                "/auth/refresh", json={"refresh_token": sessions[0]["refresh_token"]}
            ).status_code
            == 200
        )
        assert client.post("/auth/logout", headers=auth(sessions[1])).status_code == 204
        assert raw_login(client).status_code == 200
        assert raw_login(client).status_code == 429


def test_expired_sessions_are_pruned_so_the_cap_recovers(monkeypatch):
    now = [1000]
    monkeypatch.setattr("app.demo.time.time", lambda: now[0])
    with TestClient(demo_app()) as client:
        demo = client.app.state.demo_auth
        for _ in range(MAX_SESSIONS):
            demo.login(PARENT_EMAIL, PARENT_PASSWORD)
        assert raw_login(client).status_code == 429
        now[0] += 86400
        assert raw_login(client).status_code == 200


def test_me_exposes_only_the_documented_user_fields():
    with TestClient(demo_app()) as client:
        user = client.get("/auth/me", headers=auth(login(client).json())).json()
        assert set(user) == {"id", "email", "email_confirmed_at", "user_metadata"}
        assert user["user_metadata"] == {
            "display_name": "Alex (Demo Parent)",
            "account_type": "parent",
            "demo": True,
            "demo_family_id": "demo-family",
        }
        assert PARENT_PASSWORD not in json.dumps(user)


def test_invalid_input_beats_demo_unavailability_but_valid_input_is_409():
    with TestClient(demo_app()) as client:
        session = login(client).json()
        bad = {"email": "x", "password": "short", "display_name": " ", "account_type": "admin"}
        assert client.post("/auth/register", json=bad).status_code == 422
        assert (
            client.post("/auth/verify", json={"type": "magiclink", "token_hash": "x"}).status_code
            == 422
        )
        assert client.post("/auth/forgot-password", json={"email": "nope"}).status_code == 422
        assert (
            client.put(
                "/auth/password", json={"password": "short"}, headers=auth(session)
            ).status_code
            == 422
        )
        assert (
            client.put("/auth/password", json={"password": "long-enough-password"}).status_code
            == 401
        )


def test_demo_routes_never_reach_the_network_whatever_is_sent():
    with TestClient(demo_app()) as client:  # demo_app() fails the test if its transport is used
        for path, body in [
            ("/auth/verify", {"type": "signup", "email": "a@b.co", "token": "123456"}),
            ("/auth/resend", {"email": "a@b.co"}),
            ("/auth/forgot-password", {"email": "a@b.co"}),
        ]:
            assert client.post(path, json=body).status_code == 409


def test_demo_mode_cannot_be_switched_on_outside_development_or_test(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    for env in ("production", "staging", ""):
        monkeypatch.setenv("APP_ENV", env)
        with pytest.raises(ValueError):
            Settings(_env_file=None)
    monkeypatch.delenv("APP_ENV")
    with pytest.raises(ValueError, match="DEMO_MODE is allowed only"):
        Settings(_env_file=None)  # APP_ENV defaults to production
    monkeypatch.setenv("APP_ENV", "development")
    assert Settings(_env_file=None).demo_mode is True


def test_demo_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
    assert Settings(_env_file=None).demo_mode is False


# ---------------------------------------------------------------- secrets stay out of logs
def test_credentials_are_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(demo_app()) as client:
        assert raw_login(client, password=CANARY).status_code == 401
        assert raw_login(client, password=CANARY * 20).status_code == 422
        assert client.post("/auth/refresh", json={"refresh_token": CANARY}).status_code == 401
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {CANARY}"}).status_code == 401
        )
    assert CANARY not in caplog.text


# ---------------------------------------------------------------- Supabase mode: input rules
def register_body(**changes):
    body = {
        "email": USER["email"],
        "password": "test-password",
        "display_name": "Sam",
        "account_type": "learner",
    }
    return {**body, **changes}


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "a" * 61},
        {"password": "1234567"},
        {"password": "x" * 129},
        {"account_type": "admin"},
        {"account_type": "Parent"},
        {"account_type": "parent "},
        {"account_type": None},
        {"role": "service_role"},
        {"data": {"role": "admin"}},
        {"email": "x" * 255 + "@example.com"},
    ],
)
def test_registration_rejects_bad_or_privileged_input_before_calling_supabase(changes):
    body = register_body(**changes)
    with make_client(lambda req: pytest.fail("Supabase must not be called")) as client:
        r = client.post("/auth/register", json=body)
        assert r.status_code == 422 and "test-password" not in r.text


def test_registration_forwards_only_trimmed_display_name_and_account_type():
    seen = []

    def upstream(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"user": USER})

    with make_client(upstream) as client:
        r = client.post(
            "/auth/register",
            json=register_body(display_name="  Sam  ", email="Sam <player@example.com>"),
        )
        assert r.status_code == 202
    assert seen[0]["data"] == {"display_name": "Sam", "account_type": "learner"}
    assert seen[0]["email"] == "player@example.com"
    assert set(seen[0]) == {"email", "password", "data"}


def test_new_and_existing_addresses_get_the_same_registration_answer():
    outcomes = [
        httpx.Response(200, json={"user": USER}),
        httpx.Response(400, json={"error_code": "user_already_exists"}),
        httpx.Response(422, json={"error_code": "email_exists"}),
    ]
    answers = []
    for response in outcomes:
        with make_client(lambda req, response=response: response) as client:
            r = client.post("/auth/register", json=register_body())
            answers.append((r.status_code, r.json()))
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "signup"},
        {"type": "signup", "email": USER["email"]},
        {"type": "signup", "token": "123456"},
        {"type": "signup", "email": USER["email"], "token": "12345"},
        {"type": "signup", "email": USER["email"], "token": "12345678901"},
        {"type": "magiclink", "token_hash": CANARY},
        {"type": "recovery", "token_hash": ""},
        {"type": "recovery", "token_hash": CANARY * 30},
        {"type": "recovery", "token_hash": CANARY, "email": USER["email"]},
        {"type": "recovery", "token_hash": CANARY, "token": "123456"},
    ],
)
def test_verification_requires_exactly_one_well_formed_method(payload):
    with make_client(lambda req: pytest.fail("Supabase must not be called")) as client:
        r = client.post("/auth/verify", json=payload)
        assert r.status_code == 422 and CANARY not in r.text


def test_email_requests_never_reveal_whether_an_account_exists():
    answers = []
    for response in [
        httpx.Response(200, json={}),
        httpx.Response(400, json={"error_code": "user_not_found"}),
        httpx.Response(404, json={"error_code": "user_not_found"}),
    ]:
        with make_client(lambda req, response=response: response) as client:
            answers.append(
                [
                    client.post(p, json={"email": USER["email"]})
                    for p in ("/auth/forgot-password", "/auth/resend")
                ]
            )
    flat = [(r.status_code, r.text) for group in answers for r in group]
    assert len(set(flat[:2])) == 2  # the two endpoints word their message differently...
    assert (
        flat[0:2] == flat[2:4] == flat[4:6]
    )  # ...but each answer is identical for every account state


def test_password_policy_and_upstream_refusals_are_sanitised():
    def upstream(req):
        if req.method == "PUT":
            return httpx.Response(422, json={"error_code": "weak_password", "msg": CANARY})
        return httpx.Response(200, json=USER)

    with make_client(upstream) as client:
        assert (
            client.put(
                "/auth/password",
                json={"password": "1234567"},
                headers={"Authorization": "Bearer ok"},
            ).status_code
            == 422
        )
        r = client.put(
            "/auth/password",
            json={"password": "long-enough-password"},
            headers={"Authorization": "Bearer ok"},
        )
        assert (
            r.status_code == 400 and CANARY not in r.text and "long-enough-password" not in r.text
        )


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, {"msg": CANARY}, 401),
        (422, {"error_code": "validation_failed", "msg": CANARY}, 401),
        (403, {"error_code": "something_else"}, 401),
        (502, {}, 503),
        (504, {}, 503),
        (429, {"error_code": "over_email_send_rate_limit"}, 429),
    ],
)
def test_unusual_upstream_statuses_map_to_sanitised_answers(status, body, expected):
    with make_client(lambda req: httpx.Response(status, json=body)) as client:
        r = client.post("/auth/login", json={"email": USER["email"], "password": "test-password"})
        assert (
            r.status_code == expected
            and CANARY not in r.text
            and r.headers["cache-control"] == "no-store"
        )


def test_upstream_timeouts_and_resets_are_retryable_503s():
    for error in (
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("reset"),
    ):

        def upstream(req, error=error):
            raise error

        with make_client(upstream) as client:
            assert (
                client.post(
                    "/auth/login", json={"email": USER["email"], "password": "test-password"}
                ).status_code
                == 503
            )
            assert (
                client.get("/api/me/profile", headers={"Authorization": "Bearer t"}).status_code
                == 503
            )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 5}),
    ],
    ids=["empty-body", "json-list", "missing-user"],
)
@pytest.mark.xfail(
    strict=True, reason="BUG-14: a malformed 200 from Supabase becomes an unhandled 500"
)
@pytest.mark.parametrize(
    "path,body",
    [
        ("/auth/login", {"email": USER["email"], "password": "test-password"}),
        ("/auth/refresh", {"refresh_token": "r"}),
    ],
)
def test_a_malformed_upstream_session_is_a_502_not_a_crash(response, path, body):
    app = create_app(
        Settings(
            app_env="test",
            supabase_url="https://example.supabase.co",
            supabase_publishable_key="sb_publishable_test",
            _env_file=None,
        ),
        httpx.MockTransport(lambda req: response),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(path, json=body)
        assert r.status_code == 502 and r.headers.get("cache-control") == "no-store"


@pytest.mark.xfail(
    strict=True, reason="BUG-14: a malformed 200 from Supabase becomes an unhandled 500"
)
def test_a_malformed_upstream_user_is_a_502_on_auth_me():
    app = create_app(
        Settings(
            app_env="test",
            supabase_url="https://example.supabase.co",
            supabase_publishable_key="sb_publishable_test",
            _env_file=None,
        ),
        httpx.MockTransport(lambda req: httpx.Response(200, json={})),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/auth/me", headers={"Authorization": "Bearer t"}).status_code == 502


# ---------------------------------------------------------------- Supabase mode: identity for /api
def user_for(token):
    return {**USER, "id": f"00000000-0000-4000-8000-{int(token.removeprefix('user-')):012d}"}


def tokens_handler(req):
    """A provider that only knows the tokens `user-<number>`; anything else is a bad JWT."""
    token = req.headers.get("authorization", "").removeprefix("Bearer ")
    if re.fullmatch(r"user-\d+", token):
        return httpx.Response(200, json=user_for(token))
    return httpx.Response(401, json={"error_code": "bad_jwt"})


def test_identity_is_always_verified_upstream_never_decoded_locally():
    forged = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTQwMDAtODAwMC0wMDAwMDAwMDAwMDEiLCJyb2xlIjoic2VydmljZV9yb2xlIn0."
    seen = []

    def upstream(req):
        seen.append(req.headers["authorization"])
        return tokens_handler(req)

    with make_client(upstream) as client:
        for token in [forged, "user-1.tampered", "USER-1", "user-1x"]:
            r = client.get("/api/me/profile", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401, token
    assert seen and f"Bearer {forged}" in seen  # the verdict came from the provider


def test_demo_prefixed_tokens_never_reach_the_provider_or_the_data_routes():
    with make_client(lambda req: pytest.fail("A demo token must be rejected locally")) as client:
        for path in ["/api/me/profile", "/api/resources/lessons", "/auth/me"]:
            assert (
                client.get(
                    path, headers={"Authorization": "Bearer demo_access_whatever"}
                ).status_code
                == 401
            )


def test_two_provider_users_never_see_each_others_data():
    with make_client(tokens_handler) as client:
        one, two = ({"Authorization": f"Bearer user-{n}"} for n in (1, 2))
        assert (
            client.put("/api/me/profile", json={"display_name": "One"}, headers=one).status_code
            == 200
        )
        assert client.get("/api/me/profile", headers=two).json()["display_name"] == "Learner"
        assert (
            client.put("/api/me/profile", json={"display_name": "Two"}, headers=two).status_code
            == 200
        )
        assert client.get("/api/me/profile", headers=one).json()["display_name"] == "One"


def test_concurrent_requests_with_different_tokens_never_share_a_session():
    with make_client(tokens_handler) as client:

        def whoami(n):
            r = client.get("/auth/me", headers={"Authorization": f"Bearer user-{n}"})
            return n, r.status_code, r.json().get("id")

        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(whoami, [n for n in range(1, 41)]))
        assert all(
            status == 200 and uid == user_for(f"user-{n}")["id"] for n, status, uid in results
        )


@pytest.mark.parametrize(
    "raw_id", [USER["id"].upper(), "{" + USER["id"] + "}", USER["id"].replace("-", "")]
)
def test_equivalent_spellings_of_one_user_id_are_one_owner(raw_id):
    def upstream(req):
        if req.headers["authorization"] == "Bearer spelled":
            return httpx.Response(200, json={**USER, "id": raw_id})
        return httpx.Response(200, json=USER)

    with make_client(upstream) as client:
        assert (
            client.put(
                "/api/me/profile",
                json={"display_name": "Same"},
                headers={"Authorization": "Bearer spelled"},
            ).status_code
            == 200
        )
        assert (
            client.get("/api/me/profile", headers={"Authorization": "Bearer canonical"}).json()[
                "display_name"
            ]
            == "Same"
        )


@pytest.mark.parametrize(
    "identity",
    [{"id": "admin"}, {"id": ""}, {"id": 5}, {"id": None}, {"email": "x@example.com"}, {}],
)
def test_an_unusable_provider_identity_is_a_502_and_touches_no_data(identity):
    with make_client(lambda req: httpx.Response(200, json=identity)) as client:
        r = client.put(
            "/api/me/profile", json={"display_name": "Nope"}, headers={"Authorization": "Bearer t"}
        )
        assert r.status_code == 502


def test_account_type_metadata_grants_no_access():
    def upstream(req):
        number = 1 if req.headers["authorization"].endswith("1") else 2
        metadata = {
            "account_type": "parent",
            "role": "admin",
            "demo_family_id": "same-family",
            "is_admin": True,
        }
        return httpx.Response(200, json={**user_for(f"user-{number}"), "user_metadata": metadata})

    with make_client(upstream) as client:
        one, two = ({"Authorization": f"Bearer t{n}"} for n in (1, 2))
        client.put("/api/me/profile", json={"display_name": "Private"}, headers=one)
        assert client.get("/api/me/profile", headers=two).json()["display_name"] == "Learner"
        assert client.get("/api/resources/lessons", headers=two).json()["items"] == []


def test_the_provider_never_receives_the_other_users_token_or_a_service_key():
    seen = []

    def upstream(req):
        seen.append((req.headers.get("authorization"), req.headers.get("apikey")))
        return tokens_handler(req)

    with make_client(upstream) as client:
        client.get("/api/me/profile", headers={"Authorization": "Bearer user-7"})
        client.get("/api/me/profile")
    assert seen == [("Bearer user-7", "sb_publishable_test")]


# ---------------------------------------------------------------- configuration safety
@pytest.mark.parametrize(
    "key",
    [
        "sb_secret_abcdefghijklmnop",
        "",
        " sb_publishable_x",
        "SB_PUBLISHABLE_x",
        "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig",
        "service_role",
    ],
)
def test_only_publishable_keys_are_accepted(key):
    with pytest.raises(ValueError) as error:
        Settings(
            supabase_url="https://example.supabase.co", supabase_publishable_key=key, _env_file=None
        )
    assert key not in str(error.value) or key == ""


@pytest.mark.parametrize("origins", [["*"], ["http://localhost:3000", "*"]])
def test_wildcard_origins_are_refused(origins):
    with pytest.raises(ValueError):
        Settings(demo_mode=True, app_env="test", cors_origins=origins, _env_file=None)


@pytest.mark.xfail(
    strict=True, reason="BUG-12: the opaque 'null' origin is accepted as an 'exact' origin"
)
def test_the_null_origin_is_not_an_exact_origin():
    with pytest.raises(ValueError):
        Settings(demo_mode=True, app_env="test", cors_origins=["null"], _env_file=None)


def test_secret_settings_are_masked_in_repr_and_errors():
    secret_db = "postgresql://svc:TOPSECRETPASSWORD@db.example.com/postgres"
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_VISIBLE",
        database_url=secret_db,
        _env_file=None,
    )
    assert "TOPSECRETPASSWORD" not in repr(settings) and "sb_publishable_VISIBLE" not in repr(
        settings
    )
    with pytest.raises(ValueError) as error:
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_publishable_key="sb_publishable_VISIBLE",
            database_url=secret_db,
            database_pool_size=0,
            _env_file=None,
        )
    assert "TOPSECRETPASSWORD" not in str(error.value) and "sb_publishable_VISIBLE" not in str(
        error.value
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_requests_per_minute", 0),
        ("generation_concurrency", 9),
        ("generation_daily_limit", 0),
        ("job_attempt_limit", 6),
        ("model_timeout_seconds", 301),
        ("database_pool_size", 65),
        ("app_env", "staging"),
    ],
)
def test_resource_limits_have_sane_bounds(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{"demo_mode": True, "app_env": "test", field: value})


def test_production_refuses_to_start_without_durable_storage():
    prod = Settings(
        app_env="production",
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        with TestClient(create_app(prod)):
            pass
    with pytest.raises(RuntimeError, match="Production requires PostgreSQL"):
        with TestClient(create_app(prod, repository=MemoryRepository())):
            pass


def test_development_without_a_database_or_demo_refuses_to_start():
    dev = Settings(
        app_env="development",
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test",
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        with TestClient(create_app(dev)):
            pass


def test_example_environment_files_hold_placeholders_only():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    live = (root / ".env.example").read_text()
    demo = (root / ".env.demo.example").read_text()
    for text in (live, demo):
        assert "sb_secret_" not in text and "service_role" not in text and "eyJ" not in text
        assert "sk-ant-" not in text and "BEGIN PRIVATE KEY" not in text
    assert "DEMO_MODE=true" not in live
    assert "DEMO_MODE=true" in demo and "APP_ENV=development" in demo
    assert "GENERATION_ENABLED=true" not in live and "GENERATION_ENABLED=true" not in demo
    for line in live.splitlines():
        if line.startswith("DATABASE_URL=") and line.split("=", 1)[1].strip():
            assert any(
                word in line.lower() for word in ("replace", "placeholder", "example", "your")
            )
