"""The calling player, taken only from the verified sign-in token (app/auth.py). The `X-Player-ID` header of the
earlier public API is no longer read."""

from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import AuthError

# auto_error is off so that a missing or odd header gets the same plain 401 as a bad token, and so that Swagger shows
# an Authorize button for pasting a token.
bearer = HTTPBearer(
    auto_error=False,
    description="The sign-in session token (a Clerk JWT) with a `player_id` claim.",
)
UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def player_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str:
    verifier = request.app.state.token_verifier
    if verifier is None:
        raise HTTPException(
            503, "Sign-in is not configured on this server. Set JWT_ISSUER (and its keys)."
        )
    if credentials is None or not credentials.credentials.strip():
        raise HTTPException(401, "Sign in required.", headers=UNAUTHENTICATED)
    try:
        return verifier.player_id(credentials.credentials.strip())
    except AuthError as exc:
        raise HTTPException(
            exc.status, exc.detail, headers=UNAUTHENTICATED if exc.status == 401 else None
        ) from None
