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

## Authentication and models

The client owns login/signup and session management through Supabase. The backend's former
account/session routes have been removed; send a provider-issued access token to protected API
routes with `Authorization: Bearer <access_token>`. Supabase still verifies real identity on each
request. The API sets no cookies and never receives passwords or refresh tokens. Keep token
refresh and sign-out in the client's provider integration. See [API.md](../API.md).

Explicit offline demo mode accepts the synthetic `demo_parent` and `demo_child` tokens without
login; neither is accepted in Supabase mode. Demo mode cannot run in production.

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

Data IDs are owned. Sharing a quiz or material ID does not grant access. Separate accounts cannot
currently share lesson content through these private learning routes. Private lobbies grant access
to lobby snapshots only; they do not implicitly grant access to members' learning data.

## Visuals and rooms

Visual HTML belongs inside a sandboxed frame with scripts allowed and no same-origin permission.
`/view` requests need Bearer authentication; fetch them with Authorization and use the response body.
Never put tokens into URLs. Sandbox/structural validation does not prove scientific correctness.

Private lobbies have eight-character invite codes, readiness, host transfer, closure, expiry, and
versioned snapshots. They do not start an imposter game or family duel. On Supabase, optional private
notifications say a room changed; refetch the authorized snapshot after notifications or reconnects.
No Presence publication or active-game synchronization is implemented yet.

A future game server must keep secret cards and answer keys off shared room payloads. The prototype's
full `RoomState.secret` must never become a public API response. Derive actors from verified identity,
validate phase/turn actions on the server, and choose/freeze rules before implementing each mode.


## Quiz lifecycle integration — 24 October 2026 addition

Use the new [deterministic quiz contract](QUIZ_PRACTICE_API.md) to resume first answers, display
factual results, finish/review, create independent retries and reuse question answers as cards.
Question corrections return a new quiz/snapshot; approvals are owner confirmations bound to that
snapshot. Future frontend wiring must supply Bearer identity, idempotency keys and current answer
counts. No frontend file was changed. Generation, safety checking and AI content production are
untouched; points/coins/badges, multiplayer and family access remain separate pending rules.


## Learner approval endpoints — additive note, 24 October 2026

Opt-in canonical routes now cover pending learner registration, private receipt polling,
notification resend and verified guardian decisions. See [LEARNER_APPROVAL.md](LEARNER_APPROVAL.md).
No frontend file was changed. The signup branch still needs future API-base/proxy wiring and
required headers; no same-origin /api/registration/learner alias is introduced. Parent/learner
labels are not authority. Birth-year intake does not settle the under-16 versus under-18 policy.
No activation, username login or automatic family-link behavior is included.


## Simultaneous diagram assembly — additive handoff, 24 October 2026

[The new backend contract](DIAGRAM_ASSEMBLY.md) supports selected saved flowcharts, study, room
discovery/readiness, a shared five-second countdown, private label placements and first-perfect
server timing. Frontend work is not included: proxy the new prefix with Bearer/Idempotency-Key and
PUT support, map drops to SVG slot IDs, provide keyboard/accessible alternatives, and render
progress/results/review. The fixed prompt is “How does this work?”; no model generation is invoked.
