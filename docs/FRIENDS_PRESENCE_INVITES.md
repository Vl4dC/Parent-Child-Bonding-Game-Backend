# Friend requests, presence and game invitations

Backend-only addition based on frontend `main` `1117c7c`. No frontend, authentication, provider,
email or push-notification service is changed. The player is the sign-in token's `player_id` claim
([API.md](../API.md#sign-in-and-player-state)). Role labels do not prove a family relationship.

## Flow

1. Send a friend request by code or known player UUID, then accept it through the endpoints below.
   The existing `/api/me/friends` friend-code routes remain compatible.
2. Each active tab/device picks a UUID and renews its presence session. Poll friend presence
   every 15 seconds while visible; use the returned cadence fields rather than hard-coding timers.
3. The host creates an existing `/api/rooms` lobby, then sends an invitation naming the friend,
   room, game and mode. An offline friend can receive it and accept before it expires.
4. The recipient polls their incoming invitations and accepts or declines. Acceptance joins the
   lobby; it does not start a game, spend coins, or generate content. The host can cancel pending
   invitations. No seat is reserved by sending an invitation.
5. After the intended players have joined, create `/api/challenges` with this `room_id`, then
   `/api/matches` with its `challenge_id`, chosen mode (and teams for duo). The match creator
   starts it. Existing profile/age, mode/player-count, timing and scoring rules still apply.
6. Play through the existing match APIs and claim the existing participation/duel rewards.
   Joining, sending, accepting or declining an invitation earns no coins or quest progress.

Create the challenge **after** invitation acceptance: challenge/match participants are frozen
when content/game state is created. Invitations target lobbies, not an already-created game's
participant list. They cannot insert a late player into a running match. `game` and `mode` are
immutable invitation intent, not a new game engine; the host supplies that intent when launching.
A two-player `mcq`/`friends` lobby is a friend duel. `parent_child` retains its existing role/time
rules. Team assignments remain part of match creation after all four duo players have joined.
For diagram-assembly races, accepted players can instead use the existing room readiness and
race APIs; invitation acceptance itself does not mark anyone ready.

## Friend requests

| Method | Path | Body / result |
| --- | --- | --- |
| GET | `/api/me/friends` | Existing friend code and accepted/pending friend cards |
| POST | `/api/me/friend-requests` | Exactly one of `{code: "ABCD2345"}` or `{friend_id: "UUID"}`; returns friend card/status |
| GET | `/api/me/friend-requests` | Separate `incoming` and `outgoing` arrays with `requested_at`, server time and polling cadence |
| POST | `/api/me/friend-requests/{player_id}/accept` | Accept this player's incoming request; returns accepted friend |
| POST | `/api/me/friend-requests/{player_id}/decline` | Decline this player's incoming request; 204 |
| DELETE | `/api/me/friend-requests/{player_id}` | Cancel your outgoing request to this player; 204 |
| DELETE | `/api/me/friends/{player_id}` | Existing remove-friend route |

Request cards use the **counterparty player UUID** as their action identifier, not a separate
request UUID. Sending to yourself is 422; unknown code/player is 404. UUID targets must already
have backend home/player state (the first presence heartbeat or friend-code read initializes it).
This does not implement a username directory or query Clerk; resolve usernames to the stable
backend player UUID outside this service if needed.

Repeated sends reuse the same pending relationship and do not create duplicate requests. If the
other player has already requested you, your return request supplies mutual consent and establishes
the friendship, preserving the existing friend-code behavior. A pending outgoing request cannot
be accepted/declined by its sender, and a pending incoming request cannot be cancelled by its
recipient: wrong-direction actions return 403. Only accepted friendship grants presence and game
invitation access. Existing home visibility rules continue to apply.

Accepting an already accepted relationship returns the friend; declining/cancelling a missing
request returns 204. Decline/cancel of an accepted friendship returns 409 instead of unfriending.
Use the explicit remove-friend route for that. These actions target the current relationship for
the pair; they are not immutable per-request historical receipts. Requests remain in HomeStore,
are removed by player-data deletion, and have no automatic expiry. The ordinary API request-rate
limit applies. Existing `/me/friends/requests` routes remain available with their original semantics.

## Presence

| Method | Path | Body / result |
| --- | --- | --- |
| PUT | `/api/me/presence/sessions/{session_uuid}` | `{status: "online" | "away" | "busy"}`; returns session, expiry, heartbeat cadence, server time |
| DELETE | `/api/me/presence/sessions/{session_uuid}` | End only this session; repeat calls return 204 |
| GET | `/api/me/friends/presence` | Accepted friends with player ID, display name, avatar, status and last-seen time; server time and poll cadence |
| GET | `/api/me/social-settings` | `share_presence`, `allow_game_invites` |
| PUT | `/api/me/social-settings` | Replace those two booleans (omitted fields use defaults of true) |

Default lease is **90 seconds**, renewed every **30 seconds**. The server ignores client clocks
and does not accept timestamps. A session whose last heartbeat is exactly one TTL old is offline.
No background expiry job is required. Up to eight live sessions per player are allowed; expired
sessions are reclaimed when a new heartbeat arrives. Renewing an existing session does not consume
another slot. Deleting a tab/session does not log out or disconnect another tab/device.

When sessions disagree, `busy` takes precedence over `online`, then `away`; no live sessions means
`offline`. These are client-declared availability hints, not proof that someone is playing or
learning, and never contribute to rewards. The last-seen timestamp comes from accepted heartbeats.
Pending friends and strangers never appear in this listing. Removing friendship removes presence
visibility on the next read. `share_presence: false` appears offline with a null last-seen value;
it does not stop the player's private session renewals. There is no public user-presence lookup.

## Invitations

| Method | Path | Body / result |
| --- | --- | --- |
| POST | `/api/me/game-invites` | `{friend_id, room_id, game?, mode?}` and `Idempotency-Key`; returns invitation, HTTP 201 |
| GET | `/api/me/game-invites` | Paginated inbox/outbox/history |
| GET | `/api/me/game-invites/{id}` | Sender/recipient view of one invitation |
| POST | `/api/me/game-invites/{id}/accept` | Recipient only; `{invite, room}` with current joined lobby |
| POST | `/api/me/game-invites/{id}/decline` | Recipient only; `{invite, room: null}` |
| POST | `/api/me/game-invites/{id}/cancel` | Sender only; `{invite, room: null}` |

Example (use actual UUIDs and a unique request key):

```json
{
  "friend_id": "00000000-0000-4000-8000-000000000002",
  "room_id": "00000000-0000-4000-8000-000000000003",
  "game": "mcq",
  "mode": "friends"
}
```

Games: `mcq`, `heads_up`, `diagram`. Modes: `friends`, `parent_child`, `duo`; defaults are `mcq`
and `friends`. Parent-child invitations require a two-player lobby. Duo invitations require
four-player capacity and MCQ/heads-up, matching the current engine (no duo diagram game). Only
the current host may invite. Unknown games/modes and arbitrary fields are rejected.

An invitation contains IDs for itself/sender/recipient/room, game/mode, `status`, `created_at`,
`expires_at`, and `updated_at`. Timestamps use UTC ISO format. It does **not** contain a room
code, answer keys, other players' answers, tokens or client-selected scores. The room code is
returned only as part of the normal room snapshot after a successful acceptance/join.

Statuses are `pending`, `accepted`, `declined`, `cancelled`, `expired`. Default expiry is ten
minutes, capped by the lobby expiry. Pending expiry is evaluated using server time on reads and
actions; `updated_at` records the last stored change and is not an expiry-notification timestamp.
Offline status does not block creation/acceptance. Hidden presence also does not disable invites;
`allow_game_invites: false` blocks new invites but permits a deliberate response to an older one.

Creation requires an 8–128-character `Idempotency-Key`. An exact replay returns the same invitation
and its current status; a reused key with different input returns 409. A second pending invite
from the same sender to the same friend/lobby is rejected even with a new key. Pending duplicates
must be cancelled or expire before resending. Default limits are 30 new invites per rolling hour
and 20 pending invites per sender; cancellations/rejections do not reset the hourly counter.
Only accepted friends may be invited, and friendship is checked again at acceptance. These checks
use the existing home friendship store, not the separate Next.js social JSON file.

Acceptance rechecks live lobby state, invitation expiry after lock waits, and inviter host ownership, then joins through the existing
room command. A full, closed, expired, deleted or transferred-host lobby cannot grant a new join.
If a friend joined directly before accepting, acceptance keeps the existing membership. Replaying
an accepted invitation returns its current room snapshot without adding a member or resetting
readiness. If the recipient subsequently left, it returns `room: null` rather than silently
rejoining. A new deliberate invitation is needed to join again through this feature.

Repeated decline/cancel is harmless. Conflicting terminal transitions return 409. A sender cannot
accept their own outgoing invitation; a recipient cannot cancel it (they decline it instead).
Outsiders get 404; wrong participant action gets 403. Changing preferences or removing friendship
does not eject an existing room member. Lobby membership remains governed by the room APIs.

Listing parameters: `direction=incoming|outgoing|all` (default all), optional `status`, `limit`
1–100 (default 50), and `after` from `next_cursor`. Newest first, ordered by creation time and UUID.
Only the caller's invitations are visible. Response also includes `server_now` and
`poll_after_seconds: 15`. Polling is the supported delivery mechanism; this does not implement
SSE/WebSockets, an unread notification center, emails, or operating-system push notifications.

## Storage, deployment and limits

- Presence/settings live in existing `HOME_DB_PATH` SQLite alongside friendships. Startup adds
  tables without rewriting existing homes/wallets. Keep the file durable across restarts.
- Invitations live in PostgreSQL `api_private.game_invites`; the demo/test adapter keeps them
  in memory. Apply migration **`20261025001100_game_invites.sql`** before deploying this API.
  Startup does not apply SQL. `/ready` now checks the invitation table and room-lookup function.
- PostgreSQL invite acceptance locks the invitation and invited lobby, and commits the accepted
  state and room join together. A failed join/transaction changes neither. Concurrent contenders
  for the final seat cannot overfill the lobby. The memory adapter serializes and restores failed
  operations. Sender request-key uniqueness and row locks prevent duplicate invite/state changes.
- The narrow `game_invite_room(uuid)` helper allows an invitation participant's request to lock
  its target lobby before joining, while ordinary room RLS still hides that lobby from outsiders.
  Keep `api_private` unexposed. These are partition/membership rules, not verified authentication.
- Friendship/preferences and game invitations use different stores. Friendship is checked at
  creation/acceptance, but there is no distributed transaction with a concurrent friendship
  removal. Room join and invitation acceptance themselves are atomic in PostgreSQL.
- `DELETE /api/me/data` removes the player's presence/settings and all sent/received invitations.
  Deleting an empty lobby cascades its invitations. Expired/terminal invitations otherwise remain
  for replay/history; automatic historical retention pruning is not added in this feature.
- Several backend hosts need shared friendship/presence storage; independent SQLite files are
  not a global online-status service. Existing no-auth UUID selection remains unchanged.

Environment settings (also present in example env files):

```dotenv
PRESENCE_TTL_SECONDS=90
GAME_INVITE_TTL_SECONDS=600
GAME_INVITES_PER_HOUR=30
```

TTL ranges: presence 30–300 seconds, invitations 60–3600 seconds. Hourly invitations: 1–100.
Polling/heartbeat cadence is returned by the API. There is no account-level "online forever"
flag or client-supplied expiry.

## Frontend handoff

No frontend code is changed. Its current social routes use Clerk IDs and `.data/social.json`;
these APIs use the existing learning-session UUIDs and the backend friend graph. Map friends to
those stable UUIDs and consolidate accepted friendships before wiring invitations. Do not send a
Clerk `user_...` ID into a UUID field. Extend the learning proxy to forward `/me`, `/rooms`,
`/challenges`, `/matches`, PUT and `Idempotency-Key`. There is no automatic import of the Next.js
JSON data and no new async "play this saved tournament later" duel engine in this patch.

The backend flow was exercised through friendship → invitation → acceptance → challenge →
match → start → player actions → existing participation/duel rewards. Model outputs are faked
in tests; invitations/presence never invoke a model. See the generated
[social OpenAPI supplement](social.openapi.json) for typed payloads and runnable examples.
