"""Fixed server catalog: current QuestNest looks plus the existing atelier-r6 wardrobe."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Slot = Literal[
    "look", "pet", "top", "jacket", "bottom", "shoes", "hat", "glasses", "earrings", "tie"
]


class ShopItem(BaseModel):
    id: str
    name: str
    slot: Slot
    price: int = Field(ge=0)
    asset_pack: str
    starter: bool = False


CATALOG = tuple(
    ShopItem(**row) for row in json.loads(Path(__file__).with_name("shop_catalog.json").read_text())
)
ITEMS = {item.id: item for item in CATALOG}
if len(ITEMS) != len(CATALOG):
    raise RuntimeError("Duplicate shop catalog ID")
