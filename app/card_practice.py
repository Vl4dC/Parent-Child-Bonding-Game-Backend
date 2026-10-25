"""Independent, immutable-content flashcard attempts over saved cards. No AI calls."""

import copy
import hashlib
import json
import time
from uuid import uuid4

from fastapi import HTTPException

from app.practice_store import practice_unit
from learning_service.flashcards import DeckProgress, StoredDeck, review


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def timestamp():
    return time.time_ns() // 1_000_000


class CardPractice:
    def __init__(self, repository, *, clock=timestamp, attempt_limit=200):
        self.repository, self.clock, self.attempt_limit = repository, clock, attempt_limit
        if not repository.durable:
            repository.card_practice_present = True

    def source(self, unit, deck_id):
        source = unit.get("decks", deck_id, StoredDeck)
        if source is None or unit.get("lessons", source.lesson_id) is None:
            raise HTTPException(404, "Saved flashcard deck not found.")
        if not 1 <= len(source.cards) <= 100:
            raise HTTPException(422, "A practice deck must contain 1–100 saved cards.")
        return source

    def load(self, unit, attempt_id):
        attempt = unit.get("card_attempts", attempt_id)
        if attempt is None:
            raise HTTPException(404, "Flashcard attempt not found.")
        if (
            unit.get("decks", attempt["source_deck_id"]) is None
            or unit.get("lessons", attempt["lesson_id"]) is None
        ):
            raise HTTPException(404, "Saved flashcard source no longer exists.")
        return attempt

    def summary(self, attempt):
        progress = DeckProgress.model_validate(attempt["progress"])
        hard = sorted(progress.again, key=lambda card: (-progress.again[card], card))
        return {
            k: attempt[k]
            for k in (
                "id",
                "source_deck_id",
                "lesson_id",
                "title",
                "source_hash",
                "parent_id",
                "status",
                "version",
                "created_at",
                "completed_at",
                "abandoned_at",
            )
        } | {
            "state": {
                "next": progress.next,
                "known": len(progress.known),
                "total": len(attempt["cards"]),
                "reviews": progress.reviews,
                "hard": hard,
            }
        }

    def response(self, attempt):
        return self.summary(attempt) | {"cards": copy.deepcopy(attempt["cards"])}

    def list_rows(self, unit, deck_id, after="", limit=101):
        if unit.connection:
            return [
                row[0]
                for row in unit.connection.execute(
                    "select data from api_private.records where kind='card_attempts' "
                    "and data->>'source_deck_id'=%s and id>%s order by id limit %s",
                    (deck_id, after, limit),
                ).fetchall()
            ]
        return [
            copy.deepcopy(data)
            for (owner, kind, key), data in sorted(self.repository.data.items())
            if owner == unit.owner
            and kind == "card_attempts"
            and data["source_deck_id"] == deck_id
            and key > after
        ][:limit]

    def create(self, owner, deck_id, which, key, parent_id=None):
        with practice_unit(self.repository, owner, write=True) as unit:
            parent = self.load(unit, parent_id) if parent_id else None
            deck_id = parent["source_deck_id"] if parent else deck_id
            address = digest(["restart" if parent else "create", parent_id or deck_id, key])
            saved = unit.get("card_attempt_keys", address)
            if saved:
                if saved["which"] != which:
                    raise HTTPException(409, "Conflicting flashcard creation retry.")
                attempt = self.load(unit, saved["attempt_id"])
                return self.response(attempt)
            source = None if parent else self.source(unit, deck_id)
            lesson_id = parent["lesson_id"] if parent else source.lesson_id
            if len(self.list_rows(unit, deck_id, limit=self.attempt_limit)) >= self.attempt_limit:
                raise HTTPException(429, "Saved attempt limit reached for this deck.")
            if parent:
                cards = copy.deepcopy(parent["cards"])
                hard = DeckProgress.model_validate(parent["progress"]).again
                source_hash = parent["source_hash"]
                title = parent["title"]
            else:
                cards = source.public().model_dump(mode="json")["cards"]
                hard = source.progress.again
                source_hash = digest(
                    {
                        "lesson_id": source.lesson_id,
                        "title": source.title,
                        "cards": [c.model_dump(mode="json") for c in source.cards],
                    }
                )
                title = source.title
            if which == "hard":
                cards = [c for c in cards if c["index"] in hard]
            if not cards:
                raise HTTPException(409, "No previously difficult cards are available.")
            if len(json.dumps(cards).encode()) > 1_048_576:
                raise HTTPException(422, "The saved card snapshot exceeds the practice size limit.")
            now = self.clock()
            attempt = {
                "id": uuid4().hex,
                "source_deck_id": deck_id,
                "lesson_id": lesson_id,
                "title": title,
                "source_hash": source_hash,
                "cards": cards,
                "parent_id": parent_id,
                "status": "active",
                "version": 1,
                "created_at": now,
                "completed_at": None,
                "abandoned_at": None,
                "progress": DeckProgress(queue=[c["index"] for c in cards]).model_dump(mode="json"),
                "receipts": {},
            }
            unit.put("card_attempts", attempt["id"], attempt)
            if parent and parent["status"] == "active":
                parent.update(status="abandoned", abandoned_at=now, version=parent["version"] + 1)
            unit.put(
                "card_attempt_keys",
                address,
                {"attempt_id": attempt["id"], "which": which, "source_deck_id": deck_id},
            )
            return self.response(attempt)

    def get(self, owner, attempt_id, results=False):
        with practice_unit(self.repository, owner) as unit:
            attempt = self.load(unit, attempt_id)
            return self.summary(attempt) if results else self.response(attempt)

    def history(self, owner, deck_id, after, limit):
        with practice_unit(self.repository, owner) as unit:
            if unit.get("decks", deck_id) is None:
                raise HTTPException(404, "Saved flashcard deck not found.")
            rows = self.list_rows(unit, deck_id, after, limit + 1)
            return {
                "items": [self.summary(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def review(self, owner, attempt_id, body, key):
        with practice_unit(self.repository, owner, write=True) as unit:
            attempt = self.load(unit, attempt_id)
            address, fingerprint = digest(key), digest(body)
            saved = attempt["receipts"].get(address)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Conflicting flashcard review retry.")
                return saved["response"]
            if attempt["status"] != "active":
                raise HTTPException(409, "This flashcard attempt is no longer active.")
            if attempt["version"] != body["expected_version"]:
                raise HTTPException(409, "Flashcard attempt changed; reload it.")
            progress = DeckProgress.model_validate(attempt["progress"])
            if progress.next != body["card"]:
                raise HTTPException(409, "Review the current next card before advancing.")
            if len(attempt["receipts"]) >= 1000:
                raise HTTPException(429, "Flashcard attempt action limit reached.")
            progress = review(progress, body["card"], body["result"])
            attempt["progress"] = progress.model_dump(mode="json")
            attempt["version"] += 1
            if progress.next is None:
                attempt.update(status="completed", completed_at=self.clock())
            out = self.summary(attempt)
            attempt["receipts"][address] = {"fingerprint": fingerprint, "response": out}
            return out
