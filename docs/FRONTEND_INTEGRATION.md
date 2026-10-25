# Frontend integration boundary

Frontend/product repository: https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game.
The inspected main snapshot is `c1663bc` (24 October 2026): QuestNest exports, reusable older
Kay/Splash components, and a starter homepage. Older page routes remain on `user_interface_v1`.
Fetch current source before implementing future requirements. No frontend code is changed here.

## Source context

| Main-repository path | Relevant context |
| --- | --- |
| `App-screen v2/` | QuestNest phone/tablet/website visual designs; not integrated app routes |
| `lib/kay/content.ts` | Older lesson, quiz, flashcard, and scripted tutor prototype data |
| `lib/kay/store.ts` | Prototype local preferences and profile storage |
| `lib/game/types.ts`, `lib/game/mock-room.ts` | Local imposter-game simulation and phase types |
| `components/kay/`, `components/game/` | Reusable UI components; presence does not prove routes work |
| `docs/PRODUCT_PLAN.md` | Product ideas; distinguish them from implemented API behavior |

The canonical current contracts are [API_PLATFORM.md](API_PLATFORM.md), [API.md](../API.md), and
`/openapi.json`. The backend supports owned learning, review, profiles, progress, jobs, and private
lobbies. Full game rules, public matchmaking, rewards, and family permissions remain pending.

## Public API and player state

FastAPI requires no login, access token, session cookie or Supabase Auth configuration. Frontend
login/register code is unchanged. Omit Authorization; old values are ignored. The optional
`X-Player-ID: <UUID>` selects player state; no header uses the shared default UUID documented in
[API.md](../API.md). Use a different ID for each multiplayer participant. These are unverified
selectors: callers can choose another ID, so they provide no account privacy or impersonation defense.

The learner-approval list/decision routes take `guardian_email` as a query filter, plus optional
X-Player-ID. The email is not verified. Existing receipt/proof and decision-state checks remain.

Backend account labels are `parent` and `learner`. Older Splash labels are `grownup`/`kid`, Kay labels
are `parent`/`kid`/`solo`, and the signup branch uses `student`/`parent`. Map display terms explicitly.
None is proof of a verified family relationship, age, consent, or permission to read another account.

Profiles use `preferences.read_aloud`; older client stores use `readAloud`. Profile PUT is replacement,
not a merge patch. Avatar references are display metadata and grant no inventory ownership.

## Learning integration

1. Upload material and retrieve its extracted pages.
2. Correct pages with an expected revision; confirm that exact revision explicitly.
3. Queue lesson generation with an Idempotency-Key and poll the owned job result, or use the legacy
   synchronous route for development. Generation must be explicitly enabled by the operator.
4. Review drafts; approval records owner confirmation, not independent factual verification.
5. Create private quizzes/flashcards. A new quiz attempt reuses an immutable question snapshot while
   keeping answers independent. Use Idempotency-Key and expected review counters for card retries.
6. Read real progress; there are no coin, badge, ranking, or inferred-mastery awards.

Data IDs are grouped under the selected player UUID. Selecting another UUID can access that
partition; this is no longer account isolation. Private lobbies grant access
to lobby snapshots only; they do not implicitly grant access to members' learning data.

## Chat with a PDF

`POST /api/chat` (see [API_PLATFORM.md](API_PLATFORM.md#chat-with-a-pdf)) is the contract for a chat screen with an
attach-PDF button and an "only answer from the PDF" switch (`mode: "pdf_only"`). Show `label` under an answer when
`used_pdf` is true. Upload with `POST /api/materials`, confirm the revision, then pass `material_id`; keep the
conversation on the client and send the last turns in `history`. Pictures: `POST /api/chat/image` (multipart `age`, `images`, optional `message` and `history`; no `message` means "explain this picture") lets the model look at a
photo, diagram or handwritten problem directly. Send the picture again with each follow-up, shrink large photos in
the browser first (the server accepts up to 8 MiB each), and show `label` under the answer. Pictures that fail the
child-safety check come back as the usual friendly 422. No frontend for this exists in this repository.

## Saved chats

Memory lets a chat screen show a sidebar of past chats grouped by subject and continue any of them: call
`GET /api/memory/conversations` (it returns the chats and a count per subject), open one with
`GET /api/memory/conversations/{id}` (every message plus `used_pdf`/`used_image` flags and `pages`), and keep chatting by
sending its id as `conversation_id` to `/api/chat` or `/api/chat/image`; do not resend `history`. Create a chat first with
`POST /api/memory/conversations` (pass `material_id` for a PDF chat). Each player id has its own memory, so a parent and a
child use different `X-Player-ID` values; `PUT /api/memory/profile` stores their role label and display name.
The role is a label, not authentication. A saved chat whose PDF was deleted still opens: the next answer carries a
`notice` asking for the PDF again. See [API_PLATFORM.md](API_PLATFORM.md#saved-chats-memory).

## Friend challenges

Flow for a competition screen: two or more friends join a room (`/api/rooms`), then
`GET /api/challenges/shared-topics?room_id=` lists the topics they have all studied, including ones worded differently
(`reworded`; it can take a few seconds, so show a spinner). Pick one and call
`POST /api/challenges` with the game, the topic, the **lowest** player age and the `room_id`; every member then reads
the same content from `GET /api/challenges/{id}` (no answers). Show the returned `label` ("Written by AI for casual play
and not reviewed"). For `diagram`, render the returned `mermaid` and put a drop target on each id in `slots`; the word
bank is `options`. For `heads_up` the public view only gives the deck size; there is no endpoint that deals the cards
yet. No game engine, timer or scoring exists yet, so the client would have to run the clock until one does. See
[API_PLATFORM.md](API_PLATFORM.md#friend-challenges-games-made-from-memory).

## Playing matches and the weekly preview

After a challenge exists (see above), `POST /api/matches` `{challenge_id, mode}` makes a match and the creator calls
`POST /api/matches/{id}/start`. Every player then polls `GET /api/matches/{id}` about once a second and draws from `view`;
line countdowns up with `server_time_ms` and `play_at_ms`, and never send times. MCQ: show `view.question` until
`your_closes_at_ms`, send `POST .../answer`, and show `view.reveals` when they appear. Heads-up: only the two players of
the current turn get `view.turn.card`; send `POST .../cards` for correct, skip or violation (tilt down/up or buttons).
Diagram: render `view.board.mermaid`, put a drop target on each `slots` id, send the whole arrangement with
`PUT .../placements` on each drop and `POST .../finish` to check. Results come from `GET .../results`. In parent-child
and duo matches show the parent their shorter `your_seconds`.

`GET /api/weekly/next?band=` returns everything for a "next week's challenge" screen (topic, games, modes, rules,
samples once prepared, and an example leaderboard marked `example: true`). It is a preview; do not present it as live.

## Visuals and rooms

Visual HTML belongs inside a sandboxed frame with scripts allowed and no same-origin permission.
`/view` requests need no credentials; include X-Player-ID when selecting nondefault player state.
Use the response body in the sandbox. Sandbox/structural validation does not prove scientific correctness.
With `ILLUSTRATIONS_ENABLED`, a visual's `kind` may also be `illustration`: a picture from an image model,
stored in the lesson as a WebP data URL and shown by the same `html` page, so the frame code is unchanged.
It has no words in it; show `caption` next to it.

Private lobbies have eight-character invite codes, readiness, host transfer, closure, expiry, and
versioned snapshots. They do not start an imposter game or family duel. On Supabase, optional private
notifications say a room changed; refetch the authorized snapshot after notifications or reconnects.
No Presence publication or active-game synchronization is implemented yet.

A future game server must keep secret cards and answer keys off shared room payloads. The prototype's
full `RoomState.secret` must never become a public API response. Use the selected player ID for game actors,
validate phase/turn actions on the server, and choose/freeze rules before implementing each mode.


## Quiz lifecycle integration — 24 October 2026 addition

Use the new [deterministic quiz contract](QUIZ_PRACTICE_API.md) to resume first answers, display
factual results, finish/review, create independent retries and reuse question answers as cards.
Question corrections return a new quiz/snapshot; approvals are owner confirmations bound to that
snapshot. Future frontend wiring may supply X-Player-ID and must supply idempotency keys and current answer
counts. No frontend file was changed. Generation, safety checking and AI content production are
untouched; points/coins/badges, multiplayer and family access remain separate pending rules.


## Learner approval endpoints — additive note, 24 October 2026

Opt-in canonical routes now cover pending learner registration, private receipt polling,
notification resend and request-specific guardian decisions. See [LEARNER_APPROVAL.md](LEARNER_APPROVAL.md).
No frontend file was changed. The signup branch still needs future API-base/proxy wiring and
required headers; no same-origin /api/registration/learner alias is introduced. Parent/learner
labels are not authority. Birth-year intake does not settle the under-16 versus under-18 policy.
No activation, username login or automatic family-link behavior is included.


## Simultaneous diagram assembly — additive handoff, 24 October 2026

[The new backend contract](DIAGRAM_ASSEMBLY.md) supports selected saved flowcharts, study, room
discovery/readiness, a shared five-second countdown, private label placements and first-perfect
server timing. Frontend work is not included: proxy the new prefix with X-Player-ID/Idempotency-Key and
PUT support, map drops to SVG slot IDs, provide keyboard/accessible alternatives, and render
progress/results/review. The fixed prompt is “How does this work?”; no model generation is invoked.


## Diagram lifecycle API handoff — 25 October 2026 addition

Backend only: snapshots now include source availability and cancellation reason; finished results
remain readable after source withdrawal, while review may return 409. Refresh snapshots after
mutations/retries because an idempotency receipt is historical. The current room host can discover
a race ID/version via `/api/diagram-assembly/rooms/{room_id}/management` and cancel it without
participant content access. Finished/expired cancellation is 409. Source-change cancellation
does not reopen on reapproval. Readiness now exposes enabled diagram dependency failures.
See [new contracts and unchanged leave/rejoin policy](DIAGRAM_LIFECYCLE_READINESS.md). No client,
proxy, UI, AI or harness files were modified. Fresh per-race acknowledgement remains future work.


## Home, wallet and friends — 25 October 2026 addition (backend ready, frontend not wired)

Frontend reference: `lib/home-data.ts`, `lib/store.ts` and the base, visit and friends screens of
`Parent-Child-Bonding-Game` `main` at `ceb8069`. Nothing in the frontend repository was changed. To use these
routes the frontend needs to:

1. Send a stable `X-Player-ID` UUID per account. At `ff5eff2` the app signs in with Clerk, whose user ids
   (`user_…`) are not UUIDs. The `/api/learn/*` proxy only forwards `X-Player-ID` when the id is a UUID and
   otherwise falls back to the shared default player, and `lib/learn-api.ts` calls `/api/<path>`, whose proxy
   forwards no `X-Player-ID` at all. Until the frontend derives a UUID from the account (for example a UUIDv5 of
   the Clerk id) and sends it, every browser shares one player's home, wallet and friend code.
2. Add `me` and `homes` to the proxy's allowed paths, and export `PUT` (the layout, visibility and card routes use
   it; the proxy exports only GET, POST and DELETE).
3. Read levels and coins from `GET /api/me/home`: turn each room's `items` back into the level list by walking
   `ROOMS[i].items` and taking the level by id (0 when absent), and build the layout from items that have `at`.
4. Replace the local `setLevels`, `buyRoom`, `place` and `levelUp` with the POST calls, and keep `version` to send
   as `expected_version`.
5. Show the server's 402/422 message; on 409 reload the home. Starter flags and costs are in
   `learning_service/home_rules.json`.

Limits to plan for: duel and quest coin awards and shop purchases are not server-side, so the browser's coin balance
and the server's will differ. Friends and visibility are unverified and advisory; the frontend copy says a parent
approves each friend, which this API cannot do. Replace the "unlimited coins" test tool with
`POST /api/me/coins/demo-topup` (demo mode only). See [Home, wallet and friends](API_PLATFORM.md#home-wallet-and-friends).

Everything the game side still needs beyond this (duel, shop, quests, stats, presence, family link, party games) is
listed with frontend evidence in [GAME_API_GAPS.md](GAME_API_GAPS.md).


## Coins, quests and wardrobe — 25 October 2026

The [economy contract](ECONOMY.md) adds server-scored same-device duel submissions, parent-child
MCQ reward claims, 20 participation coins per completed backend game, daily/weekly quests with Europe/Berlin calendar resets, shared wallet history,
server-priced purchases, inventory and equipment. Clothing/accessories cost 100 coins each.
This supersedes earlier statements that duel/quest rewards and shop purchases are pending.
The frontend remains unchanged and needs API wiring. See the contract for request examples,
activity definitions, migration 010, storage boundaries, and the existing unverified player selector.
