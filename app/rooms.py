"""Private lobby infrastructure. No unconfirmed game rules, scoring, or rewards."""

import copy
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import Field

from app.context import api_context
from app.platform import StrictModel
from app.repository import current_session

router = APIRouter(
    prefix="/api/rooms",
    tags=["Private rooms"],
    dependencies=[Depends(api_context, scope="function")],
)


class CreateRoom(StrictModel):
    display_name: str = Field(..., min_length=1, max_length=60)
    capacity: int = Field(4, ge=2, le=8)


class JoinRoom(StrictModel):
    code: str = Field(..., pattern=r"^[A-Z0-9]{8}$")
    display_name: str = Field(..., min_length=1, max_length=60)


class RoomCommand(StrictModel):
    expected_version: int = Field(..., ge=1)
    action: Literal["ready", "leave", "close"]
    ready: bool = False


class Member(StrictModel):
    user_id: str
    display_name: str
    ready: bool


class RoomSnapshot(StrictModel):
    id: str
    code: str
    host_id: str
    capacity: int
    version: int
    closed: bool
    expires_at: datetime
    members: list[Member]


class LeftRoom(StrictModel):
    left: Literal[True]


def memory_command(repository, owner, action, room_id, payload):
    with repository.lock:
        if action == "create":
            room = {
                "id": str(uuid4()),
                "code": "".join(
                    secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)
                ),
                "host_id": owner,
                "capacity": payload["capacity"],
                "version": 1,
                "closed": False,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "members": [
                    {"user_id": owner, "display_name": payload["display_name"], "ready": False}
                ],
            }
            repository.rooms[room["id"]] = room
            return copy.deepcopy(room)
        if action == "join":
            room = next(
                (r for r in repository.rooms.values() if r["code"] == payload["code"]), None
            )
        else:
            room = repository.rooms.get(room_id)
        if room is None or datetime.fromisoformat(room["expires_at"]) <= datetime.now(timezone.utc):
            raise HTTPException(404, "Room not found.")
        member = next((m for m in room["members"] if m["user_id"] == owner), None)
        if action == "join":
            if room["closed"]:
                raise HTTPException(409, "Room is closed.")
            if member is None:
                if len(room["members"]) >= room["capacity"]:
                    raise HTTPException(409, "Room is full.")
                room["members"].append(
                    {"user_id": owner, "display_name": payload["display_name"], "ready": False}
                )
                room["version"] += 1
        else:
            if member is None:
                raise HTTPException(404, "Room not found.")
            if action != "snapshot":
                if payload["expected_version"] != room["version"]:
                    raise HTTPException(409, "Room changed. Fetch its current snapshot.")
                if action == "close":
                    if room["host_id"] != owner:
                        raise HTTPException(403, "Only the host can close the room.")
                    room["closed"] = True
                elif action == "leave":
                    room["members"].remove(member)
                    if room["members"]:
                        room["host_id"] = room["members"][0]["user_id"]
                    else:
                        del repository.rooms[room["id"]]
                        return {"left": True}
                    room["version"] += 1
                    return {"left": True}
                elif action == "ready":
                    if room["closed"]:
                        raise HTTPException(409, "Room is closed.")
                    member["ready"] = payload["ready"]
                room["version"] += 1
        return copy.deepcopy(room)


def command(action, room_id=None, payload=None):
    unit = current_session.get()
    payload = payload or {}
    if unit.connection:
        result = unit.connection.execute(
            "select api_private.room_command(%s,%s,%s)", (action, room_id, Jsonb(payload))
        ).fetchone()[0]
        if "error" in result:
            raise HTTPException(result["status"], result["error"])
        return result
    return memory_command(unit.repository, unit.owner, action, room_id, payload)


@router.post("", status_code=201, response_model=RoomSnapshot)
def create_room(body: CreateRoom):
    """Create a private lobby with an eight-character invite code and 24-hour expiry."""
    if not body.display_name.strip():
        raise HTTPException(422, "Display name must contain text.")
    return command("create", payload=body.model_dump())


@router.post("/join", response_model=RoomSnapshot)
def join_room(body: JoinRoom):
    """Join by invite code. Repeating a join by the same account returns its existing membership."""
    if not body.display_name.strip():
        raise HTTPException(422, "Display name must contain text.")
    return command("join", payload=body.model_dump())


@router.get("/{room_id}", response_model=RoomSnapshot)
def snapshot(room_id: str):
    """Read the current authorized lobby snapshot, including after reconnecting."""
    return command("snapshot", room_id)


@router.post("/{room_id}/commands", response_model=RoomSnapshot | LeftRoom)
def room_command(room_id: str, body: RoomCommand):
    """Set readiness, leave, or close as host. Reject stale versions with 409; no game is started."""
    return command(body.action, room_id, body.model_dump())
