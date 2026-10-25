# Coins, quests and wardrobe

Backend implementation on 25 October 2026. Frontend reference: QuestNest `main` `65df915`.
This adds no frontend changes, authentication, XP, gems, cash purchases or model calls.
All routes use the existing optional `X-Player-ID` UUID partition selector. It is unverified;
without it callers share the default player. Parent/learner labels do not verify a family link.

## Shared wallet

The home, quiz rewards, duel rewards, participation rewards, quest rewards and shop all use the same wallet in
`HOME_DB_PATH`. New wallets start at 240 coins. There is no client-settable balance or price.

| Method | Path | Input / result |
| --- | --- | --- |
| GET | `/api/me/wallet?after=0&limit=50` | `coins`, ascending `transactions`, `next_cursor`; limit 1–100 |
| POST | `/api/me/coins/claim-quiz` | Existing `{quiz_id}`; 10 coins per recorded first-try correct question, once per question set/index |
| POST | `/api/me/coins/claim-participation` | `{source, id}`; 20 coins per completed game, once per participant |
| POST | `/api/me/coins/claim-duel` | Exactly one of `{tournament_id}` or `{match_id}`; returns `{coins, credited}` |
| GET | `/api/me/quests` | Periods, progress, targets, coin rewards, claimed/claimable and reset timestamps |
| POST | `/api/me/quests/{id}/claim` | `{period: "YYYY-MM-DD"}` from the listing; returns `{coins, credited}` |
| GET | `/api/shop/catalog` | Server-priced item IDs, names, slots, asset packs and starter flags |
| GET | `/api/me/inventory` | `owned` IDs and `equipped` slot-to-ID/null mapping |
| POST | `/api/me/shop/purchases` | `{item_id}`; returns `item_id`, `coins`, `charged`, `inventory` |
| PUT | `/api/me/equipment/{slot}` | `{item_id}` to equip an owned item; `{item_id: null}` to remove it |

Transactions have `id`, signed `delta`, `reason`, `ref`, and `created_at`. Pagination is
oldest-first and bounded; follow `next_cursor`. The balance is current, not a historical page total.
Existing home placement (30), upgrades (20 × destination level), room construction (250 × built
rooms), and insufficient-funds checks remain unchanged. Demo top-up remains demo-only.

## Duel flow

### Same-device family duel

1. Create and review/approve a tournament using the existing routes.
2. Collect both players' choices locally without submitting the first sheet to `/submit`.
3. `POST /api/tournament/{id}/duel` with `{"answers": [[1,0], [0,1]]}`. Both sheets must be
   complete and valid. The server grades and freezes both before returning `players` (two
   score/results objects), `completed_at`, and `reward_coins`.
4. `POST /api/me/coins/claim-duel` with `{"tournament_id": "..."}`.

The selected wallet receives **10 × sheet 0 correct answers + 20** for participation,
matching the current frontend's player-1 payout. Sheet 1 is a local guest, not another verified
player partition, so it does not receive a wallet award. Wrong answers do not subtract coins.
An identical duel submission returns its saved result; changed sheets return 409. A tournament
already submitted through solo `/submit` cannot become a duel after its answers were revealed.
A solo submission alone is never enough to claim a duel reward. Approval is still required.

### Server-clocked parent-child match

After a `parent_child` `mcq` match finishes, each participant may claim with `{match_id}`.
Each receives **10 × their own correct answers + 20**. Both players must have answered at least
one question; unanswered questions earn nothing. Waiting/countdown/in-progress games, idle
matches, solo/friends/duo modes and other game types do not earn these duel rewards. Existing
match membership and expiry rules apply. Claim before match expiry.

A local duel counts toward quests when submitted. A server match is registered toward the
claiming player's duel quests when its reward is collected, using the server's original match
end time. Claiming an old match later never moves its activity into a new day/week.

Claims pay once per player/source ID. Repeated successful claims return `credited: 0` with
the current balance. The reward ledger is independent of the client session or browser storage.

## Participation in every completed game

`POST /api/me/coins/claim-participation` with `{"source":"quiz","id":"..."}` awards
**20 coins regardless of score or winning**. The server verifies its saved game state. Sources:

| Source | Completion and participation requirement |
| --- | --- |
| `quiz` | Every question has a recorded first answer; all-wrong answers still qualify |
| `tournament` | A saved complete solo submission or both saved same-device duel sheets |
| `match` — MCQ | Match has ended and this player answered at least one question |
| `match` — heads-up | Match has ended and a turn involving this player as guesser or explainer advanced at least one card; skip/violation also count |
| `match` — diagram | Match has ended and the player submitted at least one full arrangement, correct or incorrect |
| `diagram_race` | Diagram-assembly race finished or expired; this player submitted at least once and did not forfeit |

Idle, unfinished and cancelled games do not qualify. Existing membership, expiry and feature
checks remain. Flashcard decks and lessons are learning activities, not games in this bonus;
their quest rewards are unchanged. Frontend-only mock games have no verifiable backend result
and cannot claim coins. Each newly completed independent attempt/match can earn its own bonus;
there is no daily participation cap in this rule.

Call this endpoint after completion/results. Practice quiz correct-answer coins still use
`claim-quiz`; the participation bonus is separate. A perfect five-question quiz therefore earns
50 correct-answer coins plus 20 participation coins, before quest bonuses. A zero-score game
earns the 20 participation coins alone.

**The existing duel +20 is this same bonus, not an additional +20.** Duel claims atomically credit
any unpaid correct-answer reward and participation component. Claiming participation first or
claiming the duel first produces the same total, even under concurrent retries. A repeat claim
returns `credited: 0`. For local duels only sheet 0 has the selected wallet; its local guest does
not acquire a separate wallet by sending a second sheet. Each server-match player claims their
own participation bonus. Eligible parent-child MCQ claims also register duel quest activity
at the original match end time, whichever reward endpoint is used first.

## Quest rules and calendar

| ID | Activity | Target | Coins | Period |
| --- | --- | ---: | ---: | --- |
| d-lesson | Explicit lesson completion | 1 | 60 | Daily |
| d-quiz | Recorded first answers in practice quiz attempts, correct or wrong | 5 | 50 | Daily |
| d-cards | Accepted flashcard reviews (`know` or `again`) | 8 | 40 | Daily |
| d-duel | Completed eligible family duel | 1 | 40 | Daily |
| w-duels | Completed eligible family duels | 3 | 250 | Weekly |
| w-lessons | Explicit lesson completions | 5 | 200 | Weekly |
| w-weak | Completed weak-spot quizzes or mistake decks | 2 | 150 | Weekly |
| w-upload | Distinct uploads whose derived lesson was completed | 3 | 120 | Weekly |

Calendar is **Europe/Berlin**. Daily periods start at local midnight; weeks start Monday at
local midnight. `period` is the local starting date; `resets_at` includes the actual offset.
Daylight-saving changes give 23/25-hour days. Rollover is computed on reads/claims, so it needs
no cron job. Old events and paid ledger rows remain history; current progress starts at zero.
Only the current period can be claimed. Stale period or incomplete quest: 409; unknown ID: 404.
Payout is manual and once per quest/period; daily and weekly bonuses may both apply.

Activity is recorded on the server, in the same application transaction as the action:

- `POST /api/lessons/{id}/complete` counts a lesson once. Merely generating/viewing content or
  generating a study plan does not count. Completing another lesson from an already counted
  upload does not count that upload again.
- Both `/api/quiz/{id}/check` and `/answers` count only the first saved answer for that attempt/index.
  A new independent practice attempt is new activity, but the existing quiz coin claim still
  pays only once per question-set/index.
- Follow-up quizzes with weak-topic focus, missed-question retries, and mistake decks count
  weak practice only after every question is answered or every card is known. Merely creating
  the practice does not count. Full-quiz retries/all-card decks are not weak practice.
- Flashcard review retries using the same `Idempotency-Key` do not advance progress again.
  Clients should send the key and `expected_reviews` to distinguish retries from new reviews.
- Each lesson, upload, completed weak-practice resource and duel contributes only once overall.
  A reset does not make an old completion count again. New activity earns the next period's progress.

No retroactive credit is inferred for activity completed before this backend addition. There is
no endpoint accepting arbitrary event names, progress values, timestamps, scores or coin amounts.

## Catalog, purchases and equipment

`learning_service/shop_catalog.json` is the authoritative catalog. Existing looks retain their
prices: Nova free; Mia/Zara/Leo/Sam 300; Mom/Dad 500. Mochi (`cat`) is free. Nova and Mochi are
owned/equipped on first inventory access, including for existing wallets, without resetting coins.
Unequipping a starter is persistent; re-reading inventory does not equip it again.

120 existing clothing/accessory IDs and names were imported from the frontend's
`handoffs/diff-character-builder/assets/avatar/atelier-r6/manifest.json` at `65df915`.
**Every clothing/accessory item costs 100 coins**, per the user's selected pricing. Slots are
`top`, `jacket`, `bottom`, `shoes`, `hat`, `glasses`, `earrings`, `tie`, plus `look` and `pet`.
Identity features such as skin, face, hair and hearing aids are not included in the paid catalog.
Only IDs/metadata are copied; assets and rendering stay in the frontend. Whole-character looks
and modular atelier clothing are separate rendering systems; owning/equipping IDs does not
implement a renderer or make the current frontend automatically display modular clothes.

A purchase atomically checks the server price, subtracts coins, appends the ledger transaction,
and grants ownership. Already-owned purchases return `charged: 0`, including concurrent retries.
A 402 insufficient-funds or 404 unknown-item response grants nothing and charges nothing.
Equipping is free, does not buy an item, rejects unowned items (409) and wrong slots (422), and
replaces only the given slot. Purchasing does not automatically equip. Clients cannot set a price,
choose someone else's wallet in the body, sell items, transfer coins or request refunds.
Profile avatar metadata is not proof of ownership; use `/me/inventory` for owned/equipped items.

## Persistence, deployment and recovery

- Application records gain owner-scoped `economy_events`, committed with learning results and
  duel submissions. Apply additive PostgreSQL migration `20261025001000_economy_event_index.sql`
  for efficient current-week reads. The generic records table/RLS needs no new permissions.
- Existing HomeStore startup adds `inventory`/`equipment` tables non-destructively to the wallet
  SQLite database. Purchases and all wallet credits use `BEGIN IMMEDIATE` and unique ledger
  references. Multiple connections to the same file cannot spend the same balance twice.
- Application records and wallet SQLite are separate stores. Reward claims read persisted source
  results; they never pay during the learning-result transaction. If a response/commit fails after
  a wallet credit, retry the claim: its ledger reference prevents duplicate payment. A match claim
  also repairs a missing activity receipt on retry. This is not a distributed transaction.
- Preserve both application storage and `HOME_DB_PATH` across restarts. Separate API hosts need
  a shared wallet implementation; independent SQLite files are independent balances.
- `DELETE /api/me/data` removes event receipts, inventory, equipment and wallet history as part
  of the existing player reset. Deleting individual learning content preserves already-earned
  quest activity (hashed references and timestamps, no lesson text) and wallet history. Reading again creates a fresh 240-coin wallet, as before.
- Browser-local balances, purchases, quest counts and the unlimited-coins setting are not imported
  or trusted. The frontend still needs wiring to these APIs; this change does not modify it.

See [economy OpenAPI supplement](economy.openapi.json) for executable request/response schemas.
Tests cover both memory/PostgreSQL activity storage, transaction rollback, DST/week/year resets,
repeat claims, source/player checks, wardrobe ownership, SQLite restart and concurrent spending.
