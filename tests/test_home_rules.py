"""The home's rules file must match the numbers and items in the frontend's lib/home-data.ts."""

from learning_service.home_rules import RULES, load_rules

STARTERS = {
    "living": {"tv", "sofa"},
    "kitchen": {"fridge", "stove"},
    "bedroom": {"bed", "nightstand"},
    "bathroom": {"toilet", "sink"},
}


def test_rooms_are_in_unlock_order():
    assert [r.id for r in RULES.rooms] == ["living", "kitchen", "bedroom", "bathroom"]


def test_every_room_has_eight_unique_items_and_the_frontend_starters():
    for room in RULES.rooms:
        ids = [i.id for i in room.items]
        assert len(ids) == 8 and len(set(ids)) == 8, room.id
        assert {i.id for i in room.items if i.starter} == STARTERS[room.id], room.id


def test_item_ids_repeat_across_rooms_so_they_are_keyed_by_room():
    assert RULES.room("living").item("plant") and RULES.room("kitchen").item("plant")
    assert RULES.room("kitchen").item("sink") and RULES.room("bathroom").item("sink")


def test_the_costs_match_the_frontend():
    assert (RULES.place_cost, RULES.max_level, RULES.unlock_level) == (30, 10, 3)
    assert (RULES.upgrade_cost(2), RULES.upgrade_cost(10)) == (40, 200)
    assert [RULES.build_cost(n) for n in (0, 1, 2, 3)] == [0, 250, 500, 750]
    assert (RULES.floor_tiles, RULES.starter_coins, RULES.quiz_coins_per_correct) == (2.7, 240, 10)


def test_lookups_return_none_for_unknown_ids():
    assert RULES.room("attic") is None
    assert RULES.room("living").item("piano") is None


def test_the_loader_reads_the_file_it_is_given(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(
        '{"source":"x","place_cost":1,"max_level":2,"unlock_level":1,"upgrade_cost_per_level":3,'
        '"build_cost_per_room":4,"floor_tiles":1.5,"starter_coins":5,"quiz_coins_per_correct":6,'
        '"rooms":[{"id":"living","name":"L","items":[{"id":"a","name":"A","starter":true}]}]}'
    )
    rules = load_rules(path)
    assert rules.upgrade_cost(2) == 6 and rules.rooms[0].items[0].starter is True
