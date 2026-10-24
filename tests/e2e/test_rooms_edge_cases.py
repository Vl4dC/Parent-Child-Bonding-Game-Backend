"""Private lobbies: capacity, codes, host transfer, versions, expiry, privacy and races."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from tests.e2e.helpers import PARENT, as_owner, bug_on_postgres, child_headers, keys_anywhere

GUESTS = [f"00000000-0000-4000-8000-{n:012d}" for n in range(100, 112)]
SNAPSHOT_KEYS = {"id", "code", "host_id", "capacity", "version", "closed", "expires_at", "members"}


def create(api, name="Alex", capacity=4, headers=None):
    r = api.post(
        "/api/rooms", json={"display_name": name, "capacity": capacity}, headers=headers or {}
    )
    assert r.status_code == 201, r.text
    return r.json()


def join(repository, owner, code, name="Guest"):
    return as_owner(repository, owner, "join", payload={"code": code, "display_name": name})


def refused(call, *args, **kwargs):
    with pytest.raises(HTTPException) as error:
        call(*args, **kwargs)
    return error.value.status_code


def command(api, room, action, **extra):
    return api.post(
        f"/api/rooms/{room['id']}/commands",
        json={"expected_version": room["version"], "action": action, **extra},
    )


# ---------------------------------------------------------------- creating
@pytest.mark.parametrize(
    "body,ok",
    [
        ({"display_name": "A", "capacity": 2}, True),
        ({"display_name": "A", "capacity": 8}, True),
        ({"display_name": "A"}, True),
        ({"display_name": "A", "capacity": 1}, False),
        ({"display_name": "A", "capacity": 9}, False),
        ({"display_name": "A", "capacity": 0}, False),
        ({"display_name": "A", "capacity": -4}, False),
        ({"display_name": "A", "capacity": 4.5}, False),
        ({"display_name": "A", "capacity": None}, False),
        ({"display_name": "A", "capacity": [4]}, False),
        ({"display_name": ""}, False),
        ({"display_name": "   "}, False),
        ({"display_name": "a" * 60}, True),
        ({"display_name": "a" * 61}, False),
        ({"display_name": "🦊" * 60}, True),
        ({"display_name": None}, False),
        ({"display_name": 7}, False),
        ({}, False),
        ({"display_name": "A", "host_id": PARENT}, False),
        ({"display_name": "A", "code": "AAAAAAAA"}, False),
        ({"display_name": "A", "members": []}, False),
        ({"display_name": "A", "version": 99}, False),
    ],
)
def test_room_creation_boundaries(api, body, ok):
    assert (api.post("/api/rooms", json=body).status_code == 201) is ok


def test_a_new_room_has_exactly_the_documented_shape_and_defaults(api):
    room = create(api)
    assert set(room) == SNAPSHOT_KEYS
    assert (room["capacity"], room["version"], room["closed"], room["host_id"]) == (
        4,
        1,
        False,
        PARENT,
    )
    assert room["members"] == [{"user_id": PARENT, "display_name": "Alex", "ready": False}]
    assert not {
        "email",
        "token",
        "access_token",
        "refresh_token",
        "answer",
        "answers",
        "password",
    } & keys_anywhere(room)


def test_room_codes_are_eight_characters_and_unique(api):
    codes = [create(api)["code"] for _ in range(40)]
    assert all(re.fullmatch(r"[A-Z0-9]{8}", c) for c in codes) and len(set(codes)) == len(codes)


def test_room_codes_use_the_full_documented_alphabet(api, repository, request):
    bug_on_postgres(
        request, repository, "BUG-11: PostgreSQL invite codes are 8 hex characters (32 bits)"
    )
    symbols = set("".join(create(api)["code"] for _ in range(40)))
    assert (
        len(symbols) > 16
    )  # a 32-symbol alphabet: hex-only codes would never exceed 16 distinct symbols


@pytest.mark.xfail(
    strict=True,
    reason="BUG-05: room display names are stored untrimmed (profiles and registration trim)",
)
def test_room_display_names_are_trimmed(api):
    assert create(api, name="  Alex  ")["members"][0]["display_name"] == "Alex"


def test_markup_in_a_room_name_stays_inert_text(api, repository):
    name = "<script>alert(1)</script>"
    room = create(api, name=name)
    assert api.get(f"/api/rooms/{room['id']}").json()["members"][0]["display_name"] == name
    assert api.get(f"/api/rooms/{room['id']}").headers["content-type"] == "application/json"


# ---------------------------------------------------------------- joining
@pytest.mark.parametrize(
    "code",
    [
        "abcdefgh",
        "ABCDEFG",
        "ABCDEFGHI",
        "ABCDEFG\n",
        "ABCDEFG ",
        " ABCDEFG",
        "ABCD-EFG",
        "ÀBCDEFGH",
        "",
        None,
        12345678,
        ["ABCDEFGH"],
    ],
)
def test_malformed_codes_are_422_and_do_not_reach_storage(api, code):
    assert (
        api.post("/api/rooms/join", json={"code": code, "display_name": "Sam"}).status_code == 422
    )


def test_unknown_codes_are_404_whatever_their_shape(api):
    create(api)
    for code in ("ZZZZZZZZ", "00000000", "AAAAAAAA"):
        assert (
            api.post(
                "/api/rooms/join",
                headers=child_headers(api),
                json={"code": code, "display_name": "Sam"},
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "name,ok",
    [("S", True), ("s" * 60, True), ("s" * 61, False), ("", False), ("  ", False), (None, False)],
)
def test_joining_needs_a_real_display_name(api, name, ok):
    room = create(api)
    r = api.post(
        "/api/rooms/join",
        headers=child_headers(api),
        json={"code": room["code"], "display_name": name},
    )
    assert (r.status_code == 200) is ok


def test_joining_twice_is_harmless_and_keeps_the_first_name(api):
    room = create(api)
    other = child_headers(api)
    first = api.post(
        "/api/rooms/join", headers=other, json={"code": room["code"], "display_name": "Sam"}
    ).json()
    again = api.post(
        "/api/rooms/join", headers=other, json={"code": room["code"], "display_name": "Impostor"}
    ).json()
    assert (
        first == again
        and again["version"] == 2
        and [m["display_name"] for m in again["members"]] == ["Alex", "Sam"]
    )
    host_again = api.post(
        "/api/rooms/join", json={"code": room["code"], "display_name": "Alex2"}
    ).json()
    assert host_again == again


def test_capacity_is_never_exceeded(api, repository):
    room = create(api, capacity=3)
    join(repository, GUESTS[0], room["code"])
    full = join(repository, GUESTS[1], room["code"])
    assert len(full["members"]) == 3 and full["version"] == 3
    assert refused(join, repository, GUESTS[2], room["code"]) == 409
    assert (
        api.get(f"/api/rooms/{room['id']}").json()["version"] == 3
    )  # a refused join changes nothing
    assert (
        join(repository, GUESTS[0], room["code"])["version"] == 3
    )  # members already inside can still "join"


def test_the_largest_room_holds_exactly_eight(api, repository):
    room = create(api, capacity=8)
    for guest in GUESTS[:7]:
        join(repository, guest, room["code"])
    assert refused(join, repository, GUESTS[7], room["code"]) == 409
    assert len(api.get(f"/api/rooms/{room['id']}").json()["members"]) == 8


def test_racing_for_the_last_seat_has_one_winner(api, repository):
    room = create(api, capacity=4)
    for guest in GUESTS[:2]:
        join(repository, guest, room["code"])

    def try_join(guest):
        try:
            join(repository, guest, room["code"])
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(try_join, GUESTS[2:8]))
    assert sorted(codes) == [200, 409, 409, 409, 409, 409]
    assert len(api.get(f"/api/rooms/{room['id']}").json()["members"]) == 4


# ---------------------------------------------------------------- commands and versions
def test_every_state_change_bumps_the_version_by_exactly_one(api, repository):
    room = create(api)
    seen = [room["version"]]
    guest_view = join(repository, GUESTS[0], room["code"])
    seen.append(guest_view["version"])
    for ready in (True, True, False):
        room = api.post(
            f"/api/rooms/{room['id']}/commands",
            json={"expected_version": seen[-1], "action": "ready", "ready": ready},
        ).json()
        seen.append(room["version"])
    assert seen == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "version,status",
    [(0, 422), (-1, 422), (1.5, 422), (None, 422), ("x", 422), (99, 409), (2, 409)],
)
def test_stale_and_invalid_versions_never_change_a_room(api, version, status):
    room = create(api)
    r = api.post(
        f"/api/rooms/{room['id']}/commands",
        json={"expected_version": version, "action": "ready", "ready": True},
    )
    assert r.status_code == status
    assert api.get(f"/api/rooms/{room['id']}").json() == room


def test_an_absurdly_large_version_is_a_conflict_not_a_server_error(api, repository, request):
    bug_on_postgres(
        request,
        repository,
        "BUG-02: expected_version beyond the SQL integer range becomes a 503 on PostgreSQL",
    )
    room = create(api)
    r = api.post(
        f"/api/rooms/{room['id']}/commands",
        json={"expected_version": 10**30, "action": "ready", "ready": True},
    )
    assert r.status_code in (409, 422)


@pytest.mark.parametrize(
    "body",
    [
        {"action": "kick"},
        {"action": "READY"},
        {"action": ""},
        {"action": None},
        {},
        {"action": "ready", "host": "x"},
        {"action": "ready", "ready": "perhaps"},
        {"action": "close", "force": True},
    ],
)
def test_command_bodies_are_strict(api, body):
    room = create(api)
    assert (
        api.post(
            f"/api/rooms/{room['id']}/commands", json={"expected_version": 1, **body}
        ).status_code
        == 422
    )


def test_only_members_can_issue_commands_or_read_snapshots(api, repository):
    room = create(api)
    outsider = child_headers(api)
    for action in ("ready", "leave", "close"):
        r = api.post(
            f"/api/rooms/{room['id']}/commands",
            headers=outsider,
            json={"expected_version": 1, "action": action},
        )
        assert r.status_code == 404, action
    assert api.get(f"/api/rooms/{room['id']}", headers=outsider).status_code == 404
    assert api.get(f"/api/rooms/{room['id']}").json() == room  # nothing changed


def test_only_the_host_closes_and_a_closed_room_freezes_readiness_and_joins(api, repository):
    room = create(api)
    guest = join(repository, GUESTS[0], room["code"])
    assert (
        refused(
            as_owner,
            repository,
            GUESTS[0],
            "close",
            room["id"],
            {"expected_version": guest["version"]},
        )
        == 403
    )
    closed = command(api, guest, "close").json()
    assert closed["closed"] and closed["version"] == guest["version"] + 1
    assert command(api, closed, "ready", ready=True).status_code == 409
    assert refused(join, repository, GUESTS[1], room["code"]) == 409
    assert command(api, closed, "leave").json() == {"left": True}


def test_host_transfer_follows_join_order_and_the_last_one_out_deletes_the_room(api, repository):
    room = create(api, capacity=4)
    for guest in GUESTS[:2]:
        join(repository, guest, room["code"])
    snap = api.get(f"/api/rooms/{room['id']}").json()
    # A non-host leaving does not change the host.
    left = as_owner(
        repository, GUESTS[1], "leave", room["id"], {"expected_version": snap["version"]}
    )
    assert left == {"left": True}
    snap = api.get(f"/api/rooms/{room['id']}").json()
    assert snap["host_id"] == PARENT and [m["user_id"] for m in snap["members"]] == [
        PARENT,
        GUESTS[0],
    ]
    # The host leaving hands over to the earliest remaining member.
    assert command(api, snap, "leave").json() == {"left": True}
    snap = as_owner(repository, GUESTS[0], "snapshot", room["id"])
    assert snap["host_id"] == GUESTS[0] and len(snap["members"]) == 1
    as_owner(repository, GUESTS[0], "leave", room["id"], {"expected_version": snap["version"]})
    assert api.get(f"/api/rooms/{room['id']}").status_code == 404
    assert (
        api.post(
            "/api/rooms/join",
            headers=child_headers(api),
            json={"code": room["code"], "display_name": "Sam"},
        ).status_code
        == 404
    )


def test_leaving_removes_all_access_immediately_and_rejoining_works_while_open(api, repository):
    room = create(api)
    guest = join(repository, GUESTS[0], room["code"])
    command(api, guest, "leave")
    assert api.get(f"/api/rooms/{room['id']}").status_code == 404
    assert (
        api.post(
            f"/api/rooms/{room['id']}/commands",
            json={"expected_version": 3, "action": "ready", "ready": True},
        ).status_code
        == 404
    )
    back = api.post(
        "/api/rooms/join", json={"code": room["code"], "display_name": "Alex again"}
    ).json()
    assert back["host_id"] == GUESTS[0] and len(back["members"]) == 2


def test_rooms_are_independent_and_deleting_account_data_leaves_all_of_them(api, repository):
    rooms = [create(api, name=f"Room {n}") for n in range(3)]
    shared = join(repository, GUESTS[0], rooms[0]["code"])
    api.delete("/api/me/data")
    assert (
        api.get(f"/api/rooms/{rooms[1]['id']}").status_code == 404
        and api.get(f"/api/rooms/{rooms[2]['id']}").status_code == 404
    )
    survivor = as_owner(repository, GUESTS[0], "snapshot", rooms[0]["id"])
    assert survivor["host_id"] == GUESTS[0] and [m["user_id"] for m in survivor["members"]] == [
        GUESTS[0]
    ]
    assert shared["version"] == 2


# ---------------------------------------------------------------- expiry
def expire(repository, room_id):
    if repository.durable:
        with repository.control() as connection:
            connection.execute(
                "update api_private.rooms set state=jsonb_set(state,'{expires_at}',to_jsonb((now()-interval '1 second')::text)) where id=%s",
                (room_id,),
            )
    else:
        repository.rooms[room_id]["expires_at"] = "2000-01-01T00:00:00+00:00"


def test_an_expired_room_vanishes_for_everyone(api, repository):
    room = create(api)
    join(repository, GUESTS[0], room["code"])
    expire(repository, room["id"])
    assert api.get(f"/api/rooms/{room['id']}").status_code == 404
    assert (
        api.post(
            f"/api/rooms/{room['id']}/commands", json={"expected_version": 2, "action": "leave"}
        ).status_code
        == 404
    )
    assert (
        api.post(
            "/api/rooms/join",
            headers=child_headers(api),
            json={"code": room["code"], "display_name": "Sam"},
        ).status_code
        == 404
    )
    assert refused(as_owner, repository, GUESTS[0], "snapshot", room["id"]) == 404


def test_a_room_lives_for_about_a_day(api):
    from datetime import datetime, timedelta, timezone

    room = create(api)
    left = datetime.fromisoformat(room["expires_at"].replace("Z", "+00:00")) - datetime.now(
        timezone.utc
    )
    assert timedelta(hours=23, minutes=59) < left <= timedelta(hours=24, minutes=1)
