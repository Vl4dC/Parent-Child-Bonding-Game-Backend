"""
SQLite store for the home: each player's rooms and item levels, their wallet and coin history, friendships and
cheers.

One database file (HOME_DB_PATH, default home.db; ":memory:" in tests). Every row carries the player id from
the verified sign-in token. Friendships and the `visibility` setting are an advisory privacy setting: a friend is
whoever shared a code, and nothing proves two players are a real family.

Only what a player chose is saved: rooms built, each item's level, and where it was moved and turned. Room art,
item looks, default spots and the numbers (costs, max level, unlock level) are the frontend's, or are read from
home_rules.json. A home is created the first time its player is seen: the living room with its starter items at
level 1, and a wallet with the starter coins.

Spending is one `begin immediate` transaction: the wallet is debited only if it holds enough, the change is
written, the ledger row is appended and the home's `version` is bumped, or nothing at all is. This is a
single-process store, like chat memory.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Annotated, Callable, Iterator, Literal

from pydantic import BaseModel, Field

from learning_service.home_rules import RULES
from learning_service.presence import SCHEMA as PRESENCE_SCHEMA
from learning_service.shop import CATALOG, ITEMS

Visibility = Literal["family", "friends", "nobody"]
HOME = "home"  # the room id of a cheer for the whole home
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
Coord = Annotated[float, Field(ge=0, le=RULES.floor_tiles, allow_inf_nan=False)]

_ROOM_IDS = "', '".join(r.id for r in RULES.rooms)
SCHEMA = f"""
create table if not exists homes (
    player_id text primary key,
    display_name text not null default 'Player',
    avatar text,
    visibility text not null default 'friends' check (visibility in ('family', 'friends', 'nobody')),
    version integer not null default 1,
    updated_at text not null
) strict;
create table if not exists home_rooms (
    player_id text not null references homes (player_id) on delete cascade,
    room_id text not null check (room_id in ('{_ROOM_IDS}')),
    built_at text not null,
    primary key (player_id, room_id)
) strict, without rowid;
create table if not exists home_items (
    player_id text not null,
    room_id text not null,
    item_id text not null,
    level integer not null check (level between 1 and {RULES.max_level}),
    x real,
    y real,
    rot integer not null default 0 check (rot between 0 and 3),
    check ((x is null) = (y is null)),
    primary key (player_id, room_id, item_id),
    foreign key (player_id, room_id) references home_rooms (player_id, room_id) on delete cascade
) strict, without rowid;
create table if not exists wallets (
    player_id text primary key,
    coins integer not null default 0 check (coins >= 0)
) strict;
create table if not exists coin_ledger (
    id integer primary key,
    player_id text not null references wallets (player_id) on delete cascade,
    delta integer not null,
    reason text not null,
    ref text not null,
    created_at text not null,
    unique (player_id, reason, ref)
) strict;
create table if not exists inventory (
    player_id text not null references wallets (player_id) on delete cascade,
    item_id text not null,
    acquired_at text not null,
    primary key (player_id, item_id)
) strict, without rowid;
create table if not exists equipment (
    player_id text not null references wallets (player_id) on delete cascade,
    slot text not null,
    item_id text,
    primary key (player_id, slot),
    foreign key (player_id, item_id) references inventory (player_id, item_id)
) strict, without rowid;
create table if not exists friend_codes (
    player_id text primary key,
    code text not null unique
) strict;
create table if not exists friendships (
    a text not null,
    b text not null,
    status text not null check (status in ('pending', 'accepted')),
    requested_by text not null,
    created_at text not null,
    check (a < b),
    check (requested_by in (a, b)),
    primary key (a, b)
) strict, without rowid;
create index if not exists friendships_by_b on friendships (b);
create table if not exists home_cheers (
    visitor text not null,
    owner text not null references homes (player_id) on delete cascade,
    room_id text not null,
    day text not null,
    primary key (visitor, owner, room_id, day)
) strict, without rowid;
"""


class HomeError(Exception):
    """A rule was broken. `status` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Placement(BaseModel):
    """Where an item was moved (floor tiles) and how many quarter turns it was rotated."""

    at: tuple[Coord, Coord] | None = Field(
        None, description="[x, y] in floor tiles, 0 to 2.7. Null means the item's default spot"
    )
    rot: int = Field(0, ge=0, le=3, description="Quarter turns, 0 to 3")


class Owner(BaseModel):
    id: str
    display_name: str
    avatar: str | None = None


class ItemState(BaseModel):
    id: str
    level: int
    at: tuple[float, float] | None = None
    rot: int = 0


class RoomState(BaseModel):
    id: str
    items: list[ItemState] = Field(..., description="Placed items only, in the room's item order")


class Summary(BaseModel):
    rooms: int
    levels: int
    max_levels: int


class Home(BaseModel):
    """What the app needs to draw a home, its own or a friend's. No coins."""

    owner: Owner
    version: int
    rooms: list[RoomState]
    summary: Summary


class MyHome(Home):
    visibility: Visibility
    coins: int
    cheers: dict[str, int] = Field(
        default_factory=dict, description="Cheers received so far, by room id (or `home`)"
    )


class HomeCard(BaseModel):
    owner: Owner
    rooms: int
    levels: int
    max_levels: int


class Friend(BaseModel):
    player_id: str
    display_name: str
    avatar: str | None = None
    status: Literal["accepted", "incoming", "outgoing"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class HomeStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.today: Callable[[], str] = utc_today
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma foreign_keys = on")
        if path != ":memory:":
            self._db.execute("pragma journal_mode = wal")
        self._db.executescript(SCHEMA + PRESENCE_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                yield self._db
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ creating and reading a home
    def _ensure(self, db: sqlite3.Connection, pid: str) -> None:
        """Create the home, its living room and its wallet the first time a player is seen."""
        stamp = now()
        created = db.execute(
            "insert or ignore into homes (player_id, updated_at) values (?, ?)", (pid, stamp)
        )
        if created.rowcount == 0:
            return
        self._add_room(db, pid, RULES.rooms[0].id, stamp)
        db.execute(
            "insert into wallets (player_id, coins) values (?, ?)", (pid, RULES.starter_coins)
        )
        self._ledger(db, pid, RULES.starter_coins, "starter", "starter")

    def _add_room(self, db: sqlite3.Connection, pid: str, room_id: str, stamp: str) -> None:
        db.execute(
            "insert into home_rooms (player_id, room_id, built_at) values (?, ?, ?)",
            (pid, room_id, stamp),
        )
        for item in RULES.room(room_id).items:
            if item.starter:
                db.execute(
                    "insert into home_items (player_id, room_id, item_id, level) values (?, ?, ?, 1)",
                    (pid, room_id, item.id),
                )

    def _ledger(
        self,
        db: sqlite3.Connection,
        pid: str,
        delta: int,
        reason: str,
        ref: str,
        *,
        ignore: bool = False,
    ) -> bool:
        verb = "insert or ignore" if ignore else "insert"
        cursor = db.execute(
            f"{verb} into coin_ledger (player_id, delta, reason, ref, created_at) "
            "values (?, ?, ?, ?, ?)",
            (pid, delta, reason, ref, now()),
        )
        return cursor.rowcount > 0

    def _built(self, db: sqlite3.Connection, pid: str) -> list[str]:
        rows = db.execute("select room_id from home_rooms where player_id = ?", (pid,)).fetchall()
        have = {r[0] for r in rows}
        return [room.id for room in RULES.rooms if room.id in have]

    def _levels(self, db: sqlite3.Connection, pid: str, room_id: str) -> dict[str, int]:
        rows = db.execute(
            "select item_id, level from home_items where player_id = ? and room_id = ?",
            (pid, room_id),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _home(self, db: sqlite3.Connection, pid: str) -> Home:
        head = db.execute(
            "select display_name, avatar, version from homes where player_id = ?", (pid,)
        ).fetchone()
        rooms: list[RoomState] = []
        total = 0
        for room_id in self._built(db, pid):
            rows = {
                r["item_id"]: r
                for r in db.execute(
                    "select item_id, level, x, y, rot from home_items "
                    "where player_id = ? and room_id = ?",
                    (pid, room_id),
                ).fetchall()
            }
            items = []
            for item in RULES.room(room_id).items:
                row = rows.get(item.id)
                if row is None:
                    continue
                total += row["level"]
                at = None if row["x"] is None else (row["x"], row["y"])
                items.append(ItemState(id=item.id, level=row["level"], at=at, rot=row["rot"]))
            rooms.append(RoomState(id=room_id, items=items))
        return Home(
            owner=Owner(id=pid, display_name=head["display_name"], avatar=head["avatar"]),
            version=head["version"],
            rooms=rooms,
            summary=Summary(
                rooms=len(rooms),
                levels=total,
                max_levels=sum(len(RULES.room(r.id).items) * RULES.max_level for r in rooms),
            ),
        )

    def _my_home(self, db: sqlite3.Connection, pid: str) -> MyHome:
        home = self._home(db, pid)
        visibility = db.execute(
            "select visibility from homes where player_id = ?", (pid,)
        ).fetchone()[0]
        coins = db.execute("select coins from wallets where player_id = ?", (pid,)).fetchone()[0]
        cheers = {
            r[0]: r[1]
            for r in db.execute(
                "select room_id, count(*) from home_cheers where owner = ? group by room_id", (pid,)
            ).fetchall()
        }
        return MyHome(**home.model_dump(), visibility=visibility, coins=coins, cheers=cheers)

    def get_home(self, pid: str) -> MyHome:
        with self._tx() as db:
            self._ensure(db, pid)
            return self._my_home(db, pid)

    # ------------------------------------------------------------ changes (all in one transaction)
    def _begin_change(self, db: sqlite3.Connection, pid: str, expected_version: int | None) -> None:
        self._ensure(db, pid)
        version = db.execute("select version from homes where player_id = ?", (pid,)).fetchone()[0]
        if expected_version is not None and expected_version != version:
            raise HomeError(409, "The home changed somewhere else. Reload it and try again.")

    def _bump(self, db: sqlite3.Connection, pid: str) -> None:
        db.execute(
            "update homes set version = version + 1, updated_at = ? where player_id = ?",
            (now(), pid),
        )

    def _spend(self, db: sqlite3.Connection, pid: str, cost: int, reason: str, ref: str) -> None:
        cursor = db.execute(
            "update wallets set coins = coins - ? where player_id = ? and coins >= ?",
            (cost, pid, cost),
        )
        if cursor.rowcount == 0:
            raise HomeError(402, f"You need {cost} coins.")
        self._ledger(db, pid, -cost, reason, ref)

    @staticmethod
    def _room_item(room_id: str, item_id: str | None = None):
        room = RULES.room(room_id)
        if room is None:
            raise HomeError(404, "There is no such room.")
        if item_id is None:
            return room, None
        item = room.item(item_id)
        if item is None:
            raise HomeError(404, "There is no such item in that room.")
        return room, item

    def _require_built(self, db: sqlite3.Connection, pid: str, room_id: str) -> None:
        if room_id not in self._built(db, pid):
            raise HomeError(422, f"Build the {RULES.room(room_id).name.lower()} first.")

    def build_room(self, pid: str, expected_version: int | None) -> MyHome:
        with self._tx() as db:
            self._begin_change(db, pid, expected_version)
            built = self._built(db, pid)
            if len(built) == len(RULES.rooms):
                raise HomeError(422, "Every room is already built.")
            newest = RULES.room(built[-1])
            levels = self._levels(db, pid, newest.id)
            if any(levels.get(i.id, 0) < RULES.unlock_level for i in newest.items):
                raise HomeError(
                    422,
                    f"Every item in the {newest.name.lower()} must be at level "
                    f"{RULES.unlock_level} or higher first.",
                )
            room = RULES.rooms[len(built)]
            self._spend(db, pid, RULES.build_cost(len(built)), "build_room", room.id)
            self._add_room(db, pid, room.id, now())
            self._bump(db, pid)
            return self._my_home(db, pid)

    def place_item(
        self,
        pid: str,
        room_id: str,
        item_id: str,
        placement: Placement,
        expected_version: int | None,
    ) -> MyHome:
        room, item = self._room_item(room_id, item_id)
        with self._tx() as db:
            self._begin_change(db, pid, expected_version)
            self._require_built(db, pid, room.id)
            if item.id in self._levels(db, pid, room.id):
                raise HomeError(422, f"The {item.name.lower()} is already placed.")
            self._spend(db, pid, RULES.place_cost, "place", f"{room.id}/{item.id}/1")
            x, y = placement.at if placement.at else (None, None)
            db.execute(
                "insert into home_items (player_id, room_id, item_id, level, x, y, rot) "
                "values (?, ?, ?, 1, ?, ?, ?)",
                (pid, room.id, item.id, x, y, placement.rot),
            )
            self._bump(db, pid)
            return self._my_home(db, pid)

    def upgrade_item(
        self, pid: str, room_id: str, item_id: str, expected_version: int | None
    ) -> MyHome:
        room, item = self._room_item(room_id, item_id)
        with self._tx() as db:
            self._begin_change(db, pid, expected_version)
            self._require_built(db, pid, room.id)
            level = self._levels(db, pid, room.id).get(item.id)
            if level is None:
                raise HomeError(422, f"Place the {item.name.lower()} first.")
            if level >= RULES.max_level:
                raise HomeError(422, f"The {item.name.lower()} is already at the top level.")
            self._spend(
                db,
                pid,
                RULES.upgrade_cost(level + 1),
                "upgrade",
                f"{room.id}/{item.id}/{level + 1}",
            )
            db.execute(
                "update home_items set level = level + 1 "
                "where player_id = ? and room_id = ? and item_id = ?",
                (pid, room.id, item.id),
            )
            self._bump(db, pid)
            return self._my_home(db, pid)

    def set_layout(
        self, pid: str, room_id: str, items: dict[str, Placement], expected_version: int | None
    ) -> MyHome:
        """Replace the room's whole layout. Placed items left out go back to their default spot."""
        room, _ = self._room_item(room_id)
        with self._tx() as db:
            self._begin_change(db, pid, expected_version)
            self._require_built(db, pid, room.id)
            placed = self._levels(db, pid, room.id)
            for item_id in items:
                item = room.item(item_id)
                if item is None:
                    raise HomeError(422, f"There is no {item_id!r} in the {room.name.lower()}.")
                if item_id not in placed:
                    raise HomeError(422, f"The {item.name.lower()} is not placed yet.")
            db.execute(
                "update home_items set x = null, y = null, rot = 0 "
                "where player_id = ? and room_id = ?",
                (pid, room.id),
            )
            for item_id, placement in items.items():
                x, y = placement.at if placement.at else (None, None)
                db.execute(
                    "update home_items set x = ?, y = ?, rot = ? "
                    "where player_id = ? and room_id = ? and item_id = ?",
                    (x, y, placement.rot, pid, room.id, item_id),
                )
            self._bump(db, pid)
            return self._my_home(db, pid)

    def set_visibility(
        self, pid: str, visibility: Visibility, expected_version: int | None
    ) -> MyHome:
        with self._tx() as db:
            self._begin_change(db, pid, expected_version)
            db.execute("update homes set visibility = ? where player_id = ?", (visibility, pid))
            self._bump(db, pid)
            return self._my_home(db, pid)

    def set_card(self, pid: str, display_name: str, avatar: str | None) -> MyHome:
        """The name and avatar shown to friends. Not part of the drawn home, so `version` is untouched."""
        with self._tx() as db:
            self._ensure(db, pid)
            db.execute(
                "update homes set display_name = ?, avatar = ? where player_id = ?",
                (display_name, avatar, pid),
            )
            return self._my_home(db, pid)

    # ------------------------------------------------------------ coins
    def credit(self, pid: str, reason: str, entries: list[tuple[str, int]]) -> tuple[int, int]:
        """Add coins for `(ref, amount)` pairs. A reference already paid under this reason is skipped.

        Returns (balance, coins credited now)."""
        return self.credit_rewards(pid, [(reason, ref, amount) for ref, amount in entries])

    def credit_rewards(self, pid: str, entries: list[tuple[str, str, int]]) -> tuple[int, int]:
        """Atomically pay several reward components, each with its own duplicate protection."""
        with self._tx() as db:
            self._ensure(db, pid)
            credited = 0
            for reason, ref, amount in entries:
                if amount > 0 and self._ledger(db, pid, amount, reason, ref, ignore=True):
                    credited += amount
            db.execute("update wallets set coins = coins + ? where player_id = ?", (credited, pid))
            coins = db.execute("select coins from wallets where player_id = ?", (pid,)).fetchone()[
                0
            ]
            return coins, credited

    def _inventory(self, db: sqlite3.Connection, pid: str) -> dict:
        self._ensure(db, pid)
        for item in CATALOG:
            if item.starter:
                db.execute(
                    "insert or ignore into inventory values (?, ?, ?)", (pid, item.id, now())
                )
                db.execute(
                    "insert or ignore into equipment values (?, ?, ?)", (pid, item.slot, item.id)
                )
        return {
            "owned": [
                r[0]
                for r in db.execute(
                    "select item_id from inventory where player_id=? order by item_id", (pid,)
                )
            ],
            "equipped": dict(
                db.execute("select slot,item_id from equipment where player_id=?", (pid,))
            ),
        }

    def inventory(self, pid: str) -> dict:
        with self._tx() as db:
            return self._inventory(db, pid)

    def purchase(self, pid: str, item_id: str) -> dict:
        item = ITEMS.get(item_id)
        if item is None:
            raise HomeError(404, "Shop item not found.")
        with self._tx() as db:
            inventory = self._inventory(db, pid)
            owned = item_id in inventory["owned"]
            if not owned:
                self._spend(db, pid, item.price, "shop_purchase", item_id)
                db.execute("insert into inventory values (?, ?, ?)", (pid, item_id, now()))
            coins = db.execute("select coins from wallets where player_id=?", (pid,)).fetchone()[0]
            return {
                "item_id": item_id,
                "charged": 0 if owned else item.price,
                "coins": coins,
                "inventory": self._inventory(db, pid),
            }

    def equip(self, pid: str, slot: str, item_id: str | None) -> dict:
        with self._tx() as db:
            inventory = self._inventory(db, pid)
            if item_id is not None:
                item = ITEMS.get(item_id)
                if item is None:
                    raise HomeError(404, "Shop item not found.")
                if item.slot != slot:
                    raise HomeError(422, "That item does not fit this slot.")
                if item_id not in inventory["owned"]:
                    raise HomeError(409, "Buy this item before equipping it.")
            db.execute(
                "insert into equipment values (?, ?, ?) on conflict(player_id,slot) "
                "do update set item_id=excluded.item_id",
                (pid, slot, item_id),
            )
            return self._inventory(db, pid)

    def wallet(self, pid: str, after: int, limit: int) -> dict:
        with self._tx() as db:
            self._ensure(db, pid)
            rows = db.execute(
                "select id,delta,reason,ref,created_at from coin_ledger "
                "where player_id=? and id>? order by id limit ?",
                (pid, after, limit + 1),
            ).fetchall()
            return {
                "coins": db.execute(
                    "select coins from wallets where player_id=?", (pid,)
                ).fetchone()[0],
                "transactions": [dict(r) for r in rows[:limit]],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def claimed_quests(self, pid: str) -> set[str]:
        return {
            r[0]
            for r in self._query(
                "select ref from coin_ledger where player_id=? and reason='quest'", (pid,)
            )
        }

    # ------------------------------------------------------------ friends
    def friend_code(self, pid: str) -> str:
        with self._tx() as db:
            self._ensure(db, pid)
            row = db.execute("select code from friend_codes where player_id = ?", (pid,)).fetchone()
            if row:
                return row[0]
            while True:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
                if not db.execute("select 1 from friend_codes where code = ?", (code,)).fetchone():
                    break
            db.execute("insert into friend_codes (player_id, code) values (?, ?)", (pid, code))
            return code

    @staticmethod
    def _pair(one: str, two: str) -> tuple[str, str]:
        return (one, two) if one < two else (two, one)

    def _friend(self, db: sqlite3.Connection, other: str, status: str) -> Friend:
        row = db.execute(
            "select display_name, avatar from homes where player_id = ?", (other,)
        ).fetchone()
        return Friend(
            player_id=other,
            display_name=row["display_name"] if row else "Player",
            avatar=row["avatar"] if row else None,
            status=status,
        )

    def request_friend(self, pid: str, code: str) -> Friend:
        with self._tx() as db:
            self._ensure(db, pid)
            row = db.execute(
                "select player_id from friend_codes where code = ?", (code.strip().upper(),)
            ).fetchone()
            if row is None:
                raise HomeError(404, "No player has that friend code.")
            return self._request_friend(db, pid, row[0])

    def request_friend_id(self, pid: str, other: str) -> Friend:
        with self._tx() as db:
            self._ensure(db, pid)
            if not db.execute("select 1 from homes where player_id=?", (other,)).fetchone():
                raise HomeError(404, "Player not found.")
            return self._request_friend(db, pid, other)

    def _request_friend(self, db: sqlite3.Connection, pid: str, other: str) -> Friend:
        if other == pid:
            raise HomeError(422, "That is your own friend code.")
        a, b = self._pair(pid, other)
        existing = db.execute(
            "select status, requested_by from friendships where a = ? and b = ?", (a, b)
        ).fetchone()
        if existing is None:
            db.execute(
                "insert into friendships (a, b, status, requested_by, created_at) "
                "values (?, ?, 'pending', ?, ?)",
                (a, b, pid, now()),
            )
            return self._friend(db, other, "outgoing")
        if existing["status"] == "accepted":
            return self._friend(db, other, "accepted")
        if existing["requested_by"] == pid:
            return self._friend(db, other, "outgoing")
        db.execute("update friendships set status = 'accepted' where a = ? and b = ?", (a, b))
        return self._friend(db, other, "accepted")

    def respond(self, pid: str, requester: str, accept: bool) -> Friend | None:
        """Accept or decline a pending request that `requester` sent to `pid`."""
        a, b = self._pair(pid, requester)
        with self._tx() as db:
            row = db.execute(
                "select 1 from friendships where a = ? and b = ? and status = 'pending' "
                "and requested_by = ? and requested_by != ?",
                (a, b, requester, pid),
            ).fetchone()
            if row is None:
                raise HomeError(404, "There is no such friend request.")
            if not accept:
                db.execute("delete from friendships where a = ? and b = ?", (a, b))
                return None
            db.execute("update friendships set status = 'accepted' where a = ? and b = ?", (a, b))
            return self._friend(db, requester, "accepted")

    def pending_friend_requests(self, pid: str) -> dict:
        with self._lock:
            result = {"incoming": [], "outgoing": []}
            for row in self._db.execute(
                "select a,b,requested_by,created_at from friendships "
                "where status='pending' and (a=? or b=?) order by created_at desc,a,b",
                (pid, pid),
            ).fetchall():
                other = row["b"] if row["a"] == pid else row["a"]
                direction = "outgoing" if row["requested_by"] == pid else "incoming"
                result[direction].append(
                    self._friend(self._db, other, direction).model_dump()
                    | {"requested_at": row["created_at"]}
                )
            return result

    def answer_friend_request(self, pid: str, other: str, action: str) -> Friend | None:
        """Natural pair-key idempotency; cancelling a request never removes an accepted friend."""
        if pid == other:
            raise HomeError(422, "Choose another player.")
        a, b = self._pair(pid, other)
        with self._tx() as db:
            row = db.execute(
                "select status,requested_by from friendships where a=? and b=?", (a, b)
            ).fetchone()
            if row is None:
                if action == "accept":
                    raise HomeError(404, "Friend request not found.")
                return None
            if row["status"] == "accepted":
                if action == "accept":
                    return self._friend(db, other, "accepted")
                raise HomeError(409, "Already friends. Use remove friend to end the friendship.")
            expected_sender = pid if action == "cancel" else other
            if row["requested_by"] != expected_sender:
                raise HomeError(403, "Only the recipient can accept/decline or the sender cancel.")
            if action == "accept":
                db.execute("update friendships set status='accepted' where a=? and b=?", (a, b))
                return self._friend(db, other, "accepted")
            db.execute("delete from friendships where a=? and b=?", (a, b))
            return None

    def remove_friend(self, pid: str, other: str) -> bool:
        """Remove a friend, or cancel a request either way."""
        a, b = self._pair(pid, other)
        with self._tx() as db:
            return db.execute("delete from friendships where a = ? and b = ?", (a, b)).rowcount > 0

    def list_friends(self, pid: str) -> list[Friend]:
        with self._lock:
            rows = self._db.execute(
                "select a, b, status, requested_by from friendships where a = ? or b = ?",
                (pid, pid),
            ).fetchall()
            friends = []
            for r in rows:
                other = r["b"] if r["a"] == pid else r["a"]
                if r["status"] == "accepted":
                    status = "accepted"
                else:
                    status = "outgoing" if r["requested_by"] == pid else "incoming"
                friends.append(self._friend(self._db, other, status))
            return sorted(friends, key=lambda f: (f.display_name.lower(), f.player_id))

    # ------------------------------------------------------------ visits and cheers
    def _may_visit(self, db: sqlite3.Connection, viewer: str, owner: str) -> bool:
        row = db.execute("select visibility from homes where player_id = ?", (owner,)).fetchone()
        if row is None:
            return False
        if viewer == owner:
            return True
        if row[0] != "friends":  # `family` admits only the owner until a family link exists
            return False
        a, b = self._pair(viewer, owner)
        return (
            db.execute(
                "select 1 from friendships where a = ? and b = ? and status = 'accepted'", (a, b)
            ).fetchone()
            is not None
        )

    def visit(self, viewer: str, owner: str) -> Home:
        with self._tx() as db:
            if viewer == owner:
                self._ensure(db, owner)
            if not self._may_visit(db, viewer, owner):
                raise HomeError(404, "We couldn't find that home.")
            return self._home(db, owner)

    def list_homes(self, viewer: str, scope: Literal["friends", "family"]) -> list[HomeCard]:
        if scope == "family":  # no family link exists yet
            return []
        with self._lock:
            cards = []
            for friend in self.list_friends(viewer):
                if friend.status != "accepted" or not self._may_visit(
                    self._db, viewer, friend.player_id
                ):
                    continue
                home = self._home(self._db, friend.player_id)
                cards.append(
                    HomeCard(
                        owner=home.owner,
                        rooms=home.summary.rooms,
                        levels=home.summary.levels,
                        max_levels=home.summary.max_levels,
                    )
                )
            return cards

    def cheer(self, viewer: str, owner: str, room_id: str) -> str:
        with self._tx() as db:
            if not self._may_visit(db, viewer, owner):
                raise HomeError(404, "We couldn't find that home.")
            if viewer == owner:
                raise HomeError(422, "You can't cheer your own home.")
            if room_id != HOME and room_id not in self._built(db, owner):
                raise HomeError(422, "That room is not built yet.")
            day = self.today()
            cursor = db.execute(
                "insert or ignore into home_cheers (visitor, owner, room_id, day) "
                "values (?, ?, ?, ?)",
                (viewer, owner, room_id, day),
            )
            if cursor.rowcount == 0:
                raise HomeError(409, "You already sent a cheer there today.")
            return day

    # ------------------------------------------------------------ deletion
    def _erase(self, db: sqlite3.Connection, pid: str) -> None:
        db.execute("delete from homes where player_id = ?", (pid,))  # rooms, items, cheers received
        db.execute("delete from equipment where player_id = ?", (pid,))
        db.execute("delete from wallets where player_id = ?", (pid,))  # and its ledger
        db.execute("delete from friend_codes where player_id = ?", (pid,))
        db.execute("delete from friendships where a = ? or b = ?", (pid, pid))
        db.execute("delete from home_cheers where visitor = ?", (pid,))

    def forget_player(self, pid: str) -> None:
        """Delete everything about a player: home, wallet, friend code, friendships and cheers."""
        with self._tx() as db:
            self._erase(db, pid)

    # ------------------------------------------------------------ demo data (used by home_seed.py only)
    def seed_home(
        self,
        pid: str,
        display_name: str,
        avatar: str | None,
        rooms: dict[str, list[int]],
        layout: dict[str, dict[str, Placement]] | None = None,
    ) -> None:
        """Replace a player's home with the given item levels (per room, in item order, 0 = not placed).

        Writes the rows directly and charges nothing: demo data, not a purchase history."""
        layout = layout or {}
        with self._tx() as db:
            self._erase(db, pid)
            self._ensure(db, pid)
            built = [r.id for r in RULES.rooms if r.id in rooms]
            if built != [r.id for r in RULES.rooms[: len(built)]]:
                raise ValueError("Seed rooms must be a prefix of the unlock order")
            stamp = now()
            for room_id in built:
                if room_id not in self._built(db, pid):
                    db.execute(
                        "insert into home_rooms (player_id, room_id, built_at) values (?, ?, ?)",
                        (pid, room_id, stamp),
                    )
                db.execute(
                    "delete from home_items where player_id = ? and room_id = ?", (pid, room_id)
                )
                for item, level in zip(RULES.room(room_id).items, rooms[room_id]):
                    if level <= 0:
                        continue
                    placement = layout.get(room_id, {}).get(item.id) or Placement()
                    x, y = placement.at if placement.at else (None, None)
                    db.execute(
                        "insert into home_items (player_id, room_id, item_id, level, x, y, rot) "
                        "values (?, ?, ?, ?, ?, ?, ?)",
                        (pid, room_id, item.id, level, x, y, placement.rot),
                    )
            db.execute(
                "update homes set display_name = ?, avatar = ? where player_id = ?",
                (display_name, avatar, pid),
            )

    def make_friends(self, one: str, two: str) -> None:
        a, b = self._pair(one, two)
        with self._tx() as db:
            self._ensure(db, one)
            self._ensure(db, two)
            db.execute(
                "insert or replace into friendships (a, b, status, requested_by, created_at) "
                "values (?, ?, 'accepted', ?, ?)",
                (a, b, one, now()),
            )
