"""Shared helpers for the end-to-end edge-case suite."""

from __future__ import annotations

import pytest

from app import rooms
from app.repository import current_session
from tests.learning.test_app import create_quiz  # noqa: F401
from tests.learning.test_materials_api import book, lesson_from, upload  # noqa: F401

PARENT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
CANARY = "SECRETCANARY-9f3a71"


def login_headers(client, account="child"):
    email = f"{account}@demo.example.com"
    password = {"parent": "DemoParent123!", "child": "DemoChild123!"}[account]
    session = client.post("/auth/login", json={"email": email, "password": password}).json()
    return {"Authorization": "Bearer " + session["access_token"]}


def child_headers(client):
    return login_headers(client, "child")


def approval(revision=1):
    return {"expected_revision": revision, "confirmed": True}


def api_routes(app):
    """(METHOD, path template) for every data route under /api, read from the published schema."""
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api"):
            for method in operations:
                yield method.upper(), path


def keys_anywhere(value):
    """Every dict key that appears at any depth of a JSON-like value."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_anywhere(item)
    return found


def as_owner(repository, owner, action, room_id=None, payload=None):
    """Run a real room command as any account, so lobbies can be filled beyond the two demo users."""
    with repository.session(owner) as unit:
        token = current_session.set(unit)
        try:
            return rooms.command(action, room_id, payload)
        finally:
            current_session.reset(token)


def bug_on_postgres(request, repository, reason):
    """Mark the running test as a known failure only when PostgreSQL is the adapter under test."""
    if repository.durable:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))


def draw_diagram(client, lesson_id):
    """Draw the lesson's visuals (the fake harness returns one diagram)."""
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    assert r.status_code == 201 and r.json()["visuals"][0]["kind"] == "diagram", r.text
    return r.json()["visuals"][0]
