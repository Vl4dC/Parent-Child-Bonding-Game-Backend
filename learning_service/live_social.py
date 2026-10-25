"""Frontend-compatible social views and live duels sharing HomeStore friendships and coins."""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4

LIMITS = {
    "heartbeat_s": 20,
    "online_window_s": 45,
    "invite_ttl_s": 30,
    "start_delay_s": 5,
    "seconds_per_question": 20,
}
AVATARS = ("nova", "mia", "zara", "leo", "sam", "mom", "dad")
SCHEMA = """
create table if not exists live_social_users (
    player_id text primary key references homes(player_id) on delete cascade,
    subject text not null unique,
    issuer text not null,
    username text not null collate nocase unique,
    role text not null check(role in ('learner','parent'))
) strict;
create table if not exists live_social_request_ids (
    id text primary key,
    a text not null,
    b text not null,
    created_at text not null,
    unique(a,b,created_at),
    foreign key(a,b) references friendships(a,b) on delete cascade
) strict;
create table if not exists live_social_duels (
    id text primary key,
    challenger text not null references homes(player_id) on delete cascade,
    opponent text not null references homes(player_id) on delete cascade,
    lesson_id text not null,
    lesson_title text not null,
    source_revision integer not null,
    status text not null check(status in ('invited','declined','expired','cancelled','active','finished')),
    created_at integer not null,
    invite_expires_at integer not null,
    start_at integer,
    ends_at integer,
    finished_at integer,
    questions text not null,
    answer_key text not null,
    check(challenger<>opponent)
) strict;
create index if not exists live_social_duels_challenger on live_social_duels(challenger,status);
create index if not exists live_social_duels_opponent on live_social_duels(opponent,status);
create index if not exists live_social_duels_expiry on live_social_duels(status,invite_expires_at,ends_at);
create table if not exists live_social_answers (
    duel_id text not null references live_social_duels(id) on delete cascade,
    player_id text not null references homes(player_id) on delete cascade,
    idx integer not null,
    choice integer not null,
    answered_at integer not null,
    answer_number integer not null check(answer_number>0),
    primary key(duel_id,player_id,idx)
) strict, without rowid;
create table if not exists live_social_reservations (
    player_id text primary key references homes(player_id) on delete cascade,
    ticket text not null,
    expires_at integer not null
) strict;
create table if not exists live_social_events (
    player_id text not null references homes(player_id) on delete cascade,
    duel_id text not null,
    kind text not null,
    at text not null,
    primary key(player_id,duel_id,kind)
) strict, without rowid;
"""


class SocialError(Exception):
    def __init__(self, status, detail, code=None):
        self.status, self.detail, self.code = status, detail, code
        super().__init__(detail)


def clock():
    return time.time_ns() // 1_000_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms is not None else None


class LiveSocial:
    def __init__(self, home):
        self.home = home

    def registered(self, db, actor):
        found = db.execute(
            "select * from live_social_users where player_id=?", (actor.player_id,)
        ).fetchone()
        if found is None:
            raise SocialError(
                409, "Save your social profile before using friends or duels.", "profile_required"
            )
        if found["subject"] != actor.subject or found["issuer"] != actor.issuer:
            raise SocialError(403, "The session player mapping does not match this account.")
        return found

    def friend(self, db, one, other):
        return bool(
            db.execute(
                "select 1 from friendships where a=? and b=? and status='accepted'",
                self.home._pair(one, other),
            ).fetchone()
        )

    def settings(self, db, player):
        row = db.execute(
            "select share_presence,allow_game_invites from social_settings where player_id=?",
            (player,),
        ).fetchone()
        return tuple(map(bool, row)) if row else (True, True)

    def online(self, db, player, now):
        if not self.settings(db, player)[0]:
            return False
        return bool(
            db.execute(
                "select 1 from presence_sessions where player_id=? and last_seen>? limit 1",
                (player, now / 1000 - LIMITS["online_window_s"]),
            ).fetchone()
        )

    def busy(self, db, player):
        return bool(
            db.execute(
                "select 1 from live_social_duels where status in ('invited','active') "
                "and (challenger=? or opponent=?) limit 1",
                (player, player),
            ).fetchone()
        )

    def public_user(self, db, player, viewer, now):
        u = db.execute(
            "select u.*,h.display_name,h.avatar from live_social_users u join homes h using(player_id) where player_id=?",
            (player,),
        ).fetchone()
        if u is None:
            return {
                "id": player,
                "username": "unknown",
                "display_name": "Friend",
                "avatar": "nova",
                "online": None,
                "busy": None,
            }
        friend = player != viewer and self.friend(db, viewer, player)
        visible = self.settings(db, player)[0]
        return {
            "id": u["subject"],
            "username": u["username"],
            "display_name": u["display_name"],
            "avatar": u["avatar"] if u["avatar"] in AVATARS else "nova",
            "online": self.online(db, player, now) if friend else None,
            "busy": self.busy(db, player) if friend and visible else (False if friend else None),
        }

    def save_profile(self, actor, body):
        if not actor.username:
            raise SocialError(
                422,
                "Add a username claim to your Clerk session so friends can find you.",
                "no_username",
            )
        with self.home._tx() as db:
            existing = db.execute(
                "select * from live_social_users where player_id=? or subject=?",
                (actor.player_id, actor.subject),
            ).fetchall()
            if any(
                u["player_id"] != actor.player_id
                or u["subject"] != actor.subject
                or u["issuer"] != actor.issuer
                for u in existing
            ):
                raise SocialError(403, "The session player mapping does not match this account.")
            self.home._ensure(db, actor.player_id)
            try:
                db.execute(
                    "insert into live_social_users values (?,?,?,?,?) on conflict(player_id) do update set "
                    "username=excluded.username,role=excluded.role",
                    (actor.player_id, actor.subject, actor.issuer, actor.username, body["role"]),
                )
            except sqlite3.IntegrityError as exc:
                raise SocialError(
                    409, "This username is already linked to another player."
                ) from exc
            db.execute(
                "update homes set display_name=?,avatar=? where player_id=?",
                (body["display_name"], body["avatar"], actor.player_id),
            )
            return {"id": actor.subject, "username": actor.username, **body}

    def request_view(self, db, pair, viewer, now):
        record = db.execute(
            "select id from live_social_request_ids where a=? and b=? and created_at=?",
            (pair["a"], pair["b"], pair["created_at"]),
        ).fetchone()
        id = record[0] if record else "req_" + uuid4().hex
        if record is None:
            db.execute(
                "insert into live_social_request_ids values (?,?,?,?)",
                (id, pair["a"], pair["b"], pair["created_at"]),
            )
        sender = pair["requested_by"]
        recipient = pair["b"] if sender == pair["a"] else pair["a"]
        return {
            "id": id,
            "from": self.public_user(db, sender, viewer, now),
            "to": self.public_user(db, recipient, viewer, now),
            "created_at": pair["created_at"],
        }

    def search(self, actor, query, limit):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            escaped = query.lower().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            rows = db.execute(
                "select player_id from live_social_users where player_id<>? and username like ? escape '\\' order by username limit ?",
                (actor.player_id, escaped + "%", limit),
            ).fetchall()
            users = []
            for u in rows:
                pid = u[0]
                r = db.execute(
                    "select status,requested_by from friendships where a=? and b=?",
                    self.home._pair(actor.player_id, pid),
                ).fetchone()
                relationship = (
                    (
                        "friend"
                        if r["status"] == "accepted"
                        else "request_sent"
                        if r["requested_by"] == actor.player_id
                        else "request_received"
                    )
                    if r
                    else "none"
                )
                users.append(
                    self.public_user(db, pid, actor.player_id, now) | {"relationship": relationship}
                )
            return {"users": users}

    def friends(self, actor, online_only=False):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            out = {"friends": [], "incoming": [], "outgoing": []}
            for r in db.execute(
                "select * from friendships where a=? or b=?", (actor.player_id, actor.player_id)
            ).fetchall():
                other = r["b"] if r["a"] == actor.player_id else r["a"]
                if r["status"] == "accepted":
                    user = self.public_user(db, other, actor.player_id, now)
                    if not online_only or user["online"]:
                        out["friends"].append(user | {"since": r["created_at"]})
                else:
                    out["outgoing" if r["requested_by"] == actor.player_id else "incoming"].append(
                        self.request_view(db, r, actor.player_id, now)
                    )
            out["friends"].sort(
                key=lambda u: (not u["online"], u["display_name"].casefold(), u["id"])
            )
            return out

    def send_request(self, actor, username):
        username = username.strip()
        if username.startswith("@"):
            username = username[1:]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
            raise SocialError(422, "Enter a valid username.")
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            target = db.execute(
                "select player_id from live_social_users where username=? collate nocase",
                (username,),
            ).fetchone()
            if target is None:
                raise SocialError(404, "No player has that username.")
            other = target[0]
            if other == actor.player_id:
                raise SocialError(422, "You cannot request yourself.")
            pair = self.home._pair(actor.player_id, other)
            old = db.execute("select * from friendships where a=? and b=?", pair).fetchone()
            if old and (old["status"] == "accepted" or old["requested_by"] == actor.player_id):
                raise SocialError(409, "Already friends or already requested.")
            result = self.home._request_friend(db, actor.player_id, other)
            if result.status == "accepted":
                return 200, {
                    "status": "accepted",
                    "friend": self.public_user(db, other, actor.player_id, now),
                }
            saved = db.execute("select * from friendships where a=? and b=?", pair).fetchone()
            return 201, {
                "status": "sent",
                "request": self.request_view(db, saved, actor.player_id, now),
            }

    def answer_request(self, actor, request_id, action):
        with self.home._tx() as db:
            self.registered(db, actor)
            row = db.execute(
                "select f.* from live_social_request_ids r join friendships f on f.a=r.a and f.b=r.b and f.created_at=r.created_at where r.id=? and f.status='pending'",
                (request_id,),
            ).fetchone()
            if row is None or actor.player_id not in (row["a"], row["b"]):
                raise SocialError(404, "Friend request not found.")
            sender = row["requested_by"]
            recipient = row["b"] if sender == row["a"] else row["a"]
            if actor.player_id != (sender if action == "cancel" else recipient):
                raise SocialError(404, "Friend request not found.")
            if action == "accept":
                db.execute(
                    "update friendships set status='accepted' where a=? and b=?",
                    (row["a"], row["b"]),
                )
                return {"friend": self.public_user(db, sender, actor.player_id, clock())}
            db.execute("delete from friendships where a=? and b=?", (row["a"], row["b"]))
            return None

    def unfriend(self, actor, subject):
        with self.home._tx() as db:
            self.registered(db, actor)
            target = db.execute(
                "select player_id from live_social_users where subject=?", (subject,)
            ).fetchone()
            if target:
                db.execute(
                    "delete from friendships where a=? and b=? and status='accepted'",
                    self.home._pair(actor.player_id, target[0]),
                )

    def settle(self, db, now):
        db.execute(
            "update live_social_duels set status='expired' where status='invited' and invite_expires_at<=?",
            (now,),
        )
        for duel in db.execute(
            "select * from live_social_duels where status='active' and ends_at<=?", (now,)
        ).fetchall():
            self.finish(db, duel, duel["ends_at"])
        db.execute("delete from live_social_reservations where expires_at<=?", (now,))

    def sheet(self, db, duel, player):
        questions = json.loads(duel["questions"])
        key = json.loads(duel["answer_key"])
        answers = {
            r["idx"]: r["choice"]
            for r in db.execute(
                "select idx,choice from live_social_answers where duel_id=? and player_id=?",
                (duel["id"], player),
            )
        }
        results = [
            {
                "index": q["index"],
                "choice": answers.get(i),
                "correct": answers.get(i) == key[i]["answer_index"],
                **key[i],
            }
            for i, q in enumerate(questions)
        ]
        return {
            "score": sum(r["correct"] for r in results),
            "total": len(questions),
            "results": results,
        }

    def answered(self, db, duel_id, player):
        return db.execute(
            "select count(*) from live_social_answers where duel_id=? and player_id=?",
            (duel_id, player),
        ).fetchone()[0]

    def finish(self, db, duel, finished_at):
        from app.economy import PARTICIPATION_COINS
        from learning_service.home_rules import RULES

        changed = db.execute(
            "update live_social_duels set status='finished',finished_at=? where id=? and status='active'",
            (finished_at, duel["id"]),
        )
        if changed.rowcount == 0:
            return
        both = all(
            self.answered(db, duel["id"], p) > 0 for p in (duel["challenger"], duel["opponent"])
        )
        for player in (duel["challenger"], duel["opponent"]):
            if not self.answered(db, duel["id"], player):
                continue
            score = self.sheet(db, duel, player)["score"]
            credited = 0
            ref = "social_duel/" + duel["id"]
            for reason, amount in [
                ("participation", PARTICIPATION_COINS),
                ("duel", score * RULES.quiz_coins_per_correct),
            ]:
                if amount and self.home._ledger(db, player, amount, reason, ref, ignore=True):
                    credited += amount
            db.execute("update wallets set coins=coins+? where player_id=?", (credited, player))
            if both:
                db.execute(
                    "insert or ignore into live_social_events values (?,?,?,?)",
                    (player, duel["id"], "duel", iso(finished_at)),
                )

    def duel(self, db, duel_id, player):
        found = db.execute(
            "select * from live_social_duels where id=? and (challenger=? or opponent=?)",
            (duel_id, player, player),
        ).fetchone()
        if found is None:
            raise SocialError(404, "Duel not found.")
        return found

    def duel_view(self, db, duel, viewer, now):
        other = duel["opponent"] if viewer == duel["challenger"] else duel["challenger"]
        questions = json.loads(duel["questions"])
        started = (
            duel["status"] in ("active", "finished")
            and duel["start_at"] is not None
            and now >= duel["start_at"]
        )
        results = None
        if duel["status"] == "finished":
            me, them = self.sheet(db, duel, viewer), self.sheet(db, duel, other)
            results = {
                "me": me,
                "opponent": them,
                "winner": "draw"
                if me["score"] == them["score"]
                else "me"
                if me["score"] > them["score"]
                else "opponent",
            }
        return {
            "id": duel["id"],
            "status": duel["status"],
            "my_role": "challenger" if viewer == duel["challenger"] else "opponent",
            "challenger": self.public_user(db, duel["challenger"], viewer, now),
            "opponent": self.public_user(db, duel["opponent"], viewer, now),
            "lesson_title": duel["lesson_title"],
            "num_questions": len(questions),
            "created_at": iso(duel["created_at"]),
            "invite_expires_at": iso(duel["invite_expires_at"]),
            "start_at": iso(duel["start_at"]),
            "ends_at": iso(duel["ends_at"]),
            "server_now": iso(now),
            "questions": questions if started else None,
            "progress": {
                "me": self.answered(db, duel["id"], viewer),
                "opponent": self.answered(db, duel["id"], other),
            },
            "results": results,
        }

    def heartbeat(self, actor, state):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            session = "clerk:" + actor.session_id
            if state == "offline":
                db.execute(
                    "delete from presence_sessions where player_id=? and session_id=?",
                    (actor.player_id, session),
                )
            else:
                db.execute(
                    "delete from presence_sessions where player_id=? and last_seen<=?",
                    (actor.player_id, now // 1000 - 300),
                )
                db.execute(
                    "insert into presence_sessions values (?,?,?,?) on conflict(player_id,session_id) do update set status=excluded.status,last_seen=excluded.last_seen",
                    (actor.player_id, session, "online", now // 1000),
                )
                db.execute(
                    "insert into presence_seen values (?,?) on conflict(player_id) do update set last_seen=excluded.last_seen",
                    (actor.player_id, now // 1000),
                )
            invites = [
                self.duel_view(db, d, actor.player_id, now)
                for d in db.execute(
                    "select * from live_social_duels where opponent=? and status='invited' order by created_at,id",
                    (actor.player_id,),
                ).fetchall()
            ]
            return {
                "heartbeat_s": LIMITS["heartbeat_s"],
                "online_window_s": LIMITS["online_window_s"],
                "invites": invites,
            }

    def creation_check(self, db, actor, other, now, ticket=None):
        self.registered(db, actor)
        if not self.friend(db, actor.player_id, other):
            raise SocialError(403, "You can only challenge your friends.")
        if not self.settings(db, other)[1]:
            raise SocialError(
                409, "Your friend is not accepting game invitations.", "invites_disabled"
            )
        if not self.online(db, other, now):
            raise SocialError(409, "Your friend is offline right now.", "offline")
        for player in (actor.player_id, other):
            reservation = db.execute(
                "select ticket from live_social_reservations where player_id=? and expires_at>?",
                (player, now),
            ).fetchone()
            if self.busy(db, player) or (reservation and reservation[0] != ticket):
                raise SocialError(409, "One of the players is already in a duel.", "busy")

    def reserve_duel(self, actor, opponent_id):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            other = db.execute(
                "select player_id from live_social_users where subject=?", (opponent_id,)
            ).fetchone()
            if other is None:
                raise SocialError(403, "You can only challenge your friends.")
            other = other[0]
            self.creation_check(db, actor, other, now)
            ticket = uuid4().hex
            for player in (actor.player_id, other):
                db.execute(
                    "insert into live_social_reservations values (?,?,?)",
                    (player, ticket, now + 300_000),
                )
            return ticket, other

    def release(self, ticket):
        with self.home._tx() as db:
            db.execute("delete from live_social_reservations where ticket=?", (ticket,))

    def create_duel(self, actor, other, ticket, lesson, questions, key):
        with self.home._tx() as db:
            now = clock()
            self.settle(db, now)
            if (
                db.execute(
                    "select count(*) from live_social_reservations where ticket=? and expires_at>?",
                    (ticket, now),
                ).fetchone()[0]
                != 2
            ):
                raise SocialError(
                    409, "Question generation took too long; try inviting again.", "expired"
                )
            self.creation_check(db, actor, other, now, ticket)
            id = "duel_" + uuid4().hex
            db.execute(
                "insert into live_social_duels values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    actor.player_id,
                    other,
                    lesson.id,
                    lesson.lesson.title[:120],
                    lesson.revision,
                    "invited",
                    now,
                    now + LIMITS["invite_ttl_s"] * 1000,
                    None,
                    None,
                    None,
                    json.dumps(questions),
                    json.dumps(key),
                ),
            )
            db.execute("delete from live_social_reservations where ticket=?", (ticket,))
            return self.duel_view(db, self.duel(db, id, actor.player_id), actor.player_id, now)

    def get_duel(self, actor, id=None):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            if id is None:
                d = db.execute(
                    "select * from live_social_duels where status in ('invited','active') "
                    "and (challenger=? or opponent=?) order by created_at desc limit 1",
                    (actor.player_id, actor.player_id),
                ).fetchone()
            else:
                d = self.duel(db, id, actor.player_id)
            result = self.duel_view(db, d, actor.player_id, now) if d is not None else None
        # Commit timed-out games/rewards even when the current-duel lookup now returns 404.
        if result is None:
            raise SocialError(404, "No duel right now.")
        return result

    def action(self, actor, id, action):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            d = self.duel(db, id, actor.player_id)
            if actor.player_id != (d["challenger"] if action == "cancel" else d["opponent"]):
                raise SocialError(403, "That is not your duel to answer.")
            if d["status"] != "invited":
                raise SocialError(409, "This invitation is no longer pending.", d["status"])
            if action == "accept":
                if not self.friend(db, d["challenger"], d["opponent"]):
                    raise SocialError(409, "You are no longer friends.", "not_friends")
                start_at = now + LIMITS["start_delay_s"] * 1000
                end = (
                    start_at
                    + len(json.loads(d["questions"])) * LIMITS["seconds_per_question"] * 1000
                )
                db.execute(
                    "update live_social_duels set status='active',start_at=?,ends_at=? where id=?",
                    (start_at, end, id),
                )
            else:
                db.execute(
                    "update live_social_duels set status=? where id=?",
                    ("cancelled" if action == "cancel" else "declined", id),
                )
            return self.duel_view(db, self.duel(db, id, actor.player_id), actor.player_id, now)

    def answer(self, actor, id, index, choice):
        with self.home._tx() as db:
            self.registered(db, actor)
            now = clock()
            self.settle(db, now)
            d = self.duel(db, id, actor.player_id)
            questions = json.loads(d["questions"])
            if (
                index < 0
                or index >= len(questions)
                or choice < 0
                or choice >= len(questions[index]["options"])
            ):
                raise SocialError(422, "Unknown question or answer option.")
            saved = db.execute(
                "select choice,answer_number from live_social_answers where duel_id=? and player_id=? and idx=?",
                (id, actor.player_id, index),
            ).fetchone()
            if saved:
                if saved["choice"] != choice:
                    raise SocialError(409, "Your first answer cannot be changed.")
                return {"answered": saved["answer_number"]}
            if d["status"] != "active" or now < d["start_at"] or now >= d["ends_at"]:
                raise SocialError(409, "This duel is not taking answers right now.", d["status"])
            count = self.answered(db, id, actor.player_id) + 1
            db.execute(
                "insert into live_social_answers values (?,?,?,?,?,?)",
                (id, actor.player_id, index, choice, now, count),
            )
            if all(
                self.answered(db, id, p) == len(questions) for p in (d["challenger"], d["opponent"])
            ):
                self.finish(db, d, now)
            return {"answered": count}
