# Frontend social and live-duel compatibility

Implemented against frontend `main` **2ff2ec5**, specifically `docs/SOCIAL_API.md`,
`lib/social-api.ts`, the presence component and live-duel screens. No frontend file is changed.
All **16** frontend operations now exist in FastAPI under `/api/social` and appear in the live
Swagger UI `/docs`, with request examples, response models and Clerk Bearer authorization.
The generated [OpenAPI supplement](frontend-social.openapi.json) contains this contract.

This supersedes the earlier statement that `/api/social` was missing. Existing `/api/me`
friend, presence, lobby-invite, economy and learning APIs remain compatible and registered.
The social contract is a facade over the existing friendship/home/wallet data, with a new
server-clocked two-player duel lifecycle. It does not replace the flexible lobby/game engines.

## Setup and identity

The user explicitly approved **Clerk verification for `/api/social` only** on 25 October 2026.
Other routes retain their existing optional, unverified `X-Player-ID` selector and do not gain
a token requirement. Login/password/provider-account management routes remain absent.

Set these backend values before switching the frontend's existing `SOCIAL_API=backend` flag:

```dotenv
CLERK_ISSUER=https://your-instance.clerk.accounts.dev
CLERK_AUTHORIZED_PARTIES=["http://localhost:3000"]
```

Use the actual issuer and application origins. An HTTPS origin is required; credentials,
paths, query strings and fragments are rejected. If authorized parties are omitted, exact
`CORS_ORIGINS` are used. No Clerk secret/API key is needed for public JWKS verification.
The issuer's `/.well-known/jwks.json` URL is constructed from configuration, never from token
headers or unverified claims. Keys are cached using PyJWT's JWKS cache (300-second default,
bounded network timeout); signatures and token lifetime are still checked on each request.

Configure the Clerk session's signed custom claims already described by the frontend:

```json
{
  "player_id": "{{user.public_metadata.player_id}}",
  "username": "{{user.username}}"
}
```

The existing frontend learning-session helper assigns the UUID in public metadata. Refresh the
session token after setting metadata/claims (or sign out and back in) before testing. A stale token
without `player_id` is rejected rather than trusting the proxy header. The verified UUID connects
the social account to that same player's existing lessons, friendship graph, inventory and coins.

Verification fixes the algorithm to RS256, validates the signature and exact issuer, requires
`exp`, `nbf`, `iat`, `sub`, `sid`, and `player_id`, checks `azp` when present, and rejects Clerk
sessions marked `pending`. An optional `X-Player-ID` must match the signed UUID (otherwise 403).
Public social IDs are Clerk `sub` values; internal learning/wallet IDs stay UUIDs. Profile binding
rejects an existing subject or UUID being reassigned to another identity/issuer. Usernames come
only from signed claims; profile bodies cannot forge them. Username uniqueness is case-insensitive.

Missing/invalid tokens return 401 and `WWW-Authenticate: Bearer`. Unconfigured issuer or temporary
JWKS failure returns a sanitized 503, never an unauthenticated fallback. A valid session with no
username gets 422/`no_username` when registering a profile. All API responses remain no-store.
Swagger's **Authorize** button takes a raw Clerk session JWT; no `X-Player-ID` is needed there for
these routes. The legacy routes remain unlocked and use their documented selector independently.

**Scope boundary:** verifying this prefix does not authenticate the rest of the application.
Legacy selector APIs can still access or modify shared player data when supplied a UUID. This
patch is not an app-wide account-isolation change. Deployments requiring that guarantee must
restrict or separately authenticate those legacy entry points; their behavior was intentionally
left unchanged at the user's request.

## Endpoints

| Method | Path under `/api/social` | Request / response |
| --- | --- | --- |
| PUT | `/me` | `{display_name, avatar, role}` → `{id, username, display_name, avatar, role}` |
| POST | `/presence` | `{state: "online" | "offline"}` → heartbeat cadence, online window, incoming `invites` |
| GET | `/users/search?q=...&limit=10` | Username prefix results with relationship and friend-only status |
| GET | `/friends?online=true` | Friends with `since`, incoming requests, outgoing requests |
| POST | `/friend-requests` | `{username}` → 201 sent/request, or 200 accepted/friend for mutual requests |
| POST | `/friend-requests/{id}/accept` | Recipient accepts exact opaque request ID → `{friend}` |
| POST | `/friend-requests/{id}/decline` | Recipient declines → 204 |
| DELETE | `/friend-requests/{id}` | Sender cancels → 204 |
| DELETE | `/friends/{user_id}` | Remove by public Clerk subject → 204 |
| POST | `/duels` | `{opponent_id, lesson_id, num_questions}` → 201 invited duel |
| GET | `/duels/current` | My invited/active duel or 404 |
| GET | `/duels/{id}` | Participant-only state, timing, progress and finished results |
| POST | `/duels/{id}/accept` | Opponent starts shared countdown |
| POST | `/duels/{id}/decline` | Opponent declines while invited |
| POST | `/duels/{id}/cancel` | Challenger cancels while invited |
| POST | `/duels/{id}/answers` | `{index, choice}` → `{answered}` acknowledgement |

Errors use `{detail, code?}`. Typical codes are `offline`, `busy`, `expired`, `cancelled`,
`not_friends`, `invites_disabled`, `no_username` and `profile_required`. Unknown resources or
another player's requests/duels return 404. Wrong duel participant actions return 403.
Validation failures use FastAPI's existing sanitized 422 format.

## Profiles, search and friend requests

Only users who registered through `PUT /me` appear in username search. Search requires 2–64
characters and limit 1–20, excludes the caller, and uses a case-insensitive literal prefix
(`_` and `%` do not become SQL wildcard searches). Results are sorted by username and carry
`relationship: none | friend | request_sent | request_received`.

Public users contain exactly the frontend's `id`, `username`, `display_name`, `avatar`, `online`
and `busy` fields. For strangers, pending requests and the caller's own card, `online` and `busy`
are null. Accepted friends see availability. The existing `share_presence: false` preference hides
both flags; `allow_game_invites: false` rejects new live invitations as well as the existing lobby
invitations. Profile avatar is display metadata, not a grant of shop inventory ownership.

Friend requests use opaque `req_...` IDs, with `{id, from, to, created_at}`. They map to the same
`friendships` rows used by friend-code and UUID APIs, not a second friendship graph. Repeated
outgoing requests or already-accepted friends return 409 as the frontend expects. Mutual requests
accept the pair. Request IDs are invalidated when their underlying relationship is removed, so an
old ID cannot accept/cancel a later request. The older `/me/friend-requests` pair-key semantics stay
unchanged. Legacy friendships whose other player has not registered a social profile appear as a
placeholder until that user registers; username discovery cannot invent an external account.

## Presence and busy state

The compatibility cadence is **20 seconds** with a **45-second online window**. The signed Clerk
session ID identifies the presence lease; offline clears only that session's lease. Separate
Clerk sessions/devices can remain online. Tabs sharing one Clerk session share that lease; the
frontend has no per-tab ID in this contract. The existing explicit-session presence API remains
available with its original configurable TTL and semantics.

Heartbeat responses include incoming unexpired duels, so the frontend's existing popup can work
on any page. `busy` means participating in an invited or active social duel. It is not a global
lock on every older game mode. A short generation reservation prevents simultaneous creators from
spending model calls on the same busy players; it does not expose half-created duels.

## Live-duel lifecycle

- Create requires an accepted, online friend, both players free, and an owned source lesson.
  Input question count is 3–8. Existing generation enablement, daily limits, provider selection
  and safety checks apply. No model is called when friendship/online/busy checks already fail.
- It uses the existing `quiz_from_lesson` generation engine and freezes public questions plus
  a private answer key. The source is selected for **casual play**; generating this live duel
  does not silently change the lesson's approval state or relax the old tournament routes'
  approval requirements. Existing safety filtering can reduce the actual question count; use
  returned `num_questions`. Zero usable questions fails with 502.
- Both players are reserved while generation runs; the SQLite transaction is not held during
  model work. Online/friendship/preference/busy state is checked again before saving the duel.
  Failed generation releases reservations; a crashed worker's reservation expires after five minutes.
- Invites expire after **30 seconds**, starting after generation. Accept gives both players the
  same `start_at = server_now + 5 seconds`. `ends_at = start_at + actual questions × 20 seconds`.
  These are the frontend's fixed compatibility timings, independent of older lobby-invite TTLs.
- Questions stay null before the countdown ends. During active play, public questions contain no
  answer indices/explanations; progress reveals counts only. All questions become available together,
  with a shared whole-duel deadline (there is no independent per-question answer window).
- Each player's first valid choice is final. Identical retries return the saved acknowledgement,
  even after finish; a different retry is 409. Invalid indices/options are 422. A new answer before
  start or at/after the deadline is rejected, using server time evaluated inside the write lock.
- Finish occurs when both players answer all questions or time runs out. Unanswered choices are
  null and incorrect. Only then are both result sheets, answer indices and explanations revealed.
  Winner is computed from correct counts (`me`, `opponent`, `draw`).
- Expiration/timeout is evaluated on social requests; no background timer is required. `/duels/current`
  returning 404 after timeout still commits the completed game/rewards. Declined, cancelled and
  expired invites never become active again. Strict duplicate accept/decline/cancel returns 409,
  matching the frontend contract.

## Coins, quests and persistence

Each player who actually answered at least one question earns **20 participation coins + 10 per
correct answer** when the duel finishes. An idle player earns zero. Finished-state persistence,
answer recording and wallet credits commit atomically in the existing SQLite wallet database.
The usual ledger reasons/reference keys prevent repeated reads or retries paying twice. No extra
manual `/me/coins/claim-duel` call is needed for these new social duel IDs.

When both players participated, each gets one persisted duel activity receipt at the actual finish
time (the deadline for timed-out games, not the later polling time). Daily/weekly quest reads and
claims include these receipts, so social duels use the existing quest targets, Berlin resets and
claim protections. Activity is kept in the same SQLite transaction as the result/coins instead of
attempting a distributed transaction with the learning repository.

The existing frontend still increments its browser-local coin display on results. This does not
write the backend wallet; API balance wiring remains a separate frontend integration concern.
The backend never trusts client-reported scores, rewards, progress counters or timestamps.

New `live_social_*` tables are created non-destructively in `HOME_DB_PATH` at startup. No new
PostgreSQL migration is needed for this layer; existing application migrations, including 011,
remain required. Keep the wallet database durable and shared appropriately for the deployment.
Independent SQLite files on different hosts are not a shared social/game service.

Player-data deletion removes profile bindings, requests, presence and shared duel copies through
existing cascades. Material deletion also removes derived social duel question/answer copies for
that source owner. Already-earned wallet history and non-content quest receipts are retained for
the other player. Learning storage and SQLite deletion are not a distributed transaction.
Automatic historical retention/pruning and migration of the frontend's local social JSON file
are not part of this patch.

## Verification and references

Tests exercise real RSA-signed JWTs with synthetic public JWKS (no live Clerk calls), invalid
claims/signatures, legacy-route compatibility, exact frontend shapes, friendship reuse, privacy,
countdowns, early/timeout finish, no early answer leakage, retry receipts, generation races,
wallet rollback, quest integration, deletion, restart persistence and every Swagger operation.
Model outputs are faked; no live model generation or provider configuration is performed.

Implementation references: [Clerk manual JWT verification](https://clerk.com/docs/guides/sessions/manual-jwt-verification),
[Clerk session tokens](https://clerk.com/docs/guides/sessions/session-tokens), and
[PyJWT API](https://pyjwt.readthedocs.io/en/latest/api.html).
