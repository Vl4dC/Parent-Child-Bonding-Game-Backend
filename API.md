# Backend API reference

The backend exposes owned learning, review, profiles, attempts, progress, jobs, and private rooms:
see [Platform API](docs/API_PLATFORM.md). Python 3.13+ and the root environment are required.

Base URL for local development: `http://127.0.0.1:8000`. Swagger: `/docs`; ReDoc: `/redoc`.
The running `/openapi.json` is generated from the actual routes and typed models.

## Public API and player state

All FastAPI endpoints are available without account login, passwords, Bearer tokens or auth cookies.
There are no authentication provider calls or required Supabase Auth settings. Swagger/OpenAPI
contains no security schemes, Authorize button or padlocks. Former login/signup/session routes
remain removed (404). An obsolete Authorization header is ignored.

An optional `X-Player-ID` UUID selects the state partition for profiles, learning, jobs, rooms and
diagram races. Omitting it selects `00000000-0000-4000-8000-000000000001`. Use a different UUID per
player to retain independent progress and multiplayer membership. Invalid UUIDs return 422.
The ID is public, caller-selected context: anyone may select another player's ID. It supplies no
identity verification, privacy or protection against reading, changing or deleting that data.

Requests retain transactions, idempotency, quotas, resource partitioning and gameplay state rules.
Game host/membership checks use the selected player ID. Generation and optional features still
need explicit enablement. Removing authentication does not change model calls or database schema.

The separate learner-approval workflow keeps its receipt/proof and request-authority rules.
Its former /auth/registration routes are now /api/registration routes; there are no /auth endpoints.
Guardian list/decision routes accept the required `guardian_email` query filter and optional
`X-Player-ID`; neither is verified. No account session or confirmed-email check is required.
This workflow cannot establish verified guardian identity or legal consent.

## Health, errors and transport

`GET /health` returns `{"status":"ok"}`. `GET /ready` checks application storage and reports
whether it is durable. Neither calls an authentication provider.

403: gameplay or approval-workflow rule rejected. 404: absent record or wrong player partition.
409: stale state or conflicting replay. 422: invalid input. 429: quota/concurrency limit.
502: generation failure. 503: storage unavailable, generation or an optional feature disabled.
There are no account-authentication 401 responses. Errors omit raw submitted values and secrets.

All `/api/` responses retain no-store/no-cache and no-referrer headers. CORS keeps
exact origins and allows Content-Type, X-Player-ID, Idempotency-Key and workflow receipt headers;
legacy Authorization headers remain allowed but ignored. Cookie credentials remain disabled.

## Non-AI quiz lifecycle — 24 October 2026 addition

Eleven deterministic operations now supplement existing quiz contracts: resume/results, strict
idempotent answers, finish/review, all/missed retries, snapshot history, idempotent review cards,
author review, question revisions and snapshot-bound approval. See
[QUIZ_PRACTICE_API.md](docs/QUIZ_PRACTICE_API.md) and the additive
[OpenAPI supplement](docs/quiz-practice.openapi.json). AI generation/safety code is unchanged.


## Learner approval addition — 24 October 2026

Five opt-in request/status/resend/guardian-decision routes have been added. See
[LEARNER_APPROVAL.md](docs/LEARNER_APPROVAL.md) and the additive
[OpenAPI schema](docs/learner-approval.openapi.json). The feature defaults off; approval does not
activate a learner or grant family access. This workflow is independent of the removed login/signup
and session endpoints. Creation requests require a private random 32–128 character Idempotency-Key.


## Diagram assembly race — 24 October 2026 addition

Fourteen public operations reuse saved lesson flowcharts as frozen SVG label puzzles for
simultaneous private-room play. See [DIAGRAM_ASSEMBLY.md](docs/DIAGRAM_ASSEMBLY.md) and the
[additive OpenAPI](docs/diagram-assembly.openapi.json). Model source is unchanged; game grading
and timing are deterministic/server-owned. Feature defaults off; frontend and deployment are pending.


## Diagram lifecycle/readiness supplement — 25 October 2026

Added verified-session `GET /api/diagram-assembly/rooms/{room_id}/management`: current room host
only, returning latest `room_id`, `race_id`, `version`, `status`, `reason`. A late host may cancel
without participant content access. Cancel now returns 409 for finished/expired races; cancelled
retries stay stable. Active source withdrawal/change persists cancellation through migration 009.
Race snapshots add `reason` and `source`; results add `source: {available, reason}`. Finished
summaries remain readable when source invalid, while diagram review/board remain blocked.
`GET /ready` retains `ready`/`durable` and adds `features.diagram_assembly`; enabled dependency
failure returns 503. Disabled features do not require Node/Chromium. Responses are no-store.
See [exact rules and examples](docs/DIAGRAM_LIFECYCLE_READINESS.md) and the additive
[15-operation diagram/readiness OpenAPI snapshot](docs/diagram-lifecycle-readiness.openapi.json).
Earlier API descriptions and snapshots are preserved as historical records.


## Clerk, real duels and card attempts — 25 October 2026 addition

Current production requests require a verified Clerk Bearer session; earlier anonymous-mode sections
are historical. `X-Player-ID` cannot override a verified identity. `GET /api/me/player` supplies the
mapped UUID used by room membership. Account/login/signup endpoints remain removed.
The new APIs provide 16 duel operations and 6 card-attempt operations, plus that identity read.
See [complete bodies, responses and version rules](docs/DUELS_AND_FLASHCARD_ATTEMPTS.md),
[Clerk verification](docs/CLERK_BACKEND.md), and the new
[OpenAPI supplement](docs/duels-flashcards-clerk.openapi.json). Existing snapshots are retained.

New writes require Idempotency-Key; unknown fields, caller scores/actors/timestamps and stale
versions are rejected. Room duels require two captured verified players and fresh match readiness.
Local duels are creator-controlled pass-the-phone slots. Scores/solutions unlock only after both
attempts end or expiry. Card restart is deterministic and preserves original deck/earlier progress.
Enabled features add migration checks to `/ready`, and new routes reject unready feature storage.
Both features default off. See [migration, staging and rollback handoff](docs/DEPLOYMENT_READINESS_DUELS.md).
