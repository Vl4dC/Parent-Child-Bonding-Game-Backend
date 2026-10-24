"""Fixed synthetic identities for explicit local demo mode; no login or session lifecycle."""

from fastapi import HTTPException

from app.models import User

DEMO_IDENTITIES = {
    "demo_parent": {
        "id": "00000000-0000-4000-8000-000000000001",
        "email": "parent@demo.example.com",
        "display_name": "Alex (Demo Parent)",
        "account_type": "parent",
    },
    "demo_child": {
        "id": "00000000-0000-4000-8000-000000000002",
        "email": "child@demo.example.com",
        "display_name": "Sam (Demo Child)",
        "account_type": "learner",
    },
}


def verify_demo_identity(token: str) -> dict:
    account = DEMO_IDENTITIES.get(token)
    if account is None:
        raise HTTPException(401, "Unknown demo identity.")
    return User(
        id=account["id"],
        email=account["email"],
        user_metadata={
            "display_name": account["display_name"],
            "account_type": account["account_type"],
            "demo": True,
            "demo_family_id": "demo-family",
        },
    ).model_dump(mode="json")
