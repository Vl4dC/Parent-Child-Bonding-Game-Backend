# Parent–Child Bonding Game API

Backend-only FastAPI API for authentication, owned learning data, reviewed content, generation jobs,
and private lobbies. Frontend context lives in the
[main repository](https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game).

## Local demo without services

Python **3.13+** and [uv](https://docs.astral.sh/uv/) are required. Run from this repository's root:

```sh
cp .env.demo.example .env
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Generation defaults off, so this command makes no model calls. Demo storage is in memory, resets
on restart, and cannot be used in production. Parent login: `parent@demo.example.com` /
`DemoParent123!`; child login: `child@demo.example.com` / `DemoChild123!`. These are synthetic fixtures.

```sh
curl http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"parent@demo.example.com","password":"DemoParent123!"}'
```

Use the returned access token in `Authorization: Bearer <access_token>`. The API sets no auth cookies.

## Supabase and durable storage

```sh
cp .env.example .env
# Configure Supabase Auth, DATABASE_URL, and exact CORS origins.
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Apply the versioned SQL migrations in `supabase/migrations/` to a **development** project before
starting data routes. Migration/hosted database changes are explicit operator actions; application
startup never applies migrations. See [database setup and tests](docs/DATABASE.md).

`DATABASE_URL` is a private, server-only PostgreSQL credential with ownership of the migration
objects. Request transactions switch to `authenticated` and set verified identity claims so RLS
applies. Job claiming and quota reservation use server-only database functions. No service-role API
key is added to the authentication flow. Never put the database credential in browser configuration.

**Keep `api_private` outside Supabase's exposed Data API schemas.** It stores private text and
answer keys. Application data is accessed through the authenticated FastAPI routes.

Real registration/recovery delivery still needs a configured email sender and confirmation flow.
Email confirmation stays enabled; SMTP work and live email verification are not part of this change.

## API and documentation

- [API.md](API.md): authentication reference plus links to the full platform contract.
- [Platform API](docs/API_PLATFORM.md): learning, review, attempts, profiles, jobs, progress, rooms.
- [Database and verification](docs/DATABASE.md): migrations, roles, integration tests, operational limits.
- [Frontend integration](docs/FRONTEND_INTEGRATION.md): frontend source locations and integration boundary.
- [Architecture](docs/ARCHITECTURE.md): current module responsibilities and invariants.
- [Contributor instructions](AGENTS.md): backend scope and continuation workflow.

Swagger: http://127.0.0.1:8000/docs. OpenAPI: http://127.0.0.1:8000/openapi.json.
`GET /health` is process health; `GET /ready` verifies application storage and reports whether it is durable.

## Learning and generation

Upload PDF/photos → correct extracted pages → confirm material → generate a draft lesson → review
and approve it → generate private practice quizzes or flashcards → record individual results.
Edits create new lesson IDs; historical attempts retain their original immutable question set.
Approval records **owner confirmation**, not verified guardian/teacher authority or independent fact checking.
Scored solo submissions also require an explicit question-set review/approval step.

Set `GENERATION_ENABLED=true` only after configuring the model provider and budget. Direct generation
routes remain available for compatibility. Durable jobs are preferred for long operations:

```sh
uv run python -m app.worker --once
# or run continuously:
uv run python -m app.worker
```

Workers require PostgreSQL and explicit generation enablement. They do not start automatically with
the API. Jobs have idempotency keys, a ten-pending-job limit per owner, retry limits, and expiring leases.
Workflow attempt budgets count failures too; one workflow can make several provider calls. These
limits are not a currency spending guarantee: configure the provider's own budget as well.

## Checks

```sh
uv sync --locked
uv run ruff check app learning_service tests
uv run ruff format --check app learning_service tests
uv run pytest -q
```

Without `TEST_DATABASE_URL`, database-specific cases skip. For the full suite:

```sh
# This MUST be a disposable database whose name ends in _test. Its api_private schema is reset.
TEST_DATABASE_URL=postgresql://localhost/questnest_api_test uv run pytest -q
```

CI uses a disposable PostgreSQL service and runs the full test tree. Model and vision calls are
faked in tests; no real emails or paid generation are exercised. PostgreSQL tests include real RLS,
transactions, quotas, room concurrency, job retries, and restart persistence. They use a test-only
`auth.uid()` compatibility function; they do not prove a hosted Supabase Realtime connection.

## Deliberately pending

Private room APIs support lobby membership, readiness, host transfer, expiry, change notifications,
and reconnect snapshots. They do not start games or implement public matchmaking, teams, scoring,
rankings, or rewards. Private Supabase notifications carry only a room ID; refetch the authorized snapshot.

Guardian approval, family access rules, coins/boxes/badges, purchases, base upgrades, individual game
rules, and the proposed AI age-safety gate remain pending product decisions. Role labels grant no
access to another person's learning data. There is no deployment or frontend change in this work.

The old `learning-service/static/` files are retained unchanged as historical development artifacts;
the unified API does not serve them. The old Python project has been consolidated into
`learning_service/`, with one root dependency lock and one test suite.
