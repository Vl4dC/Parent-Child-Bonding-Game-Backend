# Backend operations

Run all Python commands from the backend root. This guide describes code/configuration; it does not certify the current hosted deployment.

## Runtime modes

| Mode | Configuration | Behavior |
| --- | --- | --- |
| Local API demo | `APP_ENV=development`, `DEMO_MODE=true`, no database URL | Memory application repository; generation off by default |
| Durable development | Development environment plus PostgreSQL and migrations | Durable repository and separately persisted SQLite files |
| Production | `APP_ENV=production`, demo false, PostgreSQL | Factory rejects an ephemeral application repository |
| Tests | Explicit test settings and faked models | In-memory stores unless configured; optional disposable PostgreSQL |

Use `uv sync --locked`, then `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log`.

## Configuration that matters

| Setting | Default / purpose |
| --- | --- |
| `DATABASE_URL` | Private PostgreSQL connection; required outside test/demo |
| `DATABASE_POOL_SIZE`, `DATABASE_CONTROL_POOL_SIZE` | 8 and 2 per process; budget API and worker pools together |
| `MEMORY_DB_PATH`, `HOME_DB_PATH` | `memory.db`, `home.db` outside tests; use absolute persistent paths for services |
| `CORS_ORIGINS` | Exact origins; default localhost:3000, no wildcard |
| `GENERATION_ENABLED` | false |
| `GENERATION_LIMITS_ENABLED` | true; disabling also lifts daily/request/busy gates |
| `GENERATION_CONCURRENCY`, `GENERATION_DAILY_LIMIT` | 2, 30 |
| `API_REQUESTS_PER_MINUTE`, `JOB_ATTEMPT_LIMIT` | 120, 2 |
| `QUIZ_MODEL`, `VISUAL_MODEL` | Luna main generation and Haiku visual code, as configured in source |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | Optional semantic retrieval; dimensions default 512, accepted range 64–3072 |
| `CLERK_ISSUER`, `CLERK_AUTHORIZED_PARTIES` | Verified social routes only; exact trusted HTTPS issuer/origins |
| `DIAGRAM_ASSEMBLY_ENABLED` | false; requires schema, Node/Chromium and preflight |
| `LEARNER_APPROVAL_ENABLED` | false; requires secret, schema and explicit mail worker |
| `ILLUSTRATIONS_ENABLED` | false; separate image-provider path |

Provider API keys are private environment values. Settings also read safety/embedding options in their modules; consult source/templates for the complete contract. Budget limits count workflows rather than provider tokens or currency. Configure spending controls at the provider too.

## Migrations and storage

Apply migrations explicitly in filename order using the existing Supabase workflow or an authorized connection. Do not execute against a shared database just to test setup. Never edit already-applied SQL.

| Migration suffix | Adds |
| --- | --- |
| 001 | Core application records, private schema and lobby commands |
| 002 | Job leases/usage and optional Realtime support |
| 003 | Lobby cleanup during player data deletion |
| 004 | Persistent request limiting |
| 005 | Job performance indexes |
| 006 | Learner-request workflow |
| 007 | Saved quiz attempt/history support |
| 008 | Frozen diagram templates/races |
| 009 | Diagram source lifecycle invalidation |
| 010 | Economy event index |
| 011 | Game invitation storage |

Supabase-compatible `anon`, `authenticated` and `auth.uid()` prerequisites are required. Keep `api_private` outside exposed Data API schemas. SQLite creates its own tables on opening; that is separate from PostgreSQL migrations.

Back up PostgreSQL **and both SQLite stores**. Use a consistent SQLite backup procedure or stop writers; copying only a live main database file can miss WAL contents. Restore compatible code and all relevant stores together. There is no cross-store atomic backup/commit contract.

## Workers and optional features

Generation jobs require PostgreSQL and generation enablement:

```sh
uv run python -m app.worker --once
uv run python -m app.worker
```

The worker is separate from the API. Queuing does not itself call a model. A worker failure may retry and consume another attempt; idempotent result commit does not mean a provider was called only once.

For saved-diagram races, install Node 22 and renderer dependencies:

```sh
cd tools/diagram-renderer
npm ci --ignore-scripts --no-fund --no-audit
npx --no-install playwright-core install --with-deps chromium
```

Return to the backend root and run `uv run python -m app.diagram_preflight --probe`. Enable the flag only after migrations/dependencies pass. Keep browser sandboxing enabled for the intended runtime. See [lifecycle/readiness](DIAGRAM_LIFECYCLE_READINESS.md) for rollout and stale-race handling.

Learner requests need a private secret of at least 32 bytes and an explicitly operated mail worker. Follow [LEARNER_APPROVAL.md](LEARNER_APPROVAL.md), including authority grants and proof rules. Rotating the secret invalidates receipts/proofs without a key-version migration. This workflow never activates Clerk accounts.

## Checks and what they prove

```sh
uv sync --locked
uv run ruff check app learning_service scripts tests
uv run ruff format --check app learning_service scripts tests
uv run pytest -q
```

Without `TEST_DATABASE_URL`, database cases skip. Full database checks require a disposable name ending `_test`; the suite resets its private schema. CI uses PostgreSQL 17 and synthetic model calls. The separate renderer job installs Chromium and sets `DIAGRAM_RENDERER_TESTS=1`.

Tests cover domain/state behavior, RLS, transaction rollback, concurrency, job leases, repeat rewards and storage restart. They do not prove live model accuracy, Clerk configuration, real email delivery or hosted Supabase Realtime.

## Health and recovery

`GET /health` is process liveness. `GET /ready` checks repository readiness/durability and enabled diagram dependencies. It does not call Clerk/models/mail or prove all SQLite persistence.

| Failure | Next check |
| --- | --- |
| Startup rejects storage | Database URL, production/demo settings |
| Ready returns 503 | Schema/connection and enabled diagram preflight |
| Social verification fails | Issuer/JWKS, signed player/username claims, party and header agreement |
| Generation 503 | Enablement and configuration |
| Generation 429 | Per-player request/daily allowance and concurrency |
| Job stays queued | Worker process, lease/attempt limits and database |
| State split after deployment | Absolute SQLite paths and number of API hosts |
| Mutation 409 | Refetch current version; same retry keeps original key/body |

For rollback, stop incompatible writers/workers and restore compatible application code. Schema reversal needs a reviewed procedure; destructive down migrations are not automatic. For diagram rollback, disable the feature and preserve additive schema/cancellation state.

`DELETE /api/me/data` resets application data across stores and leaves rooms. It does not delete the Clerk account, and a later wallet read can create a fresh starter wallet. Do not equate reset with provider-account deletion or verified-consent revocation.
