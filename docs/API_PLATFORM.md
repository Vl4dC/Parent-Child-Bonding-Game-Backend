# Platform API contract

All `/api/` endpoints work without authentication. Optional `X-Player-ID` selects a UUID data
partition; no header uses `00000000-0000-4000-8000-000000000001`. Requests are limited per selected
player per minute. This is unverified state selection, not account isolation: anyone can choose
another player's UUID. Gameplay membership, host actions, review and idempotency rules remain.
All API responses are non-cacheable and carry an `X-Request-ID`. See [API.md](../API.md).

Existing learning paths remain compatible except the explicit changes below. OpenAPI is generated
from the actual routes and typed models at `/openapi.json`.

## Endpoint inventory

| Method | Path | Behavior |
| --- | --- | --- |
| GET / PUT | `/api/me/profile` | Read or replace display name, goal, avatar reference, and accessibility preferences |
| DELETE | `/api/me/data` | Delete owned application content/jobs/progress and leave all rooms; auth account remains active |
| GET | `/api/me/progress` | Factual completion, first quiz-answer, correct-answer, card-review, and submission totals |
| GET | `/api/resources/{kind}` | Owned metadata summaries; `after` cursor and `limit` 1–100; no text or answer keys |
| POST | `/api/materials` | Upload one PDF or 1–20 JPG/PNG/WebP photos and extract text |
| GET / DELETE | `/api/materials/{id}` | Read metadata or delete material and its derived learning records |
| GET | `/api/materials/{id}/pages` | Read extracted pages, revision, and confirmation state |
| PUT | `/api/materials/{id}/pages` | Replace corrected pages with an expected revision; clears confirmation |
| GET | `/api/materials/{id}/revisions/{revision}` | Read the immutable extracted-text revision referenced by an older lesson |
| POST | `/api/materials/{id}/confirm` | Explicitly confirm the current extracted-text revision |
| GET | `/api/materials/{id}/find?topic=…` | Preview selected pages/chapter/source text without a model call |
| POST / GET | `/api/lessons`, `/api/lessons/{id}` | Generate draft lesson / read owned lesson |
| POST | `/api/lessons/{id}/revisions` | Create a new draft lesson ID with edited lesson content |
| POST | `/api/lessons/{id}/approve` | Record owner confirmation of a nonempty revision |
| POST | `/api/lessons/{id}/visuals` | Generate planned or requested visuals; new visuals invalidate approval |
| GET | `/api/lessons/{id}/visuals/{visual_id}/view` | Authorized sandboxed visual document |
| POST | `/api/lessons/{id}/quiz` | Generate private practice questions and initial attempt |
| GET | `/api/quiz/{id}` | Read public questions/options, excluding answer keys |
| POST | `/api/quiz/{id}/attempts` | Start an independent attempt using the same immutable question set |
| POST | `/api/quiz/{id}/check` | Check a choice; only the first answer contributes to saved progress |
| GET | `/api/quiz/{id}/questions/{index}/view` | Question diagram; completed diagram requires an answer first |
| POST | `/api/quiz/{id}/followup` | Generate new practice from recorded mistakes |
| POST | `/api/quiz/{id}/flashcards` | Create cards from missed/all questions without a model call |
| POST | `/api/lessons/{id}/flashcards` | Generate private lesson flashcards |
| GET | `/api/flashcards/{id}` | Read cards and individual review state |
| POST | `/api/flashcards/{id}/review` | Mark a card `know` or `again` |
| GET | `/api/flashcards/{id}/cards/{index}/view` | Sandboxed diagram side of a card |
| POST | `/api/lessons/{id}/complete` | Record a completion once; repeated calls return the same completion |
| POST | `/api/tournament` | Generate a solo scored question set from an approved lesson |
| GET | `/api/tournament/{id}` | Answer-free play representation |
| GET / POST | `/api/tournament/{id}/review`, `/api/tournament/{id}/approve` | Explicit author review and owner confirmation |
| POST | `/api/tournament/{id}/submit` | Save one complete scored submission; identical retries return the same result |
| POST / GET / DELETE | `/api/jobs`, `/api/jobs/{id}` | Queue generation / inspect status / cancel queued work |
| POST | `/api/rooms` | Create private lobby |
| POST | `/api/rooms/join` | Join by invite code; repeat joins do not duplicate membership |
| GET | `/api/rooms/{id}` | Authorized snapshot for initial load/reconnection |
| POST | `/api/rooms/{id}/commands` | Versioned readiness, leave, or host-close command |

`resources` kinds: `materials`, `lessons`, `quizzes`, `tournaments`, `decks`, `jobs`, `completions`.
Pagination order is ascending opaque ID; pass `next_cursor` as `after`. Stop when it is null.

## Review and versioning examples

Corrected text uses consecutive 1-based page numbers, up to 600 pages and one million characters:

```json
{"expected_revision":1,"pages":[{"number":1,"text":"Corrected source text.","via":"text"}]}
```

Replacing text increments the revision and resets `confirmed`. Original revisions remain readable;
lessons record `source.material_revision` as well as material ID and page numbers. Confirm with:

```json
{"expected_revision":2,"confirmed":true}
```

The same approval shape applies to lesson revisions; initial lessons are revision 1. Stale revisions
return 409. Lesson edits use `{"lesson":{"title":"…","summary":"…","sections":[{"heading":"…","body":"…"}],"key_points":["…"]}}`.
A revision returns a new ID and links to its predecessor. It never edits earlier lesson IDs or attempts.

Material-backed lessons require confirmation before generation. General-knowledge lessons can be
created without an upload and remain drafts. Practice is private formative work; scored solo sets
require lesson approval followed by explicit question review and approval. The author-review route
intentionally contains answers, so these self-reviewed results are not trusted ranked competition.

Profile PUT replaces the complete profile: omitted values reset to defaults. Avatar references are
display preferences and grant no cosmetic ownership or purchases.

## Attempts and progress

The initial `/quiz` ID is an attempt with an immutable question-set reference. `/attempts` creates
another attempt with the same questions and no answers recorded. Attempts are private to their owner;
selecting the same player UUID accesses the same state. Practice `/check` can be repeated for feedback, but later choices never
replace the recorded first choice. Flashcard reviews should send `Idempotency-Key` and `expected_reviews` (the current review counter).
Identical retries return their saved response; changed payloads or stale counters return 409. Each
deck retains up to 1,000 review receipts. Legacy reviews without these fields remain accepted and
do not provide replay protection. Completion markers are idempotent. No coins, XP, badges, mastery
claims, or inferred time-spent are granted by progress endpoints.

Tournament submission needs an approved question set. A repeated identical submission returns the
original score; changed answers return 409. This remains a solo scored practice foundation, not a
multiplayer tournament. Review is owner confirmation, not independent verification.

## Generation jobs

Send `Idempotency-Key` (8–128 characters) with:

```json
{"kind":"lesson","parameters":{"topic":"photosynthesis","age":12,"material_id":"OWNED_MATERIAL_ID"}}
```

Other kinds: `quiz`, `flashcards`, `visuals`, `followup`. Supply the owned lesson ID as `resource_id`
for quiz/cards/visuals; supply an owned quiz ID for followup. Parameters use the matching synchronous
endpoint's request model. Unknown parameters are rejected. Responses contain an ID, status, attempts,
created time, sanitized error, and result references. Inputs and lease credentials are not returned. Source ID/kind/revision are returned when applicable;
a worker rejects a changed material revision rather than silently generating from different text.

States: `queued` → `running` → `succeeded`, with bounded retries back to `queued` or terminal `failed`.
A stopped worker's lease expires after 15 minutes; another worker can reclaim the job. A stale worker
cannot commit output after its lease has been replaced. Job retries may repeat paid provider calls;
they do not promise exactly-once provider billing. Only queued work can be cancelled; repeating cancellation is safe. Cancelling running or completed work returns 409.

Generation is opt-in. Workflow-attempt limits count failed calls too, independently of request rollback.
`GENERATION_CONCURRENCY` limits inline calls per API process and running durable jobs across workers.
The inline and worker pools are separate; use durable jobs for production scheduling. Durable workers
release database connections during model calls and verify source versions before committing, so
profile/lobby updates and status reads can continue while generation runs. Provider calls
have explicit timeouts and no automatic SDK retries; workflow retries are bounded in the engines.

Uploads currently run synchronously because file streams are not retained. Text PDFs are extracted
locally; photo/scanned extraction can call a vision provider. Generation enablement/budgets apply to
uploads conservatively even when a particular PDF needs no model call.

## Private room contract

Create: `{"display_name":"Alex","capacity":4}`. Join: `{"code":"INVITECODE","display_name":"Sam"}`.
Codes are eight uppercase characters; send the actual returned code. Default capacity 4, range 2–8.
Rooms expire after 24 hours. Snapshots contain ID, code, host, capacity, version, expiry, closed flag,
and member display names/readiness. They contain no auth tokens, emails, lesson text, or answer keys.

Commands: `{"expected_version":2,"action":"ready","ready":true}`; actions also include `leave` and
`close`. Stale versions return 409; reread before retrying. Only the host closes a lobby. When the host
leaves, the earliest remaining member becomes host. An empty lobby is deleted. A closed room rejects
new joins and readiness changes. Lobby readiness never automatically starts a game.

On Supabase, the migration installs private `room:<id>` receive authorization and optional
`room_changed` notifications. Subscribe privately and refetch the authorized snapshot on each event
or reconnect. Room membership is checked by the API on every snapshot. Notifications carry only the
room ID; do not rely on a transport subscription remaining authorized after someone leaves. Presence
publishing and active-game synchronization are not implemented by these lobby endpoints.

## Limits and errors

JSON bodies: 4 MiB. Multipart bodies: 61 MiB, including framing; each file 25 MiB, combined file bytes
60 MiB, 20 photos, 600 PDF pages, at most 100 vision pages per upload. Actual received bytes are bounded,
including chunked or falsely declared requests. The generation workflow can return fewer reviewed
questions than requested; inspect the returned count and dropped count.

403: forbidden host action. 404: absent or unowned record. 409: stale state,
review required, or conflicting replay. 413/415: size/type. 422: invalid request. 429: request, attempt,
or concurrency limit. 502: sanitized provider failure. 503: storage unavailable or generation disabled.

## Deletion and remaining decisions

Deleting material removes its derived lessons/revisions, quizzes, question sets, decks, completions,
and associated jobs. `/me/data` removes owned application records and lobby membership without
changing any external account. Minimal anti-abuse counters remain; no raw provider prompts,
images, or extracted pages are retained in those counters. Original uploads/images are not retained
by this API after extraction; extracted text persists until explicit deletion.

Automated content retention periods, guardian permissions, reward economies, public matchmaking,
rankings, gameplay, and the proposed age-safety output gate remain pending. No frontend files or
hosted database were changed by this API implementation.
