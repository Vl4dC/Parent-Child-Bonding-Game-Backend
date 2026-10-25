"""
SQLite store for chat memory: who the players are, their conversations, and what was said in them.

One database file (MEMORY_DB_PATH, default memory.db; ":memory:" in tests) holds, for every player id:

  profiles       a role label (parent or learner) and a display name
  conversations  one row per chat: title, subject, topic, the PDF it was about, a rolling summary
  messages       what was said, in order, kept in full so a chat can be shown again and continued

Every query is scoped by player_id, so one player's memory is never visible through another's id. The role is a
label for the interface and for later features such as friend competitions; it proves nothing about who someone is
(there is no login). A competition can later read two players' digests from this one file.

The same file also holds the friend-competition challenges (the frozen content of a game made from topics players
studied, challenges.py): they are shared between the players of a room, so they cannot live in a per-player store.

This module only stores and reads. Deciding what to remember, summarising and filing by subject is memory.py.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Literal

from pydantic import BaseModel, Field

ROLES = ("parent", "learner")
Role = Literal["parent", "learner"]
MAX_CONVERSATIONS = 500  # per player
MAX_MESSAGES = 2000  # per conversation

SCHEMA = """
create table if not exists profiles (
    player_id text primary key,
    role text not null check (role in ('parent', 'learner')),
    display_name text not null,
    created_at text not null,
    updated_at text not null
);
create table if not exists conversations (
    id text primary key,
    player_id text not null,
    title text not null,
    subject text,
    topic text,
    material_id text,
    material_name text,
    summary text not null default '',
    summarized_upto integer not null default 0,
    message_count integer not null default 0,
    created_at text not null,
    updated_at text not null
);
create index if not exists conversations_by_player
    on conversations (player_id, subject, updated_at desc);
create table if not exists challenges (
    id text primary key,
    game text not null check (game in ('mcq', 'heads_up', 'diagram')),
    creator_id text not null,
    players text not null,
    room_id text,
    topic text not null,
    subject text,
    age integer not null,
    content text not null,
    created_at text not null,
    expires_at text not null
);
create table if not exists matches (
    id text primary key,
    challenge_id text not null,
    creator_id text not null,
    players text not null,
    doc text not null,
    created_at text not null,
    expires_at text not null
);
create table if not exists weekly_samples (
    week_key text not null,
    band text not null,
    game text not null,
    content text not null,
    created_at text not null,
    primary key (week_key, band, game)
);
create table if not exists messages (
    conversation_id text not null references conversations (id) on delete cascade,
    seq integer not null,
    role text not null check (role in ('user', 'assistant')),
    content text not null,
    used_pdf integer not null default 0,
    used_image integer not null default 0,
    pages text not null default '[]',
    created_at text not null,
    primary key (conversation_id, seq)
);
"""


class MemoryFull(Exception):
    """A limit was reached (too many chats, or a chat has too many messages)."""


class Profile(BaseModel):
    player_id: str
    role: Role
    display_name: str


class Conversation(BaseModel):
    id: str
    title: str
    subject: str | None = Field(
        None, description="Filed under this subject once the first answer is written"
    )
    topic: str | None = None
    material_id: str | None = None
    material_name: str | None = None
    summary: str = ""
    summarized_upto: int = 0
    message_count: int = 0
    created_at: str
    updated_at: str


class NewMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    used_pdf: bool = False
    used_image: bool = False
    pages: list[int] = []


class Message(NewMessage):
    seq: int
    created_at: str


class TopicDigest(BaseModel):
    conversation_id: str
    title: str
    messages: int
    last_studied: str


class SubjectDigest(BaseModel):
    subject: str
    conversations: int
    last_studied: str
    topics: list[TopicDigest]


class StudiedTopic(BaseModel):
    """One thing a player has studied: a chat that has had at least one answer."""

    subject: str
    topic: str
    conversation_id: str
    updated_at: str


class StoredChallenge(BaseModel):
    id: str
    game: Literal["mcq", "heads_up", "diagram"]
    creator_id: str
    players: list[str] = Field(
        ..., description="Who may read it: the creator and the room's members"
    )
    room_id: str | None = None
    topic: str
    subject: str | None = None
    age: int
    content: dict
    created_at: str
    expires_at: str


class Digest(BaseModel):
    """What a player has studied, compactly: subjects and topics, no chat text and no model call."""

    player_id: str
    role: Role | None = None
    display_name: str | None = None
    subjects: list[SubjectDigest]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class MemoryStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma foreign_keys = on")
        if path != ":memory:":
            self._db.execute("pragma journal_mode = wal")
        self._db.executescript(SCHEMA)

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

    # ------------------------------------------------------------ profiles
    def get_profile(self, player_id: str) -> Profile | None:
        rows = self._query(
            "select player_id, role, display_name from profiles where player_id = ?", (player_id,)
        )
        return Profile(**dict(rows[0])) if rows else None

    def set_profile(self, player_id: str, role: Role, display_name: str) -> Profile:
        stamp = now()
        with self._tx() as db:
            db.execute(
                "insert into profiles (player_id, role, display_name, created_at, updated_at) "
                "values (?, ?, ?, ?, ?) on conflict (player_id) do update set "
                "role = excluded.role, display_name = excluded.display_name, updated_at = excluded.updated_at",
                (player_id, role, display_name, stamp, stamp),
            )
        return Profile(player_id=player_id, role=role, display_name=display_name)

    # ------------------------------------------------------------ conversations
    def create_conversation(
        self,
        player_id: str,
        *,
        title: str = "New chat",
        material_id: str | None = None,
        material_name: str | None = None,
    ) -> Conversation:
        stamp = now()
        conversation_id = uuid.uuid4().hex
        with self._tx() as db:
            (count,) = db.execute(
                "select count(*) from conversations where player_id = ?", (player_id,)
            ).fetchone()
            if count >= MAX_CONVERSATIONS:
                raise MemoryFull(
                    f"At most {MAX_CONVERSATIONS} chats are kept. Delete an old one first."
                )
            db.execute(
                "insert into conversations (id, player_id, title, material_id, material_name, created_at, "
                "updated_at) values (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, player_id, title, material_id, material_name, stamp, stamp),
            )
        return self.get_conversation(player_id, conversation_id)

    def get_conversation(self, player_id: str, conversation_id: str) -> Conversation | None:
        rows = self._query(
            "select * from conversations where id = ? and player_id = ?",
            (conversation_id, player_id),
        )
        return (
            Conversation(**{k: rows[0][k] for k in rows[0].keys() if k != "player_id"})
            if rows
            else None
        )

    def list_conversations(
        self, player_id: str, subject: str | None = None, limit: int = 100
    ) -> list[Conversation]:
        sql = "select * from conversations where player_id = ?"
        args: list = [player_id]
        if subject is not None:
            sql += " and coalesce(subject, 'Other') = ?"
            args.append(subject)
        sql += " order by updated_at desc limit ?"
        args.append(limit)
        return [
            Conversation(**{k: row[k] for k in row.keys() if k != "player_id"})
            for row in self._query(sql, tuple(args))
        ]

    def update_conversation(
        self,
        player_id: str,
        conversation_id: str,
        *,
        title: str | None = None,
        subject: str | None = None,
        topic: str | None = None,
        material: tuple[str | None, str | None] | None = None,
    ) -> Conversation | None:
        """Change only what is given. `material` is (id, name) of the PDF the chat is about."""
        sets, args = [], []
        for column, value in (("title", title), ("subject", subject), ("topic", topic)):
            if value is not None:
                sets.append(f"{column} = ?")
                args.append(value)
        if material is not None:
            sets += ["material_id = ?", "material_name = ?"]
            args += [material[0], material[1]]
        if sets:
            with self._tx() as db:
                db.execute(
                    f"update conversations set {', '.join(sets)} where id = ? and player_id = ?",
                    (*args, conversation_id, player_id),
                )
        return self.get_conversation(player_id, conversation_id)

    def delete_conversation(self, player_id: str, conversation_id: str) -> bool:
        with self._tx() as db:
            cursor = db.execute(
                "delete from conversations where id = ? and player_id = ?",
                (conversation_id, player_id),
            )
            return cursor.rowcount > 0

    def forget_player(self, player_id: str) -> int:
        """Delete everything remembered about a player. Returns how many chats were deleted."""
        with self._tx() as db:
            cursor = db.execute("delete from conversations where player_id = ?", (player_id,))
            db.execute("delete from profiles where player_id = ?", (player_id,))
            db.execute("delete from challenges where creator_id = ?", (player_id,))
            db.execute("delete from matches where creator_id = ?", (player_id,))
            return cursor.rowcount

    # ------------------------------------------------------------ messages
    def append_turn(
        self, player_id: str, conversation_id: str, user: NewMessage, assistant: NewMessage
    ) -> Conversation:
        """Save a question and its answer together, so a chat never holds a question without its answer."""
        stamp = now()
        with self._tx() as db:
            row = db.execute(
                "select message_count from conversations where id = ? and player_id = ?",
                (conversation_id, player_id),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            count = row[0]
            if count + 2 > MAX_MESSAGES:
                raise MemoryFull("This chat is full. Start a new one.")
            for offset, message in enumerate((user, assistant), 1):
                db.execute(
                    "insert into messages (conversation_id, seq, role, content, used_pdf, used_image, "
                    "pages, created_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        count + offset,
                        message.role,
                        message.content,
                        int(message.used_pdf),
                        int(message.used_image),
                        json.dumps(message.pages),
                        stamp,
                    ),
                )
            db.execute(
                "update conversations set message_count = ?, updated_at = ? where id = ?",
                (count + 2, stamp, conversation_id),
            )
        return self.get_conversation(player_id, conversation_id)

    def messages(self, player_id: str, conversation_id: str, after_seq: int = 0) -> list[Message]:
        rows = self._query(
            "select m.seq, m.role, m.content, m.used_pdf, m.used_image, m.pages, m.created_at "
            "from messages m join conversations c on c.id = m.conversation_id "
            "where c.player_id = ? and m.conversation_id = ? and m.seq > ? order by m.seq",
            (player_id, conversation_id, after_seq),
        )
        return [
            Message(
                seq=r["seq"],
                role=r["role"],
                content=r["content"],
                used_pdf=bool(r["used_pdf"]),
                used_image=bool(r["used_image"]),
                pages=json.loads(r["pages"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def set_summary(
        self, player_id: str, conversation_id: str, summary: str, summarized_upto: int
    ) -> None:
        with self._tx() as db:
            db.execute(
                "update conversations set summary = ?, summarized_upto = ? where id = ? and player_id = ?",
                (summary, summarized_upto, conversation_id, player_id),
            )

    # ------------------------------------------------------------ topics, for finding what friends share
    def studied_topics(self, player_id: str) -> list[StudiedTopic]:
        """The chats this player has had answered, newest first, as (subject, topic) pairs."""
        return [
            StudiedTopic(
                subject=c.subject or "Other",
                topic=c.topic or c.title,
                conversation_id=c.id,
                updated_at=c.updated_at,
            )
            for c in self.list_conversations(player_id, limit=MAX_CONVERSATIONS)
            if c.message_count
        ]

    # ------------------------------------------------------------ challenges
    def save_challenge(self, challenge: StoredChallenge) -> None:
        with self._tx() as db:
            db.execute(
                "insert into challenges (id, game, creator_id, players, room_id, topic, subject, age, content, "
                "created_at, expires_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    challenge.id,
                    challenge.game,
                    challenge.creator_id,
                    json.dumps(challenge.players),
                    challenge.room_id,
                    challenge.topic,
                    challenge.subject,
                    challenge.age,
                    json.dumps(challenge.content),
                    challenge.created_at,
                    challenge.expires_at,
                ),
            )

    def get_challenge(self, player_id: str, challenge_id: str) -> StoredChallenge | None:
        """The challenge, if it exists, has not expired and this player may read it."""
        rows = self._query("select * from challenges where id = ?", (challenge_id,))
        if not rows:
            return None
        row = rows[0]
        players = json.loads(row["players"])
        if player_id not in players or row["expires_at"] <= now():
            return None
        return StoredChallenge(
            id=row["id"],
            game=row["game"],
            creator_id=row["creator_id"],
            players=players,
            room_id=row["room_id"],
            topic=row["topic"],
            subject=row["subject"],
            age=row["age"],
            content=json.loads(row["content"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def delete_challenge(self, player_id: str, challenge_id: str) -> bool:
        """Only the creator can delete a challenge."""
        with self._tx() as db:
            return (
                db.execute(
                    "delete from challenges where id = ? and creator_id = ?",
                    (challenge_id, player_id),
                ).rowcount
                > 0
            )

    def purge_expired_challenges(self) -> int:
        with self._tx() as db:
            return db.execute("delete from challenges where expires_at <= ?", (now(),)).rowcount

    # ------------------------------------------------------------ matches
    def save_match(
        self,
        match_id: str,
        challenge_id: str,
        creator_id: str,
        players: list[str],
        doc: dict,
        expires_at: str,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "insert into matches (id, challenge_id, creator_id, players, doc, created_at, expires_at) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id,
                    challenge_id,
                    creator_id,
                    json.dumps(players),
                    json.dumps(doc),
                    now(),
                    expires_at,
                ),
            )

    def get_match(self, player_id: str, match_id: str) -> dict | None:
        """The match document, if it exists, has not expired and this player is one of its players."""
        rows = self._query("select players, doc, expires_at from matches where id = ?", (match_id,))
        if (
            not rows
            or player_id not in json.loads(rows[0]["players"])
            or rows[0]["expires_at"] <= now()
        ):
            return None
        return json.loads(rows[0]["doc"])

    def mutate_match(self, player_id: str, match_id: str, change):
        """Read the match, let `change(doc)` modify it and return a result, and save it, all in one transaction so
        simultaneous players cannot overwrite each other. If `change` raises, nothing is saved. Returns the result,
        or None if the player may not see the match."""
        with self._tx() as db:
            row = db.execute(
                "select players, doc, expires_at from matches where id = ?", (match_id,)
            ).fetchone()
            if (
                row is None
                or player_id not in json.loads(row["players"])
                or row["expires_at"] <= now()
            ):
                return None
            doc = json.loads(row["doc"])
            result = change(doc)
            db.execute("update matches set doc = ? where id = ?", (json.dumps(doc), match_id))
            return result if result is not None else {}

    # ------------------------------------------------------------ weekly preview samples
    def get_weekly_sample(self, week_key: str, band: str, game: str) -> dict | None:
        rows = self._query(
            "select content from weekly_samples where week_key = ? and band = ? and game = ?",
            (week_key, band, game),
        )
        return json.loads(rows[0]["content"]) if rows else None

    def save_weekly_sample(self, week_key: str, band: str, game: str, content: dict) -> None:
        with self._tx() as db:
            db.execute(
                "insert or replace into weekly_samples (week_key, band, game, content, created_at) values (?, ?, ?, ?, ?)",
                (week_key, band, game, json.dumps(content), now()),
            )

    # ------------------------------------------------------------ digest
    def digest(self, player_id: str) -> Digest:
        profile = self.get_profile(player_id)
        subjects: dict[str, list[Conversation]] = {}
        for conversation in self.list_conversations(player_id, limit=MAX_CONVERSATIONS):
            if conversation.message_count:
                subjects.setdefault(conversation.subject or "Other", []).append(conversation)
        return Digest(
            player_id=player_id,
            role=profile.role if profile else None,
            display_name=profile.display_name if profile else None,
            subjects=sorted(
                (
                    SubjectDigest(
                        subject=name,
                        conversations=len(items),
                        last_studied=max(c.updated_at for c in items),
                        topics=[
                            TopicDigest(
                                conversation_id=c.id,
                                title=c.topic or c.title,
                                messages=c.message_count,
                                last_studied=c.updated_at,
                            )
                            for c in items
                        ],
                    )
                    for name, items in subjects.items()
                ),
                key=lambda s: s.last_studied,
                reverse=True,
            ),
        )
