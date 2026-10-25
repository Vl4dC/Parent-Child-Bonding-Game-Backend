"""Demo seed data: four synthetic players, friends with the default player."""

import pytest

from learning_service.home_seed import DEFAULT_PLAYER, DEMO_PLAYERS, seed_demo
from learning_service.home_store import HomeStore

ZARA = "00000000-0000-4000-8000-000000000101"


@pytest.fixture
def seeded():
    store = HomeStore(":memory:")
    seed_demo(store)
    yield store
    store.close()


def test_the_demo_players_are_fixed_and_synthetic():
    assert [p.name for p in DEMO_PLAYERS] == ["Zara", "Mia", "Leo", "Sam"]
    assert DEMO_PLAYERS[0].player_id == ZARA


def test_zara_has_three_rooms_with_the_example_levels(seeded):
    home = seeded.visit(DEFAULT_PLAYER, ZARA)
    assert [r.id for r in home.rooms] == ["living", "kitchen", "bedroom"]
    assert (home.summary.rooms, home.summary.levels, home.summary.max_levels) == (3, 115, 240)
    assert home.owner.display_name == "Zara" and home.owner.avatar == "zara"
    armchair = next(i for r in home.rooms for i in r.items if i.id == "armchair")
    assert (armchair.at, armchair.rot) == ((2.2, 2.1), 3)


def test_the_default_player_is_friends_with_all_four(seeded):
    friends = seeded.list_friends(DEFAULT_PLAYER)
    assert {f.display_name for f in friends} == {"Zara", "Mia", "Leo", "Sam"}
    assert all(f.status == "accepted" for f in friends)
    assert len(seeded.list_homes(DEFAULT_PLAYER, "friends")) == 4


def test_seeding_twice_gives_the_same_result(seeded):
    seed_demo(seeded)
    assert len(seeded.list_friends(DEFAULT_PLAYER)) == 4
    assert seeded.visit(DEFAULT_PLAYER, ZARA).summary.levels == 115


def test_the_default_player_keeps_a_fresh_home_to_play_with(seeded):
    home = seeded.get_home(DEFAULT_PLAYER)
    assert (home.coins, [r.id for r in home.rooms]) == (240, ["living"])
