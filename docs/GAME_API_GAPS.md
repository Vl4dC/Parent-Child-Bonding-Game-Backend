# Game APIs: what is built and what is still missing

Written 25 October 2026 for the backend and frontend developers who will finish the game side. It lists what
exists, what blocks integration, and every API the frontend needs that the backend does not yet provide.

Evidence: this backend on branch `codex/home-storage`, and the frontend repository
(`RahulRohidasPatil/Parent-Child-Bonding-Game`, private) `main` at `ff5eff2`, where all 125 source files under
`app/`, `components/`, `lib/` and `docs/` were read. Nothing in the frontend was run; statements about its
behaviour come from reading its code. The frontend commits after `ceb8069` only changed theming.

## 1. Built in this branch

Seventeen operations; details and rules are in [Home, wallet and friends](API_PLATFORM.md#home-wallet-and-friends).
Design record: [the approved spec](superpowers/specs/2026-10-25-home-storage-design.md).

| Area | Operations |
| --- | --- |
| My home | `GET /api/me/home`; `POST /api/me/home/rooms`; `POST .../items/{room}/{item}/place` and `/upgrade`; `PUT .../rooms/{room}/layout`; `PUT .../visibility`; `PUT .../card` |
| Friends | `GET /api/me/friends`; `POST /api/me/friends/requests`; `POST .../{player_id}/accept` and `/decline`; `DELETE /api/me/friends/{player_id}` |
| Visits | `GET /api/homes`; `GET /api/homes/{player_id}`; `POST /api/homes/{player_id}/cheers` |
| Coins | `POST /api/me/coins/claim-quiz`; `POST /api/me/coins/demo-topup` (demo mode only) |

Storage is SQLite (`HOME_DB_PATH`). `DELETE /api/me/data` also clears it. `scripts/seed_home_demo.py` loads four
synthetic demo players.

## 2. Blockers: fix these first

1. **Player identity.** The frontend signs in with Clerk, so `session.user.id` is a Clerk id (`user_…`), not a
   UUID. `app/api/learn/[...path]/route.ts` forwards `X-Player-ID` only when the id is a UUID and otherwise falls
   back to the shared default player. `lib/learn-api.ts` calls `/api/<path>`, whose proxy (`app/api/[...path]`)
   forwards no `X-Player-ID` at all. Until the frontend derives a stable UUID per account (for example a UUIDv5 of
   the Clerk id) and sends it on every call, all users share one home, one wallet and one friend code, and a friend
   request to "someone else" is a request to yourself (422). This is a frontend fix. The backend cannot verify the
   mapping: `X-Player-ID` is not identity (AGENTS.md).
2. **The duel cannot work against the current backend.** `app/(app)/play/duel/duel-screen.tsx` creates a
   tournament and then submits two different answer sheets to it as the same player. The backend refuses this:
   - `POST /api/tournament` returns 409 unless the lesson is `approved`, and the frontend never calls approve.
   - `POST /api/tournament/{id}/submit` returns 409 until the question set is reviewed and approved, and the
     frontend never calls review or approve.
   - A second submission with different answers returns 409 ("already been submitted").
   Needed: a match API with one submission per participant (section 3, "Duel").
3. **Proxy.** `app/api/[...path]/route.ts` and `app/api/learn/[...path]/route.ts` export only GET, POST and DELETE
   (the new layout, visibility and card routes use PUT) and allow only `materials|lessons|quiz|flashcards|tournament`
   (the learn proxy adds `chat`). `me`, `homes` and `rooms` are not allowed. Two proxies with different behaviour
   exist, and `learn-api.ts` uses the one without `chat` and without `X-Player-ID`.

## 3. Economy APIs still missing

Coins are shared by the home, shop, quests, quiz and duel. Only the home and the quiz claim are on the server; the
rest still change the browser's own balance (`lib/store.ts`), so the two balances will differ until these move.

| Needed | Evidence in the frontend | Rules the backend must own |
| --- | --- | --- |
| `GET /api/me/stats` | `StatChips` in `components/qn/ui.tsx` reads local `coins` and `streakDays` | Coins from the wallet; streak from server-recorded activity days |
| Shop: `GET /api/shop`, `POST /api/shop/{item}/buy`, `PUT /api/me/look` | `app/(app)/shop/shop-screen.tsx` | Prices: `nova` 0; `mia`, `zara`, `leo`, `sam` 300; `mom`, `dad` 500; pet `cat` 0. Buy debits the wallet and writes a ledger row once per item |
| Quests: `GET /api/me/quests`, `POST /api/me/quests/{id}/claim` | `QUESTS` in `lib/qn-data.ts`, `quests-screen.tsx` | See the table below. Progress must come from server-recorded events inside a day or week window; `GET /api/me/progress` only has lifetime totals. A claim pays once per window |
| Duel reward | `duel-screen.tsx`: `addCoins(score * 10 + 20)` | 20 for playing plus 10 per right answer, paid once per match |
| Streak | `markStudiedToday()` in `lib/store.ts`, called after a quiz, a card review and a duel | Server clock and a timezone decision |
| Settings sync | `settings-screen.tsx`: goal 10/15/20, timers, read aloud, study reminder, duel invites, cheers, visit | `/api/me/profile` already has `daily_goal`, `avatar_reference` and preferences but the frontend never calls it. `visit` duplicates `PUT /api/me/home/visibility`: pick one source |
| `GET /api/me/coins/history` | none yet | The ledger exists and nothing reads it |
| Duel coin claim | see "Duel reward" | Idempotent by match id |

Quest definitions the backend would need to mirror:

| id | Quest | Target | Reward |
| --- | --- | --- | --- |
| `d-lesson` | Finish 1 lesson | 1 | 60 |
| `d-quiz` | Answer 5 quiz questions | 5 | 50 |
| `d-cards` | Review 8 flashcards | 8 | 40 |
| `d-duel` | Play 1 family duel | 1 | 40 |
| `w-duels` | Play 3 family duels | 3 | 250 |
| `w-lessons` | Finish 5 lessons | 5 | 200 |
| `w-weak` | Practise your weak spots twice | 2 | 150 |
| `w-upload` | Learn from 3 uploads | 3 | 120 |

**Duel (match API).** One match per lesson with the same questions for every participant, one submission per
participant, results compared on the server, a reward claim per participant. The frontend today is pass-the-phone;
whether the duel becomes two devices is open (section 7).

## 4. Social features still missing

- **Presence and status.** The friends list shows an online dot and a status line (`FRIENDS`/`FAMILY` in
  `lib/qn-data.ts`, hard-coded). Needs a presence or last-active value on friends.
- **Family and parent link.** The UI shows a family code, "grown-ups join with this code to link progress and
  check notes", and "a parent approves each new friend". None exists. `LEARNER_APPROVAL.md` records that approval
  never creates a family link, and AGENTS.md says a parent label proves no linkage. Needs a product and safety
  decision before building.
- **Cheers.** The owner only sees counts in `GET /api/me/home`. Needs an unread or "since last seen" view if the UI
  should say who cheered, and the "Cheers on my base" setting is not enforced by the cheer route.
- **Duel invites** and the "Duel {friend}" button on the visit screen need cross-device matches and notifications.
- **Safety.** Display names are free text shown to other children. They should go through the existing safety
  checker (`learning_service/safety.py`). There is no block or report.

## 5. Multiplayer gameplay

The backend has private lobbies only (`/api/rooms`: create, join by 8-character code, readiness, leave, close). A
game layer on top is missing:

- Server-authoritative sessions: start (host), a per-player view that hides secrets, actions, phase deadlines,
  scoring, rematch, and a live-update transport (SSE, WebSocket or polling; only a Supabase `room_changed` hint
  exists).
- **Imposter game.** `components/game/*` and `lib/game/mock-room.ts` implement it locally. Phases:
  lobby, reveal, clues, discuss, vote, result, scoreboard. Roles `grownup`/`kid`. Scoring: imposter caught means
  everyone else +1; imposter escapes means the imposter +2. The comment in `lib/game/types.ts` says a real server
  must never send the word to the imposter. The component has no page, and its Leave button links to `/splash`,
  which does not exist.
- **Other party games** listed in `PARTY_GAMES` (`lib/qn-data.ts`), marked "Coming soon" in the play hub:
  Forbidden clues (4-8 players), Forehead guess (2-8), Knowledge relay (2-8), Fact check (1-8), Flashcard race
  (1-4, solo only), Sketch it (3-8).
- **Content for games** must come from reviewed learning blocks, not free text (`docs/PRODUCT_PLAN.md`).
- **Team modes**: Kid + parent, Duo vs duo, Friends, Solo are chosen in `play-hub.tsx`.
- **Room codes** differ: the frontend makes 4 letters (`makeRoomCode`), the backend makes 8 characters
  `[A-Z0-9]`.
- The diagram-assembly race (14 operations, off by default) exists in the backend and is not used by the frontend.

## 6. Product-plan items with no UI yet

`docs/PRODUCT_PLAN.md` also lists badges that unlock more activities, a daily learning allowance, random reward
boxes, points as a separate currency from coins, avatar customisation, and optional leaderboards. Amounts, drop
rules, allowance reset and whether points and coins differ are all marked open there. The
`handoffs/diff-character-builder` package is described as a standalone hand-over with no integration. None of
these should be built before those decisions are made.

## 7. Existing backend routes the frontend does not call

`POST /api/lessons/{id}/complete`, `GET /api/me/progress`, `/api/rooms/*`, `/api/jobs`, `/api/resources/{kind}`,
`/api/memory/*`, `/api/me/profile`, and the newer quiz routes `/api/quiz/{id}/answers`, `/finish`, `/progress`,
`/results`. The frontend uses the older `/api/quiz/{id}/check`, which does record each first answer, so the quiz
coin claim works with it.

## 8. Known limits of what is built

- SQLite is single-process, like chat memory; several API hosts would need the tables in PostgreSQL.
- `family` visibility admits only the owner because no family link exists.
- Friends and visibility are an advisory privacy setting: anyone who knows a player's UUID can act as that player.
- Quiz coins are paid once per question of a question set; a freshly generated quiz can pay again.
- Duel, quest and shop coins are not on the server (section 3).

## 9. Suggested order and decisions needed

Order: the identity fix, then the match API for the duel, then stats, shop and quests, then settings sync and
cheer enforcement, then one party game, then the family link.

Decisions needed from the product owner:

1. Does the duel stay pass-the-phone, or become two devices?
2. Do the shop and quests move to the server now?
3. Is the family or parent link built now, and what does a parent approve?
4. Which party game goes first (the imposter game already has a UI)?
5. Which reward items from section 6, if any, are in scope?
