import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.clerk_auth import ClerkVerifier, player_uuid
from app.config import Settings
from app.main import create_app

ISSUER = "https://synthetic.clerk.accounts.dev"
ORIGIN = "http://localhost:3000"


@pytest.fixture(scope="module")
def rsa_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def config(public=None, **changes):
    return Settings(
        app_env="test",
        clerk_issuer=ISSUER,
        clerk_authorized_parties=[ORIGIN],
        clerk_jwt_key=public,
        _env_file=None,
        **changes,
    )


def signed(private, **changes):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user_Alice123",
        "sid": "sess_Alice123",
        "azp": ORIGIN,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 60,
        "v": 2,
    } | changes
    return "Bearer " + jwt.encode(
        claims, private, algorithm="RS256", headers={"kid": "synthetic-key"}
    )


def test_clerk_maps_stable_player_and_ignores_forged_player_header(rsa_pair, repository):
    private, public = rsa_pair
    with TestClient(create_app(config(public), repository=repository)) as client:
        alice = {"Authorization": signed(private), "X-Player-ID": "not-even-a-uuid"}
        bob = {"Authorization": signed(private, sub="user_Bob123")}
        result = client.get("/api/me/player", headers=alice)
        assert result.status_code == 200, result.text
        assert result.json() == {
            "player_id": player_uuid(ISSUER, "user_Alice123"),
            "provider": "clerk",
        }
        assert (
            client.put("/api/me/profile", json={"display_name": "Alice"}, headers=alice).status_code
            == 200
        )
        assert client.get("/api/me/profile", headers=bob).json()["display_name"] == "Learner"
        assert client.get("/api/me/profile").status_code == 401
        assert (
            client.get(
                "/api/me/profile", headers={"X-Player-ID": result.json()["player_id"]}
            ).status_code
            == 401
        )
        assert client.get("/health").status_code == 200
        spec = client.get("/openapi.json").json()
        for path, methods in spec["paths"].items():
            if path.startswith("/api/") and not path.startswith("/api/registration/"):
                for operation in methods.values():
                    assert operation.get("security") == [{"ClerkSession": []}], path
        assert all(
            not p.get("name") == "X-Player-ID"
            for p in spec["paths"]["/api/me/profile"]["get"].get("parameters", [])
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.example"},
        {"sub": "user_../../other"},
        {"sid": "m2m_token"},
        {"azp": "https://evil.example"},
        {"azp": None},
        {"exp": 1},
        {"nbf": 9999999999},
        {"iat": 9999999999},
        {"exp": True},
        {"sts": "pending"},
        {"sts": "unknown"},
        {"v": 3},
        {"v": True},
        {"aud": "unconfigured-audience"},
    ],
)
def test_untrusted_claims_are_rejected_without_echo(rsa_pair, changes):
    private, public = rsa_pair
    verifier = ClerkVerifier(config(public))
    with pytest.raises(HTTPException) as error:
        verifier.verify(signed(private, **changes))
    assert error.value.status_code == 401
    assert "Invalid or expired" in error.value.detail


def test_missing_claims_wrong_signature_algorithms_and_audience(rsa_pair):
    private, public = rsa_pair
    verifier = ClerkVerifier(config(public))
    raw = signed(private).split(" ", 1)[1]
    payload = jwt.decode(raw, options={"verify_signature": False})
    for claim in ["iss", "sub", "sid", "exp", "nbf", "iat", "azp"]:
        token = jwt.encode(
            {k: v for k, v in payload.items() if k != claim},
            private,
            algorithm="RS256",
            headers={"kid": "synthetic-key"},
        )
        with pytest.raises(HTTPException):
            verifier.verify("Bearer " + token)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for value in [
        None,
        "Basic invalid",
        "Bearer bad-token",
        signed(wrong),
        "Bearer " + "x" * 17000,
        "Bearer "
        + jwt.encode(
            payload,
            "synthetic-hmac-key-with-at-least-32-bytes",
            algorithm="HS256",
            headers={"kid": "synthetic-key"},
        ),
    ]:
        with pytest.raises(HTTPException) as error:
            verifier.verify(value)
        assert error.value.status_code == 401
    configured = ClerkVerifier(config(public, clerk_audience="learning-api"))
    assert configured.verify(signed(private, aud="learning-api"))["subject"] == payload["sub"]
    with pytest.raises(HTTPException):
        configured.verify(signed(private, aud="another"))


def test_jwks_single_flight_cache_rotation_and_outage(rsa_pair):
    private, _ = rsa_pair
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {
        "kid": "synthetic-key",
        "alg": "RS256",
        "use": "sig",
    }
    calls, clock, broken = [], [10.0], [False]

    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) == ISSUER + "/.well-known/jwks.json"
        return (
            httpx.Response(503, text="secret upstream path")
            if broken[0]
            else httpx.Response(200, json={"keys": [key]})
        )

    verifier = ClerkVerifier(
        config(), transport=httpx.MockTransport(handler), clock=lambda: clock[0]
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(lambda _: verifier.verify(signed(private)), range(8)))
        assert all(v["subject"] == "user_Alice123" for v in out) and len(calls) == 1
        clock[0] += 11
        key["kid"] = "rotated-key"
        raw = signed(private).split(" ", 1)[1]
        payload = jwt.decode(raw, options={"verify_signature": False})
        rotated = "Bearer " + jwt.encode(
            payload, private, algorithm="RS256", headers={"kid": "rotated-key"}
        )
        assert verifier.verify(rotated)["subject"] == payload["sub"] and len(calls) == 2
        with pytest.raises(HTTPException):
            verifier.verify(signed(private))
        assert len(calls) == 2
        broken[0] = True
        clock[0] += 301
        with pytest.raises(HTTPException) as error:
            verifier.verify(rotated)
        assert error.value.status_code == 503 and "secret" not in error.value.detail
    finally:
        verifier.close()


@pytest.mark.parametrize(
    "payload",
    [{"keys": []}, {"keys": [None]}, {"keys": [{"kid": "x", "kty": "RSA"}]}, {"wrong": "format"}],
)
def test_invalid_jwks_fail_closed(rsa_pair, payload):
    verifier = ClerkVerifier(
        config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(HTTPException) as error:
            verifier.verify(signed(rsa_pair[0]))
        assert error.value.status_code == 503
    finally:
        verifier.close()


def test_production_refuses_anonymous_startup_and_bad_configuration():
    class DurableFixture:
        durable = True

    with pytest.raises(RuntimeError, match="Production API requires"):
        with TestClient(create_app(Settings(_env_file=None), repository=DurableFixture())):
            pass
    for kwargs in [
        dict(clerk_issuer="http://evil.example"),
        dict(clerk_issuer=ISSUER),
        dict(clerk_issuer=ISSUER, clerk_authorized_parties=["https://app.example/path"]),
        dict(clerk_jwt_key="private-invalid-value"),
    ]:
        with pytest.raises(ValueError):
            Settings(app_env="test", _env_file=None, **kwargs)
    with pytest.raises(ValueError, match="RSA public key"):
        create_app(config("not-a-public-key"))


def test_jwt_header_urls_are_never_used_for_key_retrieval(rsa_pair):
    private, _ = rsa_pair
    calls = []
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {
        "kid": "synthetic-key"
    }

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [key]})

    verifier = ClerkVerifier(config(), transport=httpx.MockTransport(handler))
    try:
        original = signed(private).split(" ", 1)[1]
        claims = jwt.decode(original, options={"verify_signature": False})
        token = jwt.encode(
            claims,
            private,
            algorithm="RS256",
            headers={"kid": "synthetic-key", "jku": "https://attacker.invalid/keys"},
        )
        assert verifier.verify("Bearer " + token)["subject"] == claims["sub"]
        assert calls == [ISSUER + "/.well-known/jwks.json"]
    finally:
        verifier.close()


@pytest.mark.parametrize("failure", ["redirect", "oversize", "timeout", "duplicate", "weak"])
def test_jwks_transport_and_key_bounds_fail_closed(rsa_pair, failure):
    private, _ = rsa_pair
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {
        "kid": "synthetic-key"
    }
    if failure == "weak":
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key())) | {
            "kid": "synthetic-key"
        }

    def handler(request):
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.invalid/keys"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 1_048_577)
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream details")
        return httpx.Response(200, json={"keys": [key, key] if failure == "duplicate" else [key]})

    verifier = ClerkVerifier(config(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(HTTPException) as error:
            verifier.verify(signed(private))
        assert error.value.status_code == 503 and "private" not in error.value.detail
    finally:
        verifier.close()


def test_production_clerk_verification_with_real_postgres_and_cookie_rejection(
    repository, rsa_pair
):
    if not repository.durable:
        pytest.skip("Production application startup requires PostgreSQL")
    private, public = rsa_pair
    origin = "https://app.example"
    settings = Settings(
        app_env="production",
        clerk_issuer=ISSUER,
        clerk_jwt_key=public,
        clerk_authorized_parties=[origin],
        cors_origins=[origin],
        _env_file=None,
    )
    with TestClient(create_app(settings, repository=repository)) as client:
        token = signed(private, azp=origin)
        assert client.get("/api/me/player", headers={"Authorization": token}).status_code == 200
        assert (
            client.get(
                "/api/me/player", headers={"Cookie": "__session=" + token.split(" ", 1)[1]}
            ).status_code
            == 401
        )
        assert client.get("/ready").status_code == 200
