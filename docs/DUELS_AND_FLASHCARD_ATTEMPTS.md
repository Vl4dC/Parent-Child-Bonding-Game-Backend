# Duel matches and flashcard attempts

Added 25 October 2026. Backend only; prepared for an unmerged review branch. No deployment,
hosted migration, frontend implementation or AI/model/harness changes are included. This guide
supplements existing documentation. Older solo tournament and deck routes retain their behavior.

## Active identity contract

The user selected Clerk for the backend. Production requires a configured Clerk issuer and
authorized frontend origins. Send `Authorization: Bearer <Clerk session token>`. The backend
verifies the token and derives its database player UUID; caller-supplied `X-Player-ID` cannot
override a Clerk identity. `GET /api/me/player` returns `{player_id, provider}` for comparing
room host/member IDs. See [Clerk setup and boundaries](CLERK_BACKEND.md).

New routes return 503 while their feature is disabled. New JSON inputs reject unknown fields,
client scores/timestamps/actor IDs, booleans in integer fields, and stale versions. Writes require
an `Idempotency-Key` of 8–128 characters. Use a new key per action; retry an uncertain action with
the identical key/body. HTTP/detail errors remain compatible; a general new error-code system
is outside this batch. All data responses are no-store.

## Real duels over existing reviewed questions

Create from an existing owned **approved** quiz or tournament and its approved lesson. No
questions are generated, edited or silently approved. The exact question-set ID is a required
precondition. Questions/options/solutions are frozen for the match; source deletion still erases
derived matches. The legacy solo tournament submission is not reused as two player attempts.

### Modes and rules

- `local`: one signed-in creator controls two named local slots for pass-the-phone play. Those
  labels are not two verified accounts. The owner can read/control both sheets; physical handoff
  and keeping a screen out of sight belong to the client. Choose two distinct 1–60 character names.
- `room`: exactly two current lobby members are captured as independent Clerk-backed participants.
  The creator must own the source and be the current room host. Later joiners cannot play or read
  private attempts. Each participant can change only their own readiness/answer sheet.
- Every new room match/rematch starts unready. Both captured members must be present and freshly
  ready to start; old lobby-ready flags do not count. Local mode may start directly.
- Room start creates a shared five-second countdown; local start is immediate. Public questions
  may preload during countdown, but answer/forfeit writes before the shared start are rejected.
- There is no competitive answer-speed score. Submit one complete sheet per player; the first
  accepted sheet is final. The server counts correct choices. Ties share rank/winner status.
- Scores, answer keys and explanations stay hidden until both attempts end or the operational
  deadline. An unsubmitted/forfeited player has no ranked score. Both forfeiting yields no winner.
- A match has a 24-hour operational expiry, capped by its room's expiry. Room access also ends
  with current membership or room expiry; room deletion cascades its duels. This is not permanent
  access to another player's shared source. Local owner history remains subject to source deletion.
- Disconnect/leave does not pause the clock or implicitly forfeit. A still-captured account can
  rejoin/resume while permitted. Erasing participation is different and cannot be undone by rejoin.
- Only one active duel or diagram race can occupy a room. Creation is serialized across engines;
  an active duel continues to reserve its room even if its feature is temporarily disabled.

### Endpoint inventory

Prefix: `/api/duels`. Paths below include only the suffix.

| Method | Path | Purpose/input |
| --- | --- | --- |
| GET | `/sources/{source_kind}/{source_id}` | Owner-only metadata: source kind `quiz`/`tournament`, question-set ID, title, count and approval flags; no keys |
| POST | `/api/duels` (prefix itself) | Create local or room match; request below |
| GET | `/history` | Current permitted match summaries; `after`, `limit` 1–100, default 20 |
| GET | `/rooms/{room_id}/match` | Latest room match for captured/current participants |
| GET | `/rooms/{room_id}/management` | Current host's minimal match ID/version/status/reason; no questions or player sheets |
| GET | `/{match_id}` | Current phase, timing, player readiness/submission flags, versions and source availability |
| GET | `/{match_id}/questions` | Frozen public questions, title and timing, during countdown/running only |
| GET | `/{match_id}/players/{player_id}/attempt` | Caller-controlled slot's version, readiness and own choices; no private grading |
| PUT | `/{match_id}/players/{player_id}/ready` | `{expected_version, ready}` using the player version |
| POST | `/{match_id}/players/{player_id}/submit` | `{expected_version, answers}` using the player version; one choice per question |
| POST | `/{match_id}/players/{player_id}/forfeit` | `{expected_version}` using the player version |
| POST | `/{match_id}/start` | `{expected_version}` using the match version; current captured host in room mode |
| POST | `/{match_id}/cancel` | `{expected_version}` using the match version; permitted creator/current host |
| GET | `/{match_id}/results` | Finished/expired ranking, scores, winners and source availability |
| GET | `/{match_id}/review` | Results, questions, frozen canonical solution and permitted player sheets after reveal |
| POST | `/{match_id}/rematch` | New independent match; `{}` locally or `{expected_room_version}` in room mode |

Create a local match:

```json
{
  "source_kind": "quiz",
  "source_id": "OWNED_QUIZ_ID",
  "expected_question_set_id": "REVIEWED_QUESTION_SET_ID",
  "mode": "local",
  "players": ["Alex", "Sam"]
}
```

For a room match, replace `players` with `room_id` and `expected_room_version`, and set mode
to `room`. Do not supply names/actor IDs; membership supplies them. `question_indices` optionally
selects 1–50 unique zero-based source positions, each 0–49 and within the saved question list.
The selected questions are reindexed from zero in that order, identically for both players.
Only reviewed choice-based questions with a unique correct option are supported. Unsupported
or malformed saved question sets return 422 rather than invoking a producer or judge.

Submit each slot independently:

```json
{"expected_version":1,"answers":[1,1,3]}
```

Room players use their current slot version after acknowledging readiness, not the match version.
A submit acknowledgement contains slot/match versions, phase and submitted/forfeited flags.
It does not reveal a score, answer key or correctness before the opponent ends. `GET /attempt`
can resume the caller's own choices; it cannot read the other room participant's sheet.

### Responses, reveal and recovery

A match snapshot contains `id`, `mode`, `room_id`, `title`, `status`, `version`, `total`,
`created_at`, `started_at`, `expires_at`, `server_now`, `my_player_id`, `players`, `source`
and `reason`. Timestamps are server epoch milliseconds. Local mode has no `my_player_id` because
the creator controls both local slots; use their opaque IDs from `players`.

Phases: waiting, countdown, running, finished, expired, cancelled. Match versions change on
accepted readiness/submission/forfeit/start/cancel actions; player versions are separate. Fetch
a fresh match snapshot before versioned host actions after another player's changes.

Results contain each slot's display name, submitted/forfeited flags, score/total/rank, plus
`winner_ids`, `scoring: "correct_answers_only"` and source availability. Review additionally
returns public questions, each permitted player's choices/results, and a frozen `solution`
with index, answer index, explanation and optional answer visual. The canonical solution exists
even if both players forfeited or nobody submitted. Render visual HTML in the existing sandboxed
frame contract; do not inject it into the application DOM.

Ordinary cancellation cannot replace finished/expired results. Source approval withdrawal or
meaningful source changes atomically cancel active matches through migration 011. Reapproval
never reopens a cancelled match. Finished/expired score summaries remain readable while source
invalid; review is blocked until valid again. Source/material/owner deletion removes derived
matches rather than preserving erased content. Participant data deletion removes their sheet
and personal slot; it cancels active matches and preserves terminal summaries without that slot.

Current room hosts can discover/cancel abandoned matches using the management endpoint even
when they joined too late to be participants. They do not gain board/review/sheet access. The
creating owner must still be a current member in room mode. A rematch requires the source-owning
creator and, for room mode, current host status and current room version; it never copies answers
or readiness into the next match.

Exact action replays return their historical acknowledgement without grading twice. Creation
replays return the same match ID with a current permitted snapshot. Refetch for current phase.
Histories use ascending opaque ID cursors, not a chronological ordering promise. Room history
includes only matches the caller can currently access; it is not permanent shared-source access.

## Flashcard attempts and deterministic restart

These routes freeze already-saved cards. They do not call card generation, safety models, a
provider or a new visual renderer. Saved formative cards need not be competitively approved;
duels have the separate approval requirement above. Original deck progress remains untouched.

| Method | Path | Input/result |
| --- | --- | --- |
| POST | `/api/flashcards/{deck_id}/attempts` | `{which:"all"}` (default) or `{which:"hard"}`; create an independent attempt |
| GET | `/api/flashcards/{deck_id}/attempts` | Owned history with `after`, `limit` 1–100; ascending opaque ID cursor |
| GET | `/api/flashcard-attempts/{attempt_id}` | Frozen cards, source references, attempt version/status/timestamps and state |
| POST | `/api/flashcard-attempts/{attempt_id}/reviews` | `{expected_version,card,result:"know"|"again"}`; current next card only |
| POST | `/api/flashcard-attempts/{attempt_id}/restart` | `{which:"all"|"hard"}`; freeze a new child from this attempt, with empty progress |
| GET | `/api/flashcard-attempts/{attempt_id}/results` | Cumulative summary at any phase, without duplicating the cards |

Review example:

```json
{"expected_version":1,"card":0,"result":"again"}
```

`know` retires the card; `again` repeats it after up to three other queued cards. Card indices
retain their original source positions; a hard-only attempt may start at index 2 rather than 0.
Always submit the returned `state.next`. An empty queue completes the attempt automatically.

Attempt responses include `id`, `source_deck_id`, `lesson_id`, frozen `title`, `source_hash`,
`parent_id`, `status`, `version`, creation/completion/abandonment timestamps, and `state`
(`next`, `known`, `total`, `reviews`, `hard`). The full attempt also returns frozen public cards.
`source_hash` identifies the source content; hard subsets retain that source fingerprint while
their actual selected cards are explicit in the attempt. History can include multiple source
versions and subsets for one deck.

Restarting an active parent marks it abandoned and preserves its progress. Restarting a completed
parent leaves its result unchanged. A child begins at version 1 with zero known/reviews. `all`
restarts all cards frozen in that parent, including a parent's existing subset; `hard` selects
cards previously marked again in that parent. Empty hard sets return 409 without abandonment.
To select the legacy source deck's hard cards, create through the deck route with `which: hard`.

Subsequent source content changes do not rewrite historical attempt cards or their creation
receipts; restarting a saved attempt uses its frozen cards. Source deck/lesson/material/data
deletion still erases attempts and creation receipts through migration 010. Read/write access
is owner-scoped. Aggregate `/api/me/progress.flashcard_reviews` includes both legacy deck reviews
and new attempt reviews, counting accepted actions once; restart itself adds no reviews.

## Bounds and compatibility

- Duel sources: 1–50 questions, at most 512,000 JSON characters for the source question list;
  assembled frozen match at most 2 MiB. Choice indices must fit each actual option list.
- Card attempts: 1–100 source cards and at most 1 MiB of frozen public card JSON.
- Default lifetime limits: 200 retained duels per creating player, 200 attempts per source deck,
  and 1,000 accepted action receipts per player/attempt or match action collection. A limit returns
  429; existing receipts are not silently evicted. Existing per-player request quotas also apply.
- No automatic retention/pruning period is invented here. Capacity/retention must be reviewed
  before broad enablement. Existing source/application-data deletion remains available.
- Existing `/api/tournament`, `/api/quiz`, and legacy `/api/flashcards/{id}/review` bodies/results
  retain their behavior. Do not submit two duel sheets to the legacy solo tournament route.
- These are casual formative matches: source owners can already inspect their source. No public
  ranked anti-cheat, coins, XP, quest claims or family permissions are created by this batch.

See [deployment checks](DEPLOYMENT_READINESS_DUELS.md) and the new
[OpenAPI supplement](duels-flashcards-clerk.openapi.json). Existing OpenAPI snapshots are unchanged.
