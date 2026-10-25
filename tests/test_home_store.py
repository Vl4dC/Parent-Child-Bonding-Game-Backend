"""The home store: rules, spending, versions, friends, visits and cheers, with no HTTP in the way."""

import pytest

from learning_service.home_rules import RULES
from learning_service.home_store import HomeError, HomeStore, Placement

A = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b2"
C = "00000000-0000-4000-8000-0000000000c3"


@pytest.fixture
def store():
    s = HomeStore(":memory:")
    yield s
    s.close()


def set_coins(store, pid, coins):
    store.get_home(pid)
    with store._tx() as db:
        db.execute("update wallets set coins = ? where player_id = ?", (coins, pid))


def fill(store, pid, room_id, level):
    """Test shortcut: build `room_id` if needed and put every item at `level`, without paying."""
    store.get_home(pid)
    with store._tx() as db:
        if (
            db.execute(
                "select 1 from home_rooms where player_id = ? and room_id = ?", (pid, room_id)
            ).fetchone()
            is None
        ):
            db.execute(
                "insert into home_rooms (player_id, room_id, built_at) values (?, ?, 't')",
                (pid, room_id),
            )
        for item in RULES.room(room_id).items:
            db.execute(
                "insert or replace into home_items (player_id, room_id, item_id, level) "
                "values (?, ?, ?, ?)",
                (pid, room_id, item.id, level),
            )


def levels(home, room_id):
    return {i.id: i.level for r in home.rooms if r.id == room_id for i in r.items}


def item_of(home, room_id, item_id):
    return next(i for r in home.rooms if r.id == room_id for i in r.items if i.id == item_id)


def ledger_total(store, pid):
    return store._query("select sum(delta) from coin_ledger where player_id = ?", (pid,))[0][0]


def test_a_new_player_gets_the_living_room_starters_and_240_coins(store):
    home = store.get_home(A)
    assert home.coins == 240 and home.version == 1 and home.visibility == "friends"
    assert [r.id for r in home.rooms] == ["living"]
    assert levels(home, "living") == {"tv": 1, "sofa": 1}
    assert (home.summary.rooms, home.summary.levels, home.summary.max_levels) == (1, 2, 80)
    assert home.owner.id == A and home.owner.display_name == "Player" and home.cheers == {}
    assert ledger_total(store, A) == 240


def test_reading_a_home_twice_changes_nothing(store):
    store.get_home(A)
    again = store.get_home(A)
    assert (again.version, again.coins) == (1, 240)


def test_players_never_see_each_others_homes(store):
    store.get_home(A)
    set_coins(store, A, 5)
    assert store.get_home(B).coins == 240


def test_placing_an_item_costs_30_and_remembers_where(store):
    home = store.place_item(A, "living", "table", Placement(at=(1.2, 1.4), rot=1), None)
    table = item_of(home, "living", "table")
    assert (table.level, table.at, table.rot) == (1, (1.2, 1.4), 1)
    assert (home.coins, home.version) == (210, 2)
    assert ledger_total(store, A) == home.coins


def test_an_item_can_be_placed_on_its_default_spot(store):
    home = store.place_item(A, "living", "lamp", Placement(), None)
    assert item_of(home, "living", "lamp").at is None


@pytest.mark.parametrize(
    "room,item,status",
    [
        ("living", "tv", 422),  # a starter is already there
        ("kitchen", "stove", 422),  # room not built yet
        ("attic", "tv", 404),
        ("living", "piano", 404),
    ],
)
def test_a_refused_placement_changes_nothing(store, room, item, status):
    with pytest.raises(HomeError) as refused:
        store.place_item(A, room, item, Placement(), None)
    assert refused.value.status == status
    home = store.get_home(A)
    assert (home.coins, home.version) == (240, 1)


def test_placing_twice_is_refused(store):
    store.place_item(A, "living", "table", Placement(), None)
    with pytest.raises(HomeError) as refused:
        store.place_item(A, "living", "table", Placement(), None)
    assert refused.value.status == 422
    assert store.get_home(A).coins == 210


def test_not_enough_coins_is_a_402_and_changes_nothing(store):
    set_coins(store, A, 10)
    with pytest.raises(HomeError) as refused:
        store.place_item(A, "living", "table", Placement(), None)
    assert refused.value.status == 402 and refused.value.message == "You need 30 coins."
    home = store.get_home(A)
    assert (home.coins, home.version) == (10, 1)
    assert "table" not in levels(home, "living")


def test_upgrading_costs_20_times_the_next_level(store):
    store.upgrade_item(A, "living", "tv", None)  # to 2: 40
    home = store.upgrade_item(A, "living", "tv", None)  # to 3: 60
    assert levels(home, "living")["tv"] == 3
    assert (home.coins, home.version) == (140, 3)
    assert ledger_total(store, A) == 140


def test_upgrade_needs_a_placed_item_below_the_top_level(store):
    with pytest.raises(HomeError) as unplaced:
        store.upgrade_item(A, "living", "table", None)
    assert unplaced.value.status == 422
    fill(store, A, "living", RULES.max_level)
    with pytest.raises(HomeError) as top:
        store.upgrade_item(A, "living", "tv", None)
    assert top.value.status == 422


def test_building_the_next_room_needs_every_item_at_level_3(store):
    set_coins(store, A, 1000)
    with pytest.raises(HomeError) as locked:
        store.build_room(A, None)
    assert locked.value.status == 422 and "level 3" in locked.value.message
    fill(store, A, "living", 2)
    with pytest.raises(HomeError):
        store.build_room(A, None)
    fill(store, A, "living", 3)
    home = store.build_room(A, None)
    assert [r.id for r in home.rooms] == ["living", "kitchen"]
    assert levels(home, "kitchen") == {"fridge": 1, "stove": 1}
    assert home.coins == 750 and home.version == 2


def test_rooms_cost_250_500_750_and_then_there_are_no_more(store):
    set_coins(store, A, 5000)
    for room, cost in (("living", 250), ("kitchen", 500), ("bedroom", 750)):
        fill(store, A, room, 3)
        before = store.get_home(A).coins
        assert store.build_room(A, None).coins == before - cost
    fill(store, A, "bathroom", 3)
    with pytest.raises(HomeError) as full:
        store.build_room(A, None)
    assert full.value.status == 422 and "already built" in full.value.message


def test_a_room_you_cannot_afford_stays_unbuilt(store):
    fill(store, A, "living", 3)
    set_coins(store, A, 100)
    with pytest.raises(HomeError) as poor:
        store.build_room(A, None)
    assert poor.value.status == 402
    assert [r.id for r in store.get_home(A).rooms] == ["living"]


def test_a_stale_version_is_a_409_and_changes_nothing(store):
    with pytest.raises(HomeError) as stale:
        store.place_item(A, "living", "table", Placement(), 5)
    assert stale.value.status == 409
    assert store.get_home(A).coins == 240
    assert store.place_item(A, "living", "table", Placement(), 1).version == 2


def test_two_quick_taps_cannot_overspend(store):
    import threading

    set_coins(store, A, 40)  # enough for one upgrade (40), not for a second (60)
    results = []

    def tap():
        try:
            store.upgrade_item(A, "living", "tv", None)
            results.append("ok")
        except HomeError as error:
            results.append(error.status)

    threads = [threading.Thread(target=tap) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count(402) == 5
    home = store.get_home(A)
    assert (home.coins, levels(home, "living")["tv"]) == (0, 2)


def test_a_layout_replaces_the_rooms_whole_layout(store):
    store.place_item(A, "living", "table", Placement(), None)
    home = store.set_layout(
        A,
        "living",
        {"table": Placement(at=(2.0, 2.0), rot=2), "tv": Placement(at=None, rot=3)},
        None,
    )
    table, tv = item_of(home, "living", "table"), item_of(home, "living", "tv")
    assert (table.at, table.rot) == ((2.0, 2.0), 2)
    assert (tv.at, tv.rot) == (None, 3)
    assert (home.coins, home.version) == (210, 3)  # moving is free
    home = store.set_layout(A, "living", {"table": Placement(at=(1.0, 1.0))}, None)
    assert item_of(home, "living", "tv").rot == 0 and item_of(home, "living", "tv").at is None
    assert item_of(home, "living", "table").at == (1.0, 1.0)


def test_a_layout_only_names_placed_items_in_built_rooms(store):
    with pytest.raises(HomeError) as unplaced:
        store.set_layout(A, "living", {"lamp": Placement()}, None)
    assert unplaced.value.status == 422
    with pytest.raises(HomeError) as unknown:
        store.set_layout(A, "living", {"ghost": Placement()}, None)
    assert unknown.value.status == 422
    with pytest.raises(HomeError) as unbuilt:
        store.set_layout(A, "kitchen", {}, None)
    assert unbuilt.value.status == 422
    assert store.get_home(A).version == 1


def test_a_layout_with_a_stale_version_is_a_409(store):
    with pytest.raises(HomeError) as stale:
        store.set_layout(A, "living", {}, 9)
    assert stale.value.status == 409


def test_positions_must_stay_on_the_floor():
    with pytest.raises(ValueError):
        Placement(at=(2.8, 1.0))
    with pytest.raises(ValueError):
        Placement(at=(-0.1, 1.0))
    with pytest.raises(ValueError):
        Placement(at=(float("nan"), 1.0))
    with pytest.raises(ValueError):
        Placement(rot=4)


def test_visibility_and_the_card(store):
    home = store.set_visibility(A, "nobody", None)
    assert home.visibility == "nobody" and home.version == 2
    home = store.set_card(A, "Zara", "zara")
    assert (home.owner.display_name, home.owner.avatar, home.version) == ("Zara", "zara", 2)


def test_credits_are_paid_once_per_reference(store):
    assert store.credit(A, "quiz", [("set/0", 10), ("set/1", 10)]) == (260, 20)
    assert store.credit(A, "quiz", [("set/0", 10), ("set/1", 10)]) == (260, 0)
    assert store.credit(A, "quiz", [("set/1", 10), ("set/2", 10)]) == (270, 10)
    assert ledger_total(store, A) == 270


def befriend(store, one, two):
    store.request_friend(two, store.friend_code(one))
    store.respond(one, two, True)


def test_a_friend_code_is_stable_and_unique_per_player(store):
    code = store.friend_code(A)
    assert store.friend_code(A) == code and len(code) == 8
    assert store.friend_code(B) != code


def test_the_friend_flow_request_accept_remove(store):
    code = store.friend_code(A)
    request = store.request_friend(B, code.lower())  # codes are not case sensitive
    assert (request.player_id, request.status) == (A, "outgoing")
    assert [(f.player_id, f.status) for f in store.list_friends(A)] == [(B, "incoming")]
    assert store.request_friend(B, code).status == "outgoing"  # a repeat changes nothing
    assert store.respond(A, B, True).status == "accepted"
    assert [(f.player_id, f.status) for f in store.list_friends(B)] == [(A, "accepted")]
    assert store.remove_friend(B, A) is True
    assert store.list_friends(A) == [] and store.remove_friend(B, A) is False


def test_declining_removes_the_request(store):
    store.request_friend(B, store.friend_code(A))
    assert store.respond(A, B, False) is None
    assert store.list_friends(B) == []


def test_requests_that_cannot_work(store):
    with pytest.raises(HomeError) as own:
        store.request_friend(A, store.friend_code(A))
    assert own.value.status == 422
    with pytest.raises(HomeError) as unknown:
        store.request_friend(A, "NOSUCH00")
    assert unknown.value.status == 404
    with pytest.raises(HomeError) as nothing:
        store.respond(A, B, True)
    assert nothing.value.status == 404
    store.request_friend(B, store.friend_code(A))
    with pytest.raises(HomeError) as own_request:  # only the other side can answer
        store.respond(B, A, True)
    assert own_request.value.status == 404


def test_two_players_asking_each_other_become_friends(store):
    store.request_friend(B, store.friend_code(A))
    assert store.request_friend(A, store.friend_code(B)).status == "accepted"


@pytest.mark.parametrize(
    "visibility,friend_may_visit", [("friends", True), ("family", False), ("nobody", False)]
)
def test_who_may_visit(store, visibility, friend_may_visit):
    store.set_visibility(A, visibility, None)
    befriend(store, A, B)
    assert store.visit(A, A).owner.id == A  # the owner always may
    if friend_may_visit:
        home = store.visit(B, A)
        assert home.owner.id == A and not hasattr(home, "coins")
    else:
        with pytest.raises(HomeError) as refused:
            store.visit(B, A)
        assert refused.value.status == 404


def test_strangers_and_missing_homes_look_the_same(store):
    store.get_home(A)
    for owner in (A, "00000000-0000-4000-8000-0000000000ff"):
        with pytest.raises(HomeError) as refused:
            store.visit(B, owner)
        assert (refused.value.status, refused.value.message) == (
            404,
            "We couldn't find that home.",
        )


def test_a_pending_request_is_not_a_friendship(store):
    store.get_home(A)
    store.request_friend(B, store.friend_code(A))
    with pytest.raises(HomeError):
        store.visit(B, A)


def test_the_home_list_shows_visible_friends_only(store):
    for pid, name in ((A, "Ann"), (B, "Ben"), (C, "Cy")):
        store.set_card(pid, name, None)
    befriend(store, A, B)
    befriend(store, A, C)
    store.set_visibility(C, "nobody", None)
    assert [c.owner.display_name for c in store.list_homes(A, "friends")] == ["Ben"]
    assert store.list_homes(A, "family") == []
    card = store.list_homes(A, "friends")[0]
    assert (card.rooms, card.levels, card.max_levels) == (1, 2, 80)


def test_one_cheer_per_room_per_day(store):
    befriend(store, A, B)
    store.today = lambda: "2026-10-25"
    assert store.cheer(B, A, "home") == "2026-10-25"
    with pytest.raises(HomeError) as again:
        store.cheer(B, A, "home")
    assert again.value.status == 409
    assert store.cheer(B, A, "living") == "2026-10-25"  # another room is allowed
    store.today = lambda: "2026-10-26"
    assert store.cheer(B, A, "home") == "2026-10-26"  # a new day
    assert store.get_home(A).cheers == {"home": 2, "living": 1}


def test_cheers_follow_the_visit_rules(store):
    store.get_home(A)
    with pytest.raises(HomeError) as stranger:
        store.cheer(B, A, "home")
    assert stranger.value.status == 404
    with pytest.raises(HomeError) as own:
        store.cheer(A, A, "home")
    assert own.value.status == 422
    befriend(store, A, B)
    with pytest.raises(HomeError) as unbuilt:
        store.cheer(B, A, "kitchen")
    assert unbuilt.value.status == 422
    with pytest.raises(HomeError) as nonsense:
        store.cheer(B, A, "attic")
    assert nonsense.value.status == 422


def test_forgetting_a_player_removes_everything_about_them(store):
    befriend(store, A, B)
    store.place_item(A, "living", "table", Placement(), None)
    store.today = lambda: "2026-10-25"
    store.cheer(B, A, "home")
    store.cheer(A, B, "home")
    store.forget_player(A)
    assert store.list_friends(B) == []
    fresh = store.get_home(A)
    assert (fresh.coins, fresh.version, fresh.cheers) == (240, 1, {})
    assert store.get_home(B).cheers == {}
    assert store._query("select count(*) from coin_ledger where player_id = ?", (A,))[0][0] == 1
