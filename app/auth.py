"""
Who is calling: verifying the sign-in token.

Every /api request carries `Authorization: Bearer <session JWT>` issued by the sign-in provider (Clerk). This module
checks it and returns the player the data belongs to; nothing else about the caller is read. The API still has no
login, registration or token-issuing routes: the provider signs people in, this only verifies what it signed.

  - RS256 only, with the provider's public keys found by the token's `kid`. A token of any other algorithm (`none`,
    HS256 signed with a public key, ...) is refused before any key is tried.
  - The signature, `iss` (must equal JWT_ISSUER), `exp` and `nbf` are checked, with a few seconds' leeway for clocks.
    `aud` and `azp` (the app the token was made for) are checked only if configured.
  - The player is the token's `player_id` claim, a UUID the frontend adds to the session token. A token without a
    valid one is refused, whatever else it says.

Why a token is refused is logged by kind, never with the token; the caller only learns that it was not accepted,
or that it has expired (so the app can sign in again). Keys are fetched once and kept (RemoteKeys): a new key id
(a rotation) triggers one more fetch at most every few seconds, and if the provider cannot be reached the keys already
held keep working. With no keys at all the answer is 503, not 401: the caller is not at fault.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from uuid import UUID

import httpx
import jwt

log = logging.getLogger("auth")

ALGORITHM = "RS256"
MAX_TOKEN_CHARS = 8192  # a Clerk session token is around 1 KB
NOT_ACCEPTED = "Your sign-in was not accepted. Sign in again."
EXPIRED = "Your sign-in has expired. Sign in again."
NO_PLAYER = "This sign-in has no valid player_id. Add `player_id` to the session token in Clerk."
KEYS_DOWN = "The sign-in keys are unavailable right now. Try again shortly."


class AuthError(Exception):
    """A request that must be refused. `status` is the HTTP status and `detail` is safe to show the caller."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class StaticKeys:
    """A fixed key set (JWT_JWKS_JSON): public keys only, never fetched."""

    def __init__(self, jwks: dict):
        self.keys = parse_keys(jwks)

    def get(self, kid: str) -> jwt.PyJWK | None:
        return self.keys.get(kid)


class RemoteKeys:
    """The provider's published key set, fetched on demand and kept.

    `refresh_after`: a key set older than this is fetched again. `unknown_key_wait`: after any fetch attempt, wait this
    long before another, so a flood of tokens with an unknown `kid` (or an outage) cannot hammer the provider."""

    def __init__(
        self,
        fetch: Callable[[], dict],
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_after: float = 3600,
        unknown_key_wait: float = 30,
    ):
        self.fetch, self.clock = fetch, clock
        self.refresh_after, self.unknown_key_wait = refresh_after, unknown_key_wait
        self.keys: dict[str, jwt.PyJWK] | None = None
        self.fetched_at = 0.0
        self.attempted_at: float | None = None
        self.lock = threading.Lock()

    def get(self, kid: str) -> jwt.PyJWK | None:
        with self.lock:
            now = self.clock()
            key = self.keys.get(kid) if self.keys is not None else None
            stale = self.keys is None or now - self.fetched_at > self.refresh_after
            may_try = self.attempted_at is None or now - self.attempted_at >= self.unknown_key_wait
            if (stale or key is None) and may_try:
                self.attempted_at = now
                try:
                    self.keys = parse_keys(self.fetch())
                    self.fetched_at = now
                except Exception as exc:  # network, bad status, bad JSON: keep what we hold
                    log.warning("could not fetch the sign-in keys: %s", type(exc).__name__)
                key = self.keys.get(kid) if self.keys is not None else None
            if self.keys is None:
                raise AuthError(503, KEYS_DOWN)
            return key


def parse_keys(jwks: dict) -> dict[str, jwt.PyJWK]:
    keys = {}
    for entry in jwks.get("keys", []) if isinstance(jwks, dict) else []:
        try:
            if isinstance(entry, dict) and isinstance(entry.get("kid"), str):
                keys[entry["kid"]] = jwt.PyJWK(entry, algorithm=ALGORITHM)
        except jwt.PyJWTError:
            log.warning("ignored a sign-in key that is not a usable RSA key")
    return keys


def fetch_jwks(url: str) -> dict:
    response = httpx.get(url, timeout=5.0, follow_redirects=False)
    response.raise_for_status()
    return response.json()


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        keys: StaticKeys | RemoteKeys,
        *,
        authorized_parties: tuple[str, ...] | list[str] = (),
        audience: str | None = None,
        leeway: int = 5,
    ):
        self.issuer, self.keys = issuer, keys
        self.authorized_parties, self.audience, self.leeway = authorized_parties, audience, leeway

    @classmethod
    def from_settings(cls, settings) -> TokenVerifier | None:
        """None when sign-in is not configured (no JWT_ISSUER): the API then refuses every request with a 503."""
        if not settings.jwt_issuer:
            return None
        if settings.jwt_jwks_json:
            import json

            keys: StaticKeys | RemoteKeys = StaticKeys(json.loads(settings.jwt_jwks_json))
        else:
            url = settings.jwks_url
            keys = RemoteKeys(lambda: fetch_jwks(url))
        return cls(
            settings.jwt_issuer,
            keys,
            authorized_parties=settings.jwt_authorized_parties,
            audience=settings.jwt_audience,
            leeway=settings.jwt_leeway_seconds,
        )

    def player_id(self, token: str) -> str:
        """The verified player (a canonical UUID string), or an AuthError."""
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise reject("a token of an unacceptable size")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise reject("not a JWT") from None
        kid = header.get("kid")
        if header.get("alg") != ALGORITHM or not isinstance(kid, str):
            raise reject("wrong algorithm or no key id")
        key = self.keys.get(kid)
        if key is None:
            raise reject("an unknown key id")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway,
                options={"require": ["exp", "iss"], "verify_aud": self.audience is not None},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError(401, EXPIRED) from None
        except jwt.PyJWTError as exc:
            raise reject(type(exc).__name__) from None
        if self.authorized_parties and claims.get("azp") not in self.authorized_parties:
            raise reject("an app that is not an authorized party")
        try:
            return str(UUID(claims.get("player_id")))
        except (ValueError, TypeError, AttributeError):  # missing, not text, or not a UUID
            log.info("sign-in token refused: no valid player_id claim")
            raise AuthError(401, NO_PLAYER) from None


def reject(why: str) -> AuthError:
    log.info("sign-in token refused: %s", why)
    return AuthError(401, NOT_ACCEPTED)
