"""Local-only demo identities and ephemeral sessions. Never touches Supabase."""

import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException

from app.models import User

DEMO_ACCOUNTS = {
    "parent@demo.example.com": {
        "password": "DemoParent123!",
        "id": "00000000-0000-4000-8000-000000000001",
        "display_name": "Alex (Demo Parent)",
        "account_type": "parent",
    },
    "child@demo.example.com": {
        "password": "DemoChild123!",
        "id": "00000000-0000-4000-8000-000000000002",
        "display_name": "Sam (Demo Child)",
        "account_type": "learner",
    },
}
ACCESS_TTL = 3600
REFRESH_TTL = 86400
MAX_SESSIONS = 1000


@dataclass(frozen=True)
class DemoSession:
    email: str
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int


class DemoAuth:
    def __init__(self):
        self.sessions: dict[str, DemoSession] = {}

    def user(self, email: str) -> dict:
        account = DEMO_ACCOUNTS[email]
        return User(
            id=account["id"],
            email=email,
            user_metadata={
                "display_name": account["display_name"],
                "account_type": account["account_type"],
                "demo": True,
                "demo_family_id": "demo-family",
            },
        ).model_dump(mode="json")

    def accounts(self) -> list[dict]:
        return [self.user(email) for email in DEMO_ACCOUNTS]

    def _invalid(self):
        raise HTTPException(401, "Invalid demo credentials or expired demo session.")

    def _prune(self):
        now = int(time.time())
        for key, session in list(self.sessions.items()):
            if session.refresh_expires_at <= now:
                del self.sessions[key]

    def _issue(self, email: str) -> dict:
        self._prune()
        if len(self.sessions) >= MAX_SESSIONS:
            raise HTTPException(429, "Demo session limit reached. Restart the demo server.")
        now = int(time.time())
        session = DemoSession(
            email=email,
            access_token="demo_access_" + secrets.token_urlsafe(32),
            refresh_token="demo_refresh_" + secrets.token_urlsafe(32),
            expires_at=now + ACCESS_TTL,
            refresh_expires_at=now + REFRESH_TTL,
        )
        self.sessions[session.refresh_token] = session
        return {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TTL,
            "expires_at": session.expires_at,
            "user": self.user(email),
        }

    def login(self, email: str, password: str) -> dict:
        email = email.lower()
        account = DEMO_ACCOUNTS.get(email)
        expected = account["password"] if account else "invalid-demo-account"
        valid = secrets.compare_digest(password.encode(), expected.encode())
        if account is None or not valid:
            self._invalid()
        return self._issue(email)

    def authenticate(self, token: str) -> DemoSession:
        self._prune()
        now = int(time.time())
        for session in self.sessions.values():
            if (
                secrets.compare_digest(token.encode(), session.access_token.encode())
                and session.expires_at > now
            ):
                return session
        self._invalid()

    def refresh(self, token: str) -> dict:
        self._prune()
        session = self.sessions.pop(token, None)
        if session is None:
            self._invalid()
        # Rotation invalidates both previous tokens. No await between pop and issuance.
        return self._issue(session.email)

    def logout(self, token: str):
        session = self.authenticate(token)
        del self.sessions[session.refresh_token]
