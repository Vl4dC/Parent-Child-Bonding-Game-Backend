"""Load synthetic demo homes into the home database (HOME_DB_PATH, default home.db) and make them friends with one
real player: the `player_id` claim of a signed-in account's token.

    APP_ENV=development uv run python scripts/seed_home_demo.py <player-uuid>

Refuses APP_ENV=production. Demo players are made up; see learning_service/home_seed.py."""

import sys
from uuid import UUID

from app.config import Settings
from learning_service.home_seed import seed_demo
from learning_service.home_store import HomeStore


def main() -> int:
    try:
        player = str(UUID(sys.argv[1]))
    except (IndexError, ValueError):
        print(
            "Usage: seed_home_demo.py <player-uuid>  (the account's `player_id` claim)",
            file=sys.stderr,
        )
        return 2
    settings = Settings()
    if settings.app_env == "production":
        print(
            "Refusing to seed demo homes with APP_ENV=production. Set APP_ENV=development.",
            file=sys.stderr,
        )
        return 1
    path = settings.home_db_path or "home.db"
    store = HomeStore(path)
    try:
        result = seed_demo(store, player)
    finally:
        store.close()
    print(f"Seeded Zara, Mia, Leo and Sam into {path}.")
    print(
        f"Player {result['default_player']} is now their friend and has friend code {result['friend_code']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
