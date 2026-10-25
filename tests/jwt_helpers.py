"""Signing keys and tokens for tests. A local RSA key stands in for the sign-in provider's (Clerk): the app is
given its public half as a static key set (`JWT_JWKS_JSON`), so real RS256 verification runs with no network."""

import json
import time
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://clerk.test.invalid"
KID = "test-key-1"
DEFAULT_PLAYER = "00000000-0000-4000-8000-000000000001"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # not trusted by the app


def public_jwk(key=KEY, kid=KID) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def jwks_json(*keys: tuple) -> str:
    """A key set holding the trusted key (or the given (key, kid) pairs)."""
    pairs = keys or ((KEY, KID),)
    return json.dumps({"keys": [public_jwk(k, kid) for k, kid in pairs]})


def token(
    player_id: object = DEFAULT_PLAYER,
    *,
    key=KEY,
    kid: str | None = KID,
    algorithm: str = "RS256",
    expires_in: int = 300,
    not_before_in: int = -5,
    issuer: str | None = ISSUER,
    **claims,
) -> str:
    """A signed session token. `player_id=None` leaves that claim out; any claim given as `...` is left out."""
    now = int(time.time())
    payload = {
        "sub": "user_2abcDEFghi",
        "iss": issuer,
        "iat": now - 10,
        "nbf": now + not_before_in,
        "exp": now + expires_in,
        "player_id": str(player_id) if isinstance(player_id, UUID) else player_id,
        **claims,
    }
    payload = {k: v for k, v in payload.items() if v is not None and v is not ...}
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)
