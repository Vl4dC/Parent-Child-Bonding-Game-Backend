"""
The home's rules, read once from home_rules.json so no number is typed in two places.

The JSON is copied from the frontend's lib/home-data.ts (commit recorded in its `source` field): the rooms in
unlock order, the items of each room (a `starter` item starts at level 1 when its room is built), and the costs.
Room art, item looks and default spots are the frontend's business and are not stored here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

RULES_FILE = Path(__file__).with_name("home_rules.json")


@dataclass(frozen=True)
class ItemRule:
    id: str
    name: str
    starter: bool


@dataclass(frozen=True)
class RoomRule:
    id: str
    name: str
    items: tuple[ItemRule, ...]

    def item(self, item_id: str) -> ItemRule | None:
        return next((i for i in self.items if i.id == item_id), None)


@dataclass(frozen=True)
class Rules:
    source: str
    place_cost: int
    max_level: int
    unlock_level: int
    upgrade_cost_per_level: int
    build_cost_per_room: int
    floor_tiles: float
    starter_coins: int
    quiz_coins_per_correct: int
    rooms: tuple[RoomRule, ...]

    def room(self, room_id: str) -> RoomRule | None:
        return next((r for r in self.rooms if r.id == room_id), None)

    def upgrade_cost(self, next_level: int) -> int:
        return self.upgrade_cost_per_level * next_level

    def build_cost(self, rooms_built: int) -> int:
        return self.build_cost_per_room * rooms_built


def load_rules(path: Path = RULES_FILE) -> Rules:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rooms = tuple(
        RoomRule(id=r["id"], name=r["name"], items=tuple(ItemRule(**i) for i in r["items"]))
        for r in raw.pop("rooms")
    )
    return Rules(rooms=rooms, **raw)


RULES = load_rules()
