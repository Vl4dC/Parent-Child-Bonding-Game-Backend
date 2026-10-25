# Database and integration verification

## Runtime contract

Use the existing Supabase PostgreSQL schema; FastAPI no longer uses Supabase Auth. Keep the `api_private` schema out
of the exposed Data API schema list. The schema holds JSON domain records, immutable question
sets, job inputs/state, private lobbies, and quota counters. JSON records are typed/validated by the
API; SQL primary keys, RLS, transactions, and room functions enforce storage boundaries.

`DATABASE_URL` is server-only and must be able to switch to `authenticated`; the object owner also
executes worker/usage functions. Never use it in a client. Normal record transactions set local role
`authenticated` and caller-selected player UUID in `request.jwt.claims`, then apply owner RLS.
This preserves storage partitions only; it does not authenticate callers or protect against a caller
selecting another player ID. Keep browser roles without
permission to execute worker/quota functions. Do not grant extra access as a workaround.

Migrations are ordered under `supabase/migrations/`. Apply them explicitly with the Supabase CLI
migration workflow to a development project, or through an authorized database connection in filename
order. They require Supabase's existing `auth.uid()`, `anon`, and `authenticated` roles. Startup does
not modify schema. The optional Realtime installation runs only when its schema/functions exist.
If Realtime is enabled later, install equivalent policies/triggers before claiming live notifications.

Migration 001 creates records and transactional lobby commands; 002 adds durable job claiming,
workflow usage, and optional room transport policies; 003 adds leaving every lobby on data deletion;
004 adds persistent per-account request limiting; 005 adds partial job indexes for server performance. Never edit applied migrations: append new ones.

## Isolation and concurrency

Requests hold a PostgreSQL transaction. Owner advisory locks serialize writes, including absent-row
creation and idempotency checks, across API processes. Existing records use row locks. Read requests
are read-only and take neither owner-wide nor record row locks. Workers capture sources, release
connections during model calls, then validate source versions and the lease before an atomic result
commit. Long inline generation still holds source locks; use jobs for normal integrations.

Room commands additionally lock the room row and check its version. This handles competing members,
capacity, readiness, and host departure atomically. Question snapshots are immutable; attempt state is
stored separately. The memory adapter is explicitly nonpersistent and only allowed in demo/test mode.

Connections are bounded and reused through separate data/control pools; role/claims remain local
to each transaction and reset on commit/rollback. See [server limits](SERVER_PERFORMANCE.md).

Worker claims are global, concurrency-limited, and use expiring leases. A job's result and newly created
learning data commit in one transaction after verifying its lease. Dead workers are retried up to the
configured attempt limit. Provider billing is not transactional and can repeat after a crash.

## Running the complete tests

```sh
uv sync --locked
uv run ruff check app learning_service scripts tests
uv run ruff format --check app learning_service scripts tests
TEST_DATABASE_URL=postgresql://localhost/questnest_api_test uv run pytest -q
```

The test database must be disposable and named with an `_test` suffix. The session fixture deletes
its `api_private` schema, creates test-only auth roles/`auth.uid()` compatibility, applies every
migration from empty, and resets tables between cases. Never point it at a hosted or shared project.
No live model credentials are needed. Tests fake model and vision calls but use real PDF fixtures.

The same learning/API behavior is exercised with memory and PostgreSQL repositories. PostgreSQL
cases validate RLS without the HTTP layer, forged ownership, independent attempts, durable worker
results, rollback, retry bounds, room concurrency, and concurrent usage reservations.

The current local environment used native PostgreSQL because Docker was not running. This exercises
SQL and policies, not Supabase Auth email delivery or the hosted Realtime transport. Those remain
staging acceptance checks with authorized test accounts; do not report them as completed here.

## Operations

Use HTTPS and exact CORS origins. Keep provider keys and database URLs out of logs. API data responses
are no-store, provider errors are sanitized, and responses include request IDs. Generation defaults off.
Daily attempt limits count workflows rather than individual tokens/currency; configure provider budgets.
Use one root Python 3.13+ environment and lockfile. Run workers separately from the API.

`/health` reports process health. `/ready` checks the application schema and reports storage durability;
it does not call the auth/model provider. Back up PostgreSQL before applying later production migrations.
For a failed release, stop API/workers and restore the compatible code; schema rollback or restoration
requires a reviewed database procedure. Destructive down migrations are not run automatically.

Minimal usage counters and expired lobby rows need periodic maintenance; no retention schedule has
been silently chosen for schoolwork. Explicit deletion is implemented. Age-appropriate output policy,
reviewer authority, guardian consent, gameplay, rewards, and hosted deployment remain separate work.


## Diagram game storage — migration 008 addition

Migration 008 creates private frozen templates, races, participant state and request receipts.
The source-lesson foreign key cascades deletion to matches; a caller-bound function also removes
the deleting user from other owners’ matches. Public/anon/authenticated table access is revoked.
Controller authorization checks current room membership and captured participants. Read paths are
projected/read-only; mutations lock room then race, with no DB lock held during browser rendering.
See [diagram assembly](DIAGRAM_ASSEMBLY.md). Apply migrations explicitly; no hosted change was made.


## Diagram lifecycle migration 009 — 25 October 2026 addition

`20261025000900_diagram_lifecycle.sql` adds `diagram_races_template` and the private
`diagram_source_changed` trigger/function. Lesson data updates lock affected active races and
atomically persist source cancellation, preserving finished/expired outcomes. Existing deletion
cascades remain intact; no public schema access is granted. Apply after 008 explicitly.
Preflight checks tables, indexes, cleanup/invalidation functions and an origin-enabled trigger.
The migration has no historical repair sweep. Drain/cancel previously stale matches before
enabling an existing deployment; see [rollout and readiness](DIAGRAM_LIFECYCLE_READINESS.md).
No hosted database was migrated as part of implementation.


## Clerk player mapping and migrations 010/011 — 25 October 2026

Verified Clerk issuer/subject pairs now select stable derived UUIDs before existing request-local
RLS claims are set. There is no automatic ownership migration from anonymous/default-player data.
See [mapping and configuration](CLERK_BACKEND.md). No Supabase Auth provider client is introduced.

Migration 010 adds private source/lesson links for owned card-attempt records, insert/delete
triggers and history/receipt indexes. Source/lesson deletion removes attempts and their creation
receipts. Migration 011 adds private duel match/player/request tables, source/room deletion
cascades, source invalidation and caller-bound participant erasure. New private tables have RLS
and no browser-role table privileges. The normal card-attempt records keep existing owner RLS.

Apply after 009 explicitly; startup never migrates. Tests cover both empty-schema installation and
010/011 upgrade with legacy records retained, plus rollback, role privileges, concurrency, restart
and source/data erasure. Feature readiness verifies required tables/functions/indexes and active
triggers. See [deployment checks](DEPLOYMENT_READINESS_DUELS.md); no hosted SQL was executed.
