# Parent–Child Bonding Game API

Backend-only FastAPI API for owned learning data, reviewed content, generation jobs,
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
on restart, and cannot be used in production. API calls need no login, token or auth configuration.

```sh
curl http://127.0.0.1:8000/api/me/profile
```

Optional `X-Player-ID: <UUID>` selects separate player state for profiles, practice and multiplayer.
Without it, requests share player `00000000-0000-4000-8000-000000000001`. The ID is caller-selected
and unverified: anyone can select the same ID and access or delete that partition's data.

## Supabase and durable storage

```sh
cp .env.example .env
# Configure DATABASE_URL and exact CORS origins. No Supabase Auth keys are needed.
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Apply the versioned SQL migrations in `supabase/migrations/` to a **development** project before
starting data routes. Migration/hosted database changes are explicit operator actions; application
startup never applies migrations. See [database setup and tests](docs/DATABASE.md).

`DATABASE_URL` is a private, server-only PostgreSQL credential with ownership of the migration
objects. Request transactions retain the existing `authenticated` SQL role and player-ID claims
for partitioned storage; this does not authenticate callers. Job claiming and quota reservation
use server-only database functions. Never put the database credential in browser configuration.

**Keep `api_private` outside Supabase's exposed Data API schemas.** It stores private text and
answer keys. Application data is accessed through the public FastAPI routes.

FastAPI has no password login, registration, session management, Bearer checks or authentication
provider calls. Swagger has no Authorize button or security locks. Frontend login/register remains
outside this repository and is unchanged. Learner approval keeps its request receipts and decision
proofs as a separate workflow; guardian mailbox filters are supplied by the caller and unverified.

## API and documentation

- [API.md](API.md): public API and player-state contract plus links to the full platform contract.
- [Platform API](docs/API_PLATFORM.md): learning, review, attempts, profiles, jobs, progress, rooms.
- [Game backend roadmap](docs/GAME_BACKEND_ROADMAP.md): pending Forbidden Clues/heads-up actions and tests.
- [Server performance](docs/SERVER_PERFORMANCE.md): pools, query optimization, worker resource use, and local measurements.
- [Database and verification](docs/DATABASE.md): migrations, roles, integration tests, operational limits.
- [Frontend integration](docs/FRONTEND_INTEGRATION.md): frontend source locations and integration boundary.
- [Architecture](docs/ARCHITECTURE.md): current module responsibilities and invariants.
- [Contributor instructions](AGENTS.md): backend scope and continuation workflow.

Swagger: http://127.0.0.1:8000/docs. OpenAPI: http://127.0.0.1:8000/openapi.json.
`GET /health` is process health; `GET /ready` verifies application storage and reports whether it is durable.

## Learning and generation

Upload PDF/photos → text is extracted and embedded for search by meaning → correct extracted pages →
confirm material → generate a draft lesson → review
and approve it → generate private practice quizzes or flashcards → record individual results.
Edits create new lesson IDs; historical attempts retain their original immutable question set.
Approval records **owner confirmation**, not verified guardian/teacher authority or independent fact checking.
Scored solo submissions also require an explicit question-set review/approval step.

Uploaded books are also embedded (`EMBEDDING_MODEL`, default `openrouter:openai/text-embedding-3-small`, using the same
`OPENROUTER_API_KEY`; a few cents at most per book) so a topic is found by meaning as well as by words. Leave
`EMBEDDING_MODEL` empty for keyword search only. See [Platform API](docs/API_PLATFORM.md#search-by-meaning-embeddings).

Chats can be remembered per player: `POST /api/memory/conversations`, then pass `conversation_id` to the chat endpoints. They are
saved in a SQLite file (`MEMORY_DB_PATH`, default `memory.db`), filed by subject, and long ones are summarised. See
[Saved chats](docs/API_PLATFORM.md#saved-chats-memory).

A player's home (rooms, item levels, positions), coin wallet, friends, visits and cheers are saved in a second SQLite
file (`HOME_DB_PATH`, default `home.db`): `GET /api/me/home`, then build, place and upgrade with coins. Demo players
and friends for a local run: `APP_ENV=development uv run python scripts/seed_home_demo.py`. See
[Home, wallet and friends](docs/API_PLATFORM.md#home-wallet-and-friends).

`POST /api/chat/image` explains 1-4 pictures, or answers a question about them when a `message` is sent (the model looks at them directly, nothing is stored, each picture is
checked for the learner's age first). `POST /api/chat` chats with an uploaded PDF (or without one): it answers from the book when that helps and reports `used_pdf` and the pages,
or, in `pdf_only` mode, only from the book. See [Platform API](docs/API_PLATFORM.md#chat-with-a-pdf).

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
uv run ruff check app learning_service scripts tests
uv run ruff format --check app learning_service scripts tests
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


## Deterministic quiz backend — 24 October 2026 addition

Existing private quizzes now support saved lifecycle/results, idempotent first-answer recording,
finish/review, independent all/missed retries, exact-snapshot history and review-card creation.
Owning authors can review, correct into a new immutable version, and confirm that snapshot.
See [non-AI quiz API](docs/QUIZ_PRACTICE_API.md). This adds no model calls, AI changes, frontend
changes, rewards, ranking or multiplayer. Existing production/generation contracts remain intact.


## Learner approval workflow — 24 October 2026 addition

An opt-in pending-request workflow now supports private status receipts, bounded notification
resends and request-specific guardian decisions with caller-selected player/mailbox context. See
[learner approval](docs/LEARNER_APPROVAL.md) for the five endpoints, migration 006, explicit mail
worker and authority-review bridge. This records decisions only; it does not activate accounts,
create Supabase accounts or establish mandatory app-wide guardian consent. Feature defaults
off. No frontend or hosted service was changed.


## Rebuild the diagram — 24 October 2026 addition

A new backend can freeze an existing reviewed lesson flowchart, remove its labels without
rerunning layout, and run a simultaneous room race with shuffled label trays and independent
submissions. Study/review and repeat rounds reuse the same snapshot with no model call.
See [diagram assembly](docs/DIAGRAM_ASSEMBLY.md) for fourteen routes, migration 008, Node/Chromium
setup and limits. This feature defaults off; no frontend, AI harness or hosted service was changed.


## Diagram lifecycle and readiness — 25 October 2026 addition

The first fix batch protects finished/expired results, atomically cancels active races when their
approved source changes, adds minimal current-host management, and makes `/ready` check enabled
diagram dependencies. Apply migration 009 after 008; nothing applies migrations automatically.
Use `uv run python -m app.diagram_preflight --probe` to check before enabling.
See [lifecycle, API, rollout and remaining work](docs/DIAGRAM_LIFECYCLE_READINESS.md).
This entry is append-only; no frontend or AI changes or hosted deployment are included.
