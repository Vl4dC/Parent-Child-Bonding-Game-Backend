"""Atomic owner-scoped practice units and deletion mirrors; no provider calls."""

from contextlib import contextmanager


@contextmanager
def practice_unit(repository, owner, *, write=False):
    if repository.durable:
        with repository.session(owner, serialize=write) as unit:
            yield unit
    else:
        with repository.lock:
            with repository.session(owner, serialize=write) as unit:
                yield unit


def cleanup_memory(repository, deleted_owner=None):
    if not getattr(repository, "card_practice_present", False) and not hasattr(
        repository, "duel_state"
    ):
        return
    for (owner, kind, key), data in list(repository.data.items()):
        if kind == "card_attempts" and (
            (owner, "decks", data["source_deck_id"]) not in repository.data
            or (owner, "lessons", data["lesson_id"]) not in repository.data
        ):
            del repository.data[(owner, kind, key)]
    for (owner, kind, key), data in list(repository.data.items()):
        if (
            kind == "card_attempt_keys"
            and (owner, "card_attempts", data["attempt_id"]) not in repository.data
        ):
            del repository.data[(owner, kind, key)]
    if hasattr(repository, "duel_state"):
        from app.duel_store import cleanup_memory as cleanup_duels

        cleanup_duels(repository, deleted_owner)
