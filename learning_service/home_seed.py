"""
Synthetic demo homes, so the friends list, the visit screen and cheers have something to show.

These are made-up players with fixed ids, copied from the frontend's example homes (FRIEND_HOMES in
lib/home-data.ts). They are friends with the shared default player. Run explicitly (scripts/seed_home_demo.py);
nothing here runs at startup, and seeding again resets these four homes to the same state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from learning_service.home_store import HomeStore, Placement

DEFAULT_PLAYER = "00000000-0000-4000-8000-000000000001"  # app.player.DEFAULT_PLAYER_ID


@dataclass(frozen=True)
class DemoPlayer:
    player_id: str
    name: str
    avatar: str
    rooms: dict[str, list[int]]
    layout: dict[str, dict[str, Placement]] = field(default_factory=dict)


def _p(x: float, y: float, rot: int) -> Placement:
    return Placement(at=(x, y), rot=rot)


DEMO_PLAYERS = (
    DemoPlayer(
        "00000000-0000-4000-8000-000000000101",
        "Zara",
        "zara",
        {
            "living": [10, 9, 8, 10, 7, 9, 6, 9],
            "kitchen": [7, 6, 5, 6, 4, 5, 3, 4],
            "bedroom": [3, 2, 1, 0, 1, 0, 0, 0],
        },
        {
            "living": {
                "armchair": _p(2.2, 2.1, 3),
                "shelf": _p(2.3, 0, 3),
                "tv": _p(1.15, 0, 0),
            },
            "bedroom": {"bed": _p(0, 1.6, 0)},
        },
    ),
    DemoPlayer(
        "00000000-0000-4000-8000-000000000102",
        "Mia",
        "mia",
        {"living": [6, 5, 4, 3, 4, 3, 3, 2], "kitchen": [2, 1, 1, 0, 0, 0, 0, 0]},
        {
            "living": {
                "tv": _p(0, 1.4, 1),
                "sofa": _p(1.4, 0, 3),
                "table": _p(1.4, 1.2, 3),
                "rug": _p(1.4, 1.2, 1),
            }
        },
    ),
    DemoPlayer(
        "00000000-0000-4000-8000-000000000103",
        "Leo",
        "leo",
        {"living": [4, 3, 2, 1, 2, 0, 1, 0]},
    ),
    DemoPlayer(
        "00000000-0000-4000-8000-000000000104",
        "Sam",
        "sam",
        {"living": [2, 1, 0, 0, 0, 0, 0, 0]},
    ),
)


def seed_demo(store: HomeStore, default_player: str = DEFAULT_PLAYER) -> dict[str, str]:
    """Create the demo players and make them friends with `default_player`.

    Returns the default player's friend code, so a second demo account can add them."""
    store.get_home(default_player)
    for player in DEMO_PLAYERS:
        store.seed_home(player.player_id, player.name, player.avatar, player.rooms, player.layout)
        store.friend_code(player.player_id)
        store.make_friends(default_player, player.player_id)
    return {"default_player": default_player, "friend_code": store.friend_code(default_player)}
