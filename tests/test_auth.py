"""The API serves only requests that carry a valid sign-in token (a Clerk session JWT), and the player comes
only from that token. Real RS256 verification runs against a local test key (tests/jwt_helpers.py)."""

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

import app.auth as auth
from app.config import Settings
from app.main import create_app
from tests import jwt_helpers as jh

PROFILE = "/api/me/profile"
ALEX = "00000000-0000-4000-8000-0000000000a1"
SAM = "00000000-0000-4000-8000-0000000000a2"
ANON = {
    "X-Test-Anonymous": "1"
}  # the test client adds a token for every request unless told not to


def bearer(value: str) -> dict:
    return {"Authorization": f"Bearer {value}"}


def settings(**changes) -> Settings:
    return Settings(app_env="test", _env_file=None, **changes)


@pytest.fixture
def client(monkeypatch):
    def no_outbound_client(*args, **kwargs):
        pytest.fail("Verifying a token must not open an outbound HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", no_outbound_client)
    with TestClient(create_app(settings())) as c:
        yield c


# ---------------------------------------------------------------- who gets in
def test_a_valid_token_is_served_and_the_player_is_the_tokens_player_id(client):
    assert client.put(
        PROFILE, headers=bearer(jh.token(ALEX)), json={"display_name": "Alex"}
    ).is_success
    assert client.get(PROFILE, headers=bearer(jh.token(ALEX))).json()["display_name"] == "Alex"
    assert client.get(PROFILE, headers=bearer(jh.token(SAM))).json()["display_name"] == "Learner"
    again = client.get(PROFILE, headers=bearer(jh.token(ALEX, sub="user_other")))
    assert (
        again.json()["display_name"] == "Alex"
    )  # the claim decides, not the account name or the token
    assert again.headers["cache-control"] == "no-store"


def test_player_id_header_is_ignored_entirely(client):
    client.put(PROFILE, headers=bearer(jh.token(ALEX)), json={"display_name": "Alex"})
    for header in (SAM, "not-a-uuid", ""):
        r = client.get(PROFILE, headers={**bearer(jh.token(ALEX)), "X-Player-ID": header})
        assert r.status_code == 200 and r.json()["display_name"] == "Alex", header
    no_token = client.get(PROFILE, headers={**ANON, "X-Player-ID": ALEX})
    assert no_token.status_code == 401  # the header alone gets nothing


def test_players_see_each_other_only_through_the_features_not_the_header(client):
    room = client.post("/api/rooms", headers=bearer(jh.token(ALEX)), json={"display_name": "Alex"})
    assert room.status_code == 201, room.text
    code = room.json()["code"]
    joined = client.post(
        "/api/rooms/join",
        headers=bearer(jh.token(SAM)),
        json={"code": code, "display_name": "Sam"},
    )
    assert joined.status_code == 200 and len(joined.json()["members"]) == 2
    stranger = client.get(
        f"/api/rooms/{room.json()['id']}", headers=bearer(jh.token(jh.DEFAULT_PLAYER))
    )
    assert stranger.status_code == 404


# ---------------------------------------------------------------- who is turned away
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic YWxleDpwdw=="},
        {"Authorization": "Bearer not.a.jwt"},
        {"Authorization": "Bearer " + "a" * 9000},
        {"Authorization": "Token abc"},
    ],
)
def test_missing_or_malformed_credentials_are_a_401(client, headers):
    r = client.get(PROFILE, headers={**ANON, **headers})
    assert r.status_code == 401, r.text
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["cache-control"] == "no-store"
    assert isinstance(r.json()["detail"], str)


def bad_tokens() -> dict[str, str]:
    now = int(time.time())
    public_key_pem = jh.KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    good = jh.token(ALEX)
    head, body, sig = good.split(".")
    other_body = jh.token(SAM).split(".")[1]
    return {
        "expired": jh.token(ALEX, expires_in=-60),
        "not yet valid": jh.token(ALEX, not_before_in=600),
        "wrong issuer": jh.token(ALEX, issuer="https://evil.example"),
        "no issuer": jh.token(ALEX, issuer=None),
        "no expiry": jh.token(ALEX, exp=...),
        "signed by another key": jh.token(ALEX, key=jh.OTHER_KEY),
        "unknown key id": jh.token(ALEX, kid="rotated-away"),
        "no key id": jh.token(ALEX, kid=None),
        "body swapped": f"{head}.{other_body}.{sig}",
        "alg none": jwt.encode(
            {"sub": "u", "iss": jh.ISSUER, "exp": now + 99, "player_id": ALEX},
            None,
            algorithm="none",
        ),
        "hs256 signed with the public key": forged_hs256(
            public_key_pem,
            {"sub": "u", "iss": jh.ISSUER, "exp": now + 99, "player_id": ALEX},
        ),
    }


def forged_hs256(secret: bytes, claims: dict) -> str:
    """The classic algorithm-confusion attack, built by hand because PyJWT will not create it."""

    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    signing_input = f"{part({'alg': 'HS256', 'typ': 'JWT', 'kid': jh.KID})}.{part(claims)}"
    mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


@pytest.mark.parametrize("why", list(bad_tokens()))
def test_a_token_that_does_not_verify_is_a_401_and_never_echoed(client, why):
    value = bad_tokens()[why]
    r = client.get(PROFILE, headers={**ANON, **bearer(value)})
    assert r.status_code == 401, (why, r.text)
    assert value not in r.text and "BEGIN" not in r.text
    assert r.headers["www-authenticate"] == "Bearer"


def test_an_expired_token_says_so_so_the_app_can_sign_in_again(client):
    r = client.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, expires_in=-60))})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


@pytest.mark.parametrize("player", [None, "", "user_2abc", "1234", "00000000-0000", 42, ["x"]])
def test_a_token_without_a_valid_player_id_claim_is_refused_with_the_fix(client, player):
    r = client.get(
        PROFILE, headers={**ANON, **bearer(jh.token(player))}
    )  # None leaves the claim out
    assert r.status_code == 401, r.text
    assert "player_id" in r.json()["detail"]


def test_the_player_id_is_normalised_so_case_does_not_split_a_players_data(client):
    client.put(PROFILE, headers=bearer(jh.token(ALEX)), json={"display_name": "Alex"})
    upper = jh.token(ALEX.upper())
    assert client.get(PROFILE, headers=bearer(upper)).json()["display_name"] == "Alex"


# ---------------------------------------------------------------- optional checks
def test_authorized_parties_limit_which_apps_tokens_are_accepted():
    app = create_app(settings(jwt_authorized_parties=["https://app.example"]))
    with TestClient(app) as c:
        ok = c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, azp="https://app.example"))})
        assert ok.status_code == 200
        for azp in ("https://evil.example", ...):
            r = c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, azp=azp))})
            assert r.status_code == 401, azp


def test_an_audience_is_only_checked_when_one_is_configured():
    with TestClient(
        create_app(settings())
    ) as c:  # a token with an audience is fine when none is required
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, aud="anything"))}).status_code
            == 200
        )
    with TestClient(create_app(settings(jwt_audience="api"))) as c:
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, aud="api"))}).status_code == 200
        )
        assert c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))}).status_code == 401
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, aud="other"))}).status_code
            == 401
        )


def test_a_little_clock_difference_is_tolerated_but_not_much():
    with TestClient(create_app(settings(jwt_leeway_seconds=5))) as c:
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, expires_in=-2))}).status_code
            == 200
        )
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, expires_in=-30))}).status_code
            == 401
        )


# ---------------------------------------------------------------- every route
def api_routes(app):
    """Every operation of the published schema under /api (what callers can see, whatever the router layout)."""
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api"):
            for method in operations:
                yield method.upper(), path


# Learner registration intake: a child signing up with a parent's email has no token yet, so these three work on a
# private receipt and per-address limits instead. They never touch a player's data, and the guardian routes next to
# them do need a token. Any other route that answers without one is a bug.
REGISTRATION_INTAKE = {
    ("POST", "/api/registration/learners"),
    ("GET", "/api/registration/requests/{request_id}"),
    ("POST", "/api/registration/requests/{request_id}/resend"),
}


def test_every_api_route_demands_a_token_except_the_registration_intake(client):
    routes = list(api_routes(client.app))
    assert len(routes) > 100, "the route list looks too short to be the whole API"
    open_routes = set()
    for method, path in routes:
        filled = path
        for part in path.split("/"):
            if part.startswith("{"):
                filled = filled.replace(part, "x")
        if client.request(method, filled, headers=ANON).status_code != 401:
            open_routes.add((method, path))
    assert open_routes == REGISTRATION_INTAKE


def test_health_docs_and_the_schema_stay_public(client):
    assert client.get("/health", headers=ANON).json() == {"status": "ok"}
    assert client.get("/ready", headers=ANON).status_code in (200, 503)
    assert client.get("/docs", headers=ANON).status_code == 200
    assert client.get("/openapi.json", headers=ANON).status_code == 200


def test_the_schema_offers_a_bearer_token_and_still_has_no_login_routes(client):
    schema = client.get("/openapi.json", headers=ANON).json()
    schemes = schema["components"]["securitySchemes"]
    assert [s["scheme"] for s in schemes.values()] == ["bearer"]
    op = schema["paths"][PROFILE]["get"]
    assert op["security"] and "X-Player-ID" not in json.dumps(schema)
    assert not any(p.startswith("/auth/") for p in schema["paths"])
    assert client.post("/auth/login", headers=ANON, json={}).status_code == 404


# ---------------------------------------------------------------- not configured, and key handling
def test_a_server_with_no_sign_in_settings_refuses_everything_but_health():
    with TestClient(create_app(settings(jwt_issuer=None, jwt_jwks_json=None))) as c:
        r = c.get(PROFILE, headers=bearer(jh.token(ALEX)))
        assert r.status_code == 503 and "JWT_ISSUER" in r.json()["detail"]
        assert c.get(PROFILE, headers=ANON).status_code == 503
        assert c.get("/health", headers=ANON).status_code == 200


def test_the_key_set_url_comes_from_the_issuer_unless_given():
    assert (
        settings(jwt_issuer="https://abc.clerk.accounts.dev/").jwks_url
        == "https://abc.clerk.accounts.dev/.well-known/jwks.json"
    )
    assert (
        settings(jwt_issuer="https://x", jwt_jwks_url="https://keys.example/k").jwks_url
        == "https://keys.example/k"
    )
    assert settings(jwt_issuer=None).jwks_url is None
    with pytest.raises(ValueError):
        Settings(app_env="production", jwt_issuer="http://insecure.example", _env_file=None)
    with pytest.raises(ValueError):
        settings(jwt_jwks_json="not json")
    with pytest.raises(ValueError):
        settings(jwt_jwks_json='{"keys": "nope"}')


class Keys:
    """A fetcher that serves whatever key set the test chooses and counts the calls."""

    def __init__(self, *pairs):
        self.pairs = pairs
        self.calls = 0
        self.fail = False

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("down")
        return json.loads(jh.jwks_json(*self.pairs))


def remote_app(fetch, now):
    verifier = auth.TokenVerifier(
        jh.ISSUER, auth.RemoteKeys(fetch, clock=now, refresh_after=30, unknown_key_wait=10)
    )
    return create_app(settings(), token_verifier=verifier)


def test_keys_are_fetched_once_and_cached():
    fetch, clock = Keys((jh.KEY, jh.KID)), lambda: 1000.0
    with TestClient(remote_app(fetch, clock)) as c:
        for _ in range(3):
            assert c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))}).status_code == 200
    assert fetch.calls == 1


def test_a_rotated_key_is_picked_up_but_unknown_keys_do_not_cause_a_fetch_storm():
    fetch, now = Keys((jh.KEY, jh.KID)), [1000.0]
    with TestClient(remote_app(fetch, lambda: now[0])) as c:
        assert c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))}).status_code == 200
        fresh = jh.token(ALEX, key=jh.OTHER_KEY, kid="new-key")
        now[0] += 60
        fetch.pairs = ((jh.KEY, jh.KID), (jh.OTHER_KEY, "new-key"))
        assert (
            c.get(PROFILE, headers={**ANON, **bearer(fresh)}).status_code == 200
        )  # refetched once
        calls = fetch.calls
        for _ in range(
            5
        ):  # a token with a key id nobody has: one refetch per wait, not one per request
            assert (
                c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX, kid="nobody"))}).status_code
                == 401
            )
        assert fetch.calls <= calls + 1


def test_when_the_keys_cannot_be_fetched_the_answer_is_503_not_401_and_old_keys_keep_working():
    fetch, now = Keys((jh.KEY, jh.KID)), [1000.0]
    with TestClient(remote_app(fetch, lambda: now[0])) as c:
        fetch.fail = True
        down = c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))})
        assert down.status_code == 503 and "keys" in down.json()["detail"]
        fetch.fail = False
        now[0] += 11  # the provider is not asked again at once after a failure
        assert c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))}).status_code == 200
        now[0] += 3600  # stale, and the provider is down again: the cached keys still verify
        fetch.fail = True
        assert c.get(PROFILE, headers={**ANON, **bearer(jh.token(ALEX))}).status_code == 200


# ---------------------------------------------------------------- unchanged behaviour
def test_invalid_values_are_still_redacted(client):
    response = client.put(
        PROFILE,
        headers=bearer(jh.token(ALEX)),
        json={"display_name": "Alex", "password": "private-password"},
    )
    assert response.status_code == 422
    assert "private-password" not in response.text and '"input"' not in response.text


def test_cors_allows_the_authorization_header_for_exact_origins(client):
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
    with pytest.raises(ValueError):
        Settings(cors_origins=["*"], _env_file=None)
