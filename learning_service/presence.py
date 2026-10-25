"""Friend-only presence leases and social preferences in the existing home database."""

from datetime import datetime, timezone

from fastapi import HTTPException

SCHEMA = """
create table if not exists social_settings (
    player_id text primary key references homes(player_id) on delete cascade,
    share_presence integer not null default 1 check(share_presence in (0,1)),
    allow_game_invites integer not null default 1 check(allow_game_invites in (0,1))
) strict;
create table if not exists presence_sessions (
    player_id text not null references homes(player_id) on delete cascade,
    session_id text not null,
    status text not null check(status in ('online','away','busy')),
    last_seen integer not null,
    primary key(player_id, session_id)
) strict, without rowid;
create table if not exists presence_seen (
    player_id text primary key references homes(player_id) on delete cascade,
    last_seen integer not null
) strict;
"""
MAX_SESSIONS = 8


def clock() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def stamp(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def settings(home, player: str) -> dict:
    rows = home._query(
        "select share_presence,allow_game_invites from social_settings where player_id=?", (player,)
    )
    return (
        dict(zip(("share_presence", "allow_game_invites"), map(bool, rows[0])))
        if rows
        else {
            "share_presence": True,
            "allow_game_invites": True,
        }
    )


def put_settings(home, player: str, values: dict) -> dict:
    with home._tx() as db:
        home._ensure(db, player)
        db.execute(
            "insert into social_settings values (?,?,?) on conflict(player_id) do update set "
            "share_presence=excluded.share_presence,allow_game_invites=excluded.allow_game_invites",
            (player, int(values["share_presence"]), int(values["allow_game_invites"])),
        )
    return settings(home, player)


def are_friends(home, one: str, other: str) -> bool:
    return bool(
        home._query(
            "select 1 from friendships where a=? and b=? and status='accepted'",
            home._pair(one, other),
        )
    )


def require_friend(home, one: str, other: str):
    if one == other:
        raise HTTPException(422, "Choose another player.")
    if not are_friends(home, one, other):
        raise HTTPException(404, "Accepted friend not found.")


def read(home, player: str, ttl: int, *, visible: bool = True) -> dict:
    now = clock()
    if not visible or not settings(home, player)["share_presence"]:
        return {"status": "offline", "last_seen": None}
    rows = home._query(
        "select status,last_seen from presence_sessions where player_id=? and last_seen>?",
        (player, now - ttl),
    )
    status = next(
        (s for s in ("busy", "online", "away") if any(r[0] == s for r in rows)), "offline"
    )
    seen = home._query("select last_seen from presence_seen where player_id=?", (player,))
    return {"status": status, "last_seen": stamp(seen[0][0]) if seen else None}


def heartbeat(home, player: str, session_id: str, status: str, ttl: int) -> dict:
    now = clock()
    with home._tx() as db:
        home._ensure(db, player)
        db.execute(
            "delete from presence_sessions where player_id=? and last_seen<=?", (player, now - ttl)
        )
        existing = db.execute(
            "select 1 from presence_sessions where player_id=? and session_id=?",
            (player, session_id),
        ).fetchone()
        count = db.execute(
            "select count(*) from presence_sessions where player_id=?", (player,)
        ).fetchone()[0]
        if existing is None and count >= MAX_SESSIONS:
            raise HTTPException(
                429, "Too many active presence sessions; close another session or wait for expiry."
            )
        db.execute(
            "insert into presence_sessions values (?,?,?,?) on conflict(player_id,session_id) "
            "do update set status=excluded.status,last_seen=excluded.last_seen",
            (player, session_id, status, now),
        )
        db.execute(
            "insert into presence_seen values (?,?) on conflict(player_id) do update set last_seen=excluded.last_seen",
            (player, now),
        )
    return {
        "session_id": session_id,
        "expires_at": stamp(now + ttl),
        "heartbeat_after_seconds": max(5, ttl // 3),
        "server_now": stamp(now),
    }


def disconnect(home, player: str, session_id: str):
    with home._tx() as db:
        db.execute(
            "delete from presence_sessions where player_id=? and session_id=?", (player, session_id)
        )


def friends(home, player: str, ttl: int) -> dict:
    with home._lock:
        items = [
            {k: v for k, v in friend.model_dump().items() if k != "status"}
            | read(home, friend.player_id, ttl)
            for friend in home.list_friends(player)
            if friend.status == "accepted"
        ]
    return {"items": items, "server_now": stamp(clock()), "poll_after_seconds": 15}
