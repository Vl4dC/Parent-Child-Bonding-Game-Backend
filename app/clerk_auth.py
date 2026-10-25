"""Clerk session verification with bounded, issuer-pinned JWKS caching. No account APIs."""

import json
import re
import threading
import time
from uuid import NAMESPACE_URL, uuid5

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException


def player_uuid(issuer, subject):
    return str(uuid5(NAMESPACE_URL, "questnest:clerk:" + issuer.rstrip("/") + ":" + subject))


def unauthorized():
    return HTTPException(
        401, "Invalid or expired Clerk session.", headers={"WWW-Authenticate": "Bearer"}
    )


class ClerkVerifier:
    def __init__(self, settings, *, transport=None, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.lock = threading.Lock()
        self.keys = {}
        self.expires = 0
        self.last_fetch = float("-inf")
        self.client = None
        self.static_key = None
        if settings.clerk_jwt_key:
            try:
                self.static_key = serialization.load_pem_public_key(
                    settings.clerk_jwt_key.get_secret_value().replace("\\n", "\n").encode()
                )
                if not isinstance(self.static_key, RSAPublicKey) or self.static_key.key_size < 2048:
                    raise ValueError()
            except Exception:
                raise ValueError(
                    "CLERK_JWT_KEY must be an RSA public key of at least 2048 bits"
                ) from None
        elif settings.clerk_issuer:
            self.client = httpx.Client(
                timeout=httpx.Timeout(5, connect=3),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            )

    def close(self):
        if self.client:
            self.client.close()

    def readiness(self):
        if not self.settings.clerk_issuer:
            return {
                "enabled": False,
                "ready": self.settings.app_env != "production",
                "state": "local_test",
            }
        ready = self.static_key is not None
        if not ready:
            try:
                if not self.keys or self.clock() >= self.expires:
                    self.key("__readiness__")
            except HTTPException:
                pass
            ready = bool(self.keys) and self.clock() < self.expires
        return {
            "enabled": True,
            "ready": ready,
            "state": "ready" if ready else "unavailable",
            "reason": None if ready else "verification_keys_unavailable",
        }

    def key(self, kid):
        if self.static_key:
            return self.static_key
        with self.lock:
            now = self.clock()
            if now < self.expires and kid in self.keys:
                return self.keys[kid]
            if now - self.last_fetch < 10:
                if now >= self.expires:
                    raise HTTPException(503, "Clerk verification keys are temporarily unavailable.")
                raise unauthorized()
            self.last_fetch = now
            try:
                started = time.monotonic()
                with self.client.stream(
                    "GET", self.settings.clerk_issuer + "/.well-known/jwks.json"
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1_048_576 or time.monotonic() - started > 10:
                            raise ValueError()
                        chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
                if not isinstance(payload.get("keys"), list) or len(payload["keys"]) > 20:
                    raise ValueError()
                keys = {}
                for value in payload["keys"]:
                    if (
                        value.get("kty") != "RSA"
                        or value.get("use", "sig") != "sig"
                        or value.get("alg", "RS256") != "RS256"
                        or "verify" not in value.get("key_ops", ["verify"])
                    ):
                        continue
                    name = value.get("kid")
                    if (
                        not isinstance(name, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name)
                        or name in keys
                    ):
                        raise ValueError()
                    public = jwt.PyJWK.from_dict(value, algorithm="RS256").key
                    if not isinstance(public, RSAPublicKey) or public.key_size < 2048:
                        raise ValueError()
                    keys[name] = public
                if not keys:
                    raise ValueError()
            except Exception:
                raise HTTPException(
                    503, "Clerk verification keys are temporarily unavailable."
                ) from None
            self.keys, self.expires = keys, now + 300
            if kid not in keys:
                raise unauthorized()
            return keys[kid]

    def verify(self, authorization):
        if not authorization or len(authorization) > 16_384:
            raise unauthorized()
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or " " in token:
            raise unauthorized()
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or header.get("crit")
                or header.get("typ", "JWT") != "JWT"
                or not isinstance(kid, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", kid)
            ):
                raise unauthorized()
            payload = jwt.decode(
                token,
                self.key(kid),
                algorithms=["RS256"],
                issuer=self.settings.clerk_issuer,
                audience=self.settings.clerk_audience,
                options={"require": ["iss", "sub", "sid", "exp", "nbf", "iat"]},
                leeway=5,
            )
            if (
                not isinstance(payload["sub"], str)
                or not re.fullmatch(r"user_[A-Za-z0-9]{1,120}", payload["sub"])
                or not isinstance(payload["sid"], str)
                or not re.fullmatch(r"sess_[A-Za-z0-9]{1,120}", payload["sid"])
                or payload.get("azp") not in self.settings.clerk_authorized_parties
                or payload.get("sts") not in (None, "active")
                or payload.get("v", 1) not in (1, 2)
                or type(payload.get("v", 1)) is not int
                or any(type(payload[c]) is not int for c in ("exp", "nbf", "iat"))
                or payload["exp"] <= payload["iat"]
            ):
                raise unauthorized()
            return {
                "player_id": player_uuid(self.settings.clerk_issuer, payload["sub"]),
                "subject": payload["sub"],
            }
        except HTTPException:
            raise
        except Exception:
            raise unauthorized() from None


def install(app):
    from typing import Annotated, Literal

    from fastapi import Depends, Request
    from pydantic import BaseModel

    from app.player import player_id

    class PlayerIdentity(BaseModel):
        player_id: str
        provider: Literal["clerk", "local_test"]

    @app.get("/api/me/player", response_model=PlayerIdentity, tags=["Player identity"])
    async def identity(request: Request, owner: Annotated[str, Depends(player_id)]):
        return {
            "player_id": owner,
            "provider": "clerk" if request.app.state.settings.clerk_issuer else "local_test",
        }

    original = app.openapi

    def openapi():
        schema = original()
        if app.state.settings.clerk_issuer:
            schema.setdefault("components", {}).setdefault("securitySchemes", {})[
                "ClerkSession"
            ] = {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "A Clerk session token from the configured issuer and frontend origin.",
            }
            for path, methods in schema["paths"].items():
                if path.startswith("/api/") and not path.startswith("/api/registration/"):
                    for method, operation in methods.items():
                        if method not in ("get", "post", "put", "patch", "delete"):
                            continue
                        operation["security"] = [{"ClerkSession": []}]
                        operation.setdefault("responses", {})["401"] = {
                            "description": "Valid Clerk session required"
                        }
                        operation["parameters"] = [
                            p
                            for p in operation.get("parameters", [])
                            if p.get("name") != "X-Player-ID"
                        ]
        return schema

    app.openapi = openapi
