# Home storage, wallet and friends: design

Status: approved by the user on 2026-10-25. Implements the "QuestNest Home Storage" proposal against
this backend. Frontend evidence: `RahulRohidasPatil/Parent-Child-Bonding-Game` `main` at `ceb8069`
(`lib/home-data.ts`, `lib/store.ts`, base, visit, friends, shop, quests and quiz screens).

## Goal

Save each player's home on the server (rooms built, item levels, item positions), charge coins for
building, placing and upgrading in one transaction, let players befriend each other by code, and let
friends visit a home read-only and cheer it. Everything the app can derive (room art, item looks,
default spots, final positions) stays in the frontend; the backend stores only what a player chose.

## Boundaries (from AGENTS.md)

- No login. `X-Player-ID` selects a data partition. It is not identity. Friend and visit rules below
  are an advisory privacy setting, never described as access control.
- `family` visibility admits only the owner until a real family link exists. It never widens access.
- Parent approval of friends (shown in the frontend copy) cannot be enforced and is not implemented.
- Demo mode stays off by default; the coin top-up route exists only when it is on.
- Keep `api_private` untouched. No migration, no PostgreSQL change: this feature is SQLite only.

## Storage

A new SQLite file, built like `learning_service/memory_store.py` (one connection, an `RLock`,
`BEGIN IMMEDIATE` transactions, foreign keys and WAL on). Setting `HOME_DB_PATH`, default `home.db`,
`:memory:` when `APP_ENV=test`. The store is a single-process store, like chat memory; a multi-host
deployment would put the same tables in PostgreSQL behind the same `HomeStore` interface.

Tables (`player_id` is the `X-Player-ID` UUID as TEXT; there is no `users` table):

| Table | Key | Purpose |
| --- | --- | --- |
| `homes` | `player_id` | `visibility` (`family`/`friends`/`nobody`), `version`, `updated_at` |
| `home_rooms` | `(player_id, room_id)` | Rooms built, with `built_at` |
| `home_items` | `(player_id, room_id, item_id)` | `level` 1-10, `x`, `y` (both null = default spot), `rot` 0-3 |
| `wallets` | `player_id` | `coins >= 0` |
| `coin_ledger` | `id` | Append-only `delta`, `reason`, `ref`; `UNIQUE(player_id, reason, ref)` for idempotent credits |
| `friend_codes` | `player_id` | A unique random 8-character code to share |
| `friendships` | `(a, b)`, `a < b` | `status` `pending`/`accepted`, `requested_by` |
| `home_cheers` | `(visitor, owner, room_id, day)` | One cheer per room (or `home`) per visitor per day |

Rows are created lazily on first use. A new wallet is credited the starter grant through the ledger
(reason `starter`). Items at level 0 have no row. Item ids repeat across rooms (`plant`, `sink`), so
every item key includes the room.

## Rules file

`learning_service/home_rules.json`, generated once from `lib/home-data.ts` at `ceb8069` and checked by
tests: four rooms in order `living`, `kitchen`, `bedroom`, `bathroom`, eight items each with a
`starter` flag, `place_cost` 30, `max_level` 10, `unlock_level` 3, upgrade cost `20 * next_level`,
build cost `250 * rooms_built`, `floor_tiles` 2.7, `starter_coins` 240, quiz reward 10 per correct
answer. Every check reads this file; no number is typed twice.

## Behaviour

- **Build room.** Must be the next room in order; every item of the newest room must be at level
  3 or more; cost `250 * rooms_built`. The living room is free and exists from the first read. A new
  room gets its starter items at level 1 at no cost.
- **Place item.** Room built, item known and not yet placed (not a starter), cost 30, level 1. Body
  `{at, rot}`; `at` is within `[0, 2.7]` on both axes, or null for the default spot.
- **Upgrade.** Level below 10, cost `20 * (level + 1)`, level plus one.
- **Layout.** `PUT` replaces the room's whole layout: only placed items, positions inside the floor,
  `rot` 0-3. Omitted items return to their default spot. Free. The server cannot check furniture
  footprints; the app's `arrangeRoom()` still resolves overlaps when drawing.
- **Spending.** One `BEGIN IMMEDIATE` transaction debits the wallet (`coins >= cost`), writes the
  change, appends the ledger row and bumps `version`. Not enough coins is 402; a broken rule is 422.
- **Versions.** Every mutation accepts an optional `expected_version`; a stale one is 409 and changes
  nothing. Every success returns the whole home and the balance.
- **Friends.** A player shares their code. `POST /api/me/friends/requests {code}` creates a pending
  request (unknown code and self are 404 and 422; a repeat is idempotent). The other player accepts or
  declines. Either side can remove a friend.
- **Visit.** `GET /api/homes/{id}` is allowed for the owner, or an accepted friend when the owner's
  visibility is `friends`. Otherwise it is 404, the same as a missing home. Visitors get no coins.
- **Cheer.** `room_id` is a built room or `home`; allowed only where a visit is allowed; one per room
  per visitor per day (a repeat is 409); the owner sees counts in their own home.
- **Coins.** `POST /api/me/coins/claim-quiz {quiz_id}` reads the owner's recorded first answers from
  the existing quiz progress and credits 10 per first-correct answer, once per question of the
  question set (ledger `ref` = question-set id and question index, so a retry of the same questions
  pays nothing again). A top-up route works only when demo mode is on and is 404 otherwise.
- **Card.** `PUT /api/me/home/card {display_name, avatar}` sets the name and avatar reference shown on
  friend lists, home cards and visits. They live with the home so no request reads another player's
  profile partition. The default name is `Player`; the card does not change the home `version`.
- **Deletion.** `DELETE /api/me/data` also removes the player's home data, wallet, friendships,
  friend code and cheers sent or received.

## Endpoints

`GET /api/me/home`, `POST /api/me/home/rooms`, `POST /api/me/home/items/{room}/{item}/place`,
`POST /api/me/home/items/{room}/{item}/upgrade`, `PUT /api/me/home/rooms/{room}/layout`,
`PUT /api/me/home/visibility`, `PUT /api/me/home/card`, `GET /api/me/friends` (also returns the
caller's friend code), `POST /api/me/friends/requests`,
`POST /api/me/friends/requests/{player_id}/accept` and `.../decline` (the id is the requester's player
id), `DELETE /api/me/friends/{player_id}`, `GET /api/homes?scope=friends`, `GET /api/homes/{player_id}`,
`POST /api/homes/{player_id}/cheers`, `POST /api/me/coins/claim-quiz`, and (demo only)
`POST /api/me/coins/demo-topup`. All use typed Pydantic models and the existing no-store, CORS and
request-limit behaviour.

## Seed data

`scripts/seed_home_demo.py` loads the frontend's example homes (zara, mia, leo, sam) as synthetic
players with fixed, documented UUIDs, accepted friendships with the default player, and starter
wallets. It writes only to the configured home database, refuses `APP_ENV=production`, and never runs
at startup. Tests call the same function.

## Errors

402 not enough coins; 404 missing or not visible; 409 stale version or repeated cheer; 422 broken
rule or invalid body; 429 existing request limit.

## Testing

Test-first, in `tests/test_home_*.py`: rules-file consistency; each rule and its failure; a rejected
spend leaves wallet, item, ledger and version unchanged; two concurrent spends cannot overdraw;
isolation between players; visibility matrix; friend-code flow; one cheer per day; quiz claim is
idempotent and ignores later answers; deletion; demo-only top-up. The existing suite must still pass.

## Out of scope (flagged in the docs)

Duel and quest coin awards, shop purchases (these stay in the browser and will not match a server
balance), parent approval of friends, family links, and all frontend changes: send `X-Player-ID`, add
`me` and `homes` to the proxy allow-list, add `PUT` to the proxy (or change layout and visibility to
`POST`), and read coins from `GET /api/me/home`.
