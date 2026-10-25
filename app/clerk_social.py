"""Verified Clerk sessions for /api/social only; legacy player selection stays unchanged."""

import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.context import api_context

bearer = HTTPBearer(
    scheme_name="ClerkSession",
    bearerFormat="JWT",
    auto_error=False,
    description="Clerk session JWT with signed player_id and username claims. Required only for /api/social.",
)


@dataclass(frozen=True)
class Identity:
    subject: str
    player_id: str
    session_id: str
    username: str | None
    issuer: str


def unauthorized():
    return HTTPException(
        401, "Invalid or expired Clerk session token.", headers={"WWW-Authenticate": "Bearer"}
    )


class ClerkVerifier:
    def __init__(self, settings):
        self.issuer = settings.clerk_issuer
        self.parties = settings.clerk_authorized_parties or settings.cors_origins
        self.lock = threading.Lock()
        self.jwks = (
            jwt.PyJWKClient(self.issuer + "/.well-known/jwks.json", timeout=5, lifespan=300)
            if self.issuer
            else None
        )

    def verify(self, token: str) -> Identity:
        if len(token) > 8192:
            raise unauthorized()
        if self.jwks is None:
            raise HTTPException(503, "Configure CLERK_ISSUER before using the social API.")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not 1 <= len(header["kid"]) <= 128
            ):
                raise unauthorized()
            # The URL is configured, never taken from unverified token headers or issuer claims.
            with self.lock:
                key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "require": ["exp", "nbf", "iat", "iss", "sub", "sid", "player_id"],
                    "verify_aud": False,
                },
            )
            if claims.get("azp") is not None and claims["azp"] not in self.parties:
                raise unauthorized()
            if claims.get("sts") == "pending":
                raise unauthorized()
            if any(
                not isinstance(claims.get(k), str) or not 1 <= len(claims[k]) <= 128
                for k in ("sub", "sid")
            ):
                raise unauthorized()
            player_id = str(UUID(claims["player_id"]))
            username = claims.get("username")
            if username is not None and (
                not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username)
            ):
                raise unauthorized()
            return Identity(claims["sub"], player_id, claims["sid"], username, self.issuer)
        except jwt.PyJWKClientConnectionError as exc:
            raise HTTPException(503, "Session verification is temporarily unavailable.") from exc
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
            raise unauthorized() from exc


def identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    player_header: Annotated[
        str | None,
        Header(
            alias="X-Player-ID",
            description="Optional compatibility header; must match signed player_id. Usually omit in Swagger.",
        ),
    ] = None,
) -> Identity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    actor = request.app.state.clerk_social.verify(credentials.credentials)
    if player_header is not None:
        try:
            matches = str(UUID(player_header)) == actor.player_id
        except ValueError:
            matches = False
        if not matches:
            raise HTTPException(403, "X-Player-ID does not match the verified session.")
    return actor


async def social_context(request: Request, actor: Annotated[Identity, Depends(identity)]):
    request.state.social_identity = actor
    async with asynccontextmanager(api_context)(request, actor.player_id):
        yield actor
