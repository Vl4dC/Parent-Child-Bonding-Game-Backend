"""Upgrade/rollback proof on the disposable PostgreSQL fixture; no hosted database."""

from pathlib import Path

import pytest

OWNER = "00000000-0000-4000-8000-000000000001"


def test_upgrade_from_migration_009_preserves_legacy_records_and_rolls_back(repository):
    if not repository.durable:
        pytest.skip("Migration upgrade proof requires PostgreSQL")
    with repository.session(OWNER) as unit:
        unit.put("profile", "self", {"display_name": "Legacy retained"})
    with repository.control() as c:
        original = c.execute(
            "select to_regclass('api_private.records')::oid,to_regclass('api_private.rooms')::oid,to_regprocedure('api_private.diagram_invalidate_lesson()')::oid"
        ).fetchone()
        with c.transaction(force_rollback=True):
            for name in [
                "card_attempt_link_insert",
                "card_attempt_source_deleted",
                "duel_invalidate_source",
                "duel_forget_player",
            ]:
                c.execute("drop function api_private." + name + "() cascade")
            c.execute(
                "drop table api_private.card_attempt_links,api_private.duel_requests,api_private.duel_players,api_private.duel_matches cascade"
            )
            c.execute(
                "drop index api_private.card_attempt_history,api_private.card_attempt_receipt_target"
            )
            assert c.execute(
                "select to_regclass('api_private.duel_matches'),to_regclass('api_private.card_attempt_links')"
            ).fetchone() == (None, None)
            for filename in ["20261025001000_card_attempts.sql", "20261025001100_duels.sql"]:
                c.execute((Path("supabase/migrations") / filename).read_text())
            assert (
                c.execute(
                    "select to_regclass('api_private.records')::oid,to_regclass('api_private.rooms')::oid,to_regprocedure('api_private.diagram_invalidate_lesson()')::oid"
                ).fetchone()
                == original
            )
            assert (
                c.execute(
                    "select data->>'display_name' from api_private.records where owner_id=%s and kind='profile' and id='self'",
                    (OWNER,),
                ).fetchone()[0]
                == "Legacy retained"
            )
            assert c.execute(
                "select has_table_privilege('authenticated','api_private.duel_matches','SELECT'),has_table_privilege('authenticated','api_private.card_attempt_links','SELECT')"
            ).fetchone() == (False, False)
            assert (
                c.execute(
                    "select count(*) from pg_trigger where tgname in ('card_attempt_link_insert','duel_source_changed') and tgenabled='O'"
                ).fetchone()[0]
                == 2
            )
        # The test's temporary DDL is rolled back: the original applied feature objects remain.
        assert c.execute(
            "select to_regclass('api_private.duel_matches') is not null,to_regclass('api_private.card_attempt_links') is not null"
        ).fetchone() == (True, True)
    with repository.session(OWNER, serialize=False) as unit:
        assert unit.get("profile", "self")["display_name"] == "Legacy retained"
