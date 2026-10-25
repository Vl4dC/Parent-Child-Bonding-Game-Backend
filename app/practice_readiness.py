"""Read-only feature migration checks. No source data, migrations or model calls."""

OBJECTS = {
    "flashcard_attempts": {
        "tables": [
            "api_private.card_attempt_links",
            "api_private.card_attempt_history",
            "api_private.card_attempt_receipt_target",
        ],
        "functions": [
            "api_private.card_attempt_link_insert()",
            "api_private.card_attempt_source_deleted()",
        ],
        "triggers": [
            (
                "api_private.records",
                "card_attempt_link_insert",
                "api_private.card_attempt_link_insert()",
            ),
            (
                "api_private.card_attempt_links",
                "card_attempt_source_deleted",
                "api_private.card_attempt_source_deleted()",
            ),
        ],
    },
    "duels": {
        "tables": [
            "api_private.duel_matches",
            "api_private.duel_players",
            "api_private.duel_requests",
            "api_private.duel_room_latest",
            "api_private.duel_owner_history",
            "api_private.duel_player_user",
            "api_private.duel_action_actors",
        ],
        "functions": ["api_private.duel_invalidate_source()", "api_private.duel_forget_player()"],
        "triggers": [
            ("api_private.records", "duel_source_changed", "api_private.duel_invalidate_source()")
        ],
    },
}


def check(repository, settings, feature):
    enabled = getattr(settings, feature + "_enabled")
    if not enabled:
        return {"enabled": False, "ready": True, "state": "disabled", "reason": None}
    out = {"enabled": True, "ready": False, "state": "unavailable", "reason": "migration_required"}
    if repository is None:
        return out | {"reason": "storage_unavailable"}
    if not repository.durable:
        ready = settings.app_env in ("test", "development")
        return out | {
            "ready": ready,
            "state": "ready" if ready else "unavailable",
            "reason": None if ready else "durable_storage_required",
        }
    objects = OBJECTS[feature]
    try:
        with repository.control() as connection:
            ready = connection.execute(
                """select
              (select bool_and(to_regclass(n) is not null) from unnest(%s::text[]) n)
              and (select bool_and(to_regprocedure(n) is not null) from unnest(%s::text[]) n)
            """,
                (objects["tables"], objects["functions"]),
            ).fetchone()[0]
            for table, trigger, function in objects["triggers"]:
                ready = (
                    ready
                    and connection.execute(
                        """select exists(select 1 from pg_trigger
                  where tgrelid=to_regclass(%s) and tgname=%s and tgfoid=to_regprocedure(%s)
                    and tgenabled in ('O','A') and not tgisinternal)""",
                        (table, trigger, function),
                    ).fetchone()[0]
                )
    except Exception:
        return out | {"reason": "storage_unavailable"}
    return out | {
        "ready": bool(ready),
        "state": "ready" if ready else "unavailable",
        "reason": None if ready else "migration_required",
    }
