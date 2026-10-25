# QuestNest backend

FastAPI backend for learning from uploaded notes, saved practice, chat, private games, rewards and homes. The [frontend repository](https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game) owns the web application and Clerk sign-in.

## Run a local API demo

Python 3.13+ and uv are required. Run from this repository root:

```sh
cp .env.demo.example .env
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

Generation defaults off: no model calls. With no database URL, application records use memory and reset on restart. Chat/home SQLite stores still default to files outside test mode; set their paths to `:memory:` for a fully ephemeral demo. Demo mode cannot run in production.

Open [Swagger](http://127.0.0.1:8001/docs), [OpenAPI](http://127.0.0.1:8001/openapi.json) or call `GET /api/me/profile`. Port 8001 matches frontend examples; the backend does not require that particular port.

## Durable setup

Copy `.env.example` to `.env`, configure a private PostgreSQL `DATABASE_URL`, and explicitly apply the ordered SQL in `supabase/migrations/`. The schema expects Supabase-compatible roles and `auth.uid()`; a plain empty PostgreSQL database needs equivalent prerequisites. Startup never applies migrations.

Retain both `MEMORY_DB_PATH` and `HOME_DB_PATH` on persistent storage. They hold conversations, challenges/matches, homes, wallets and social data outside PostgreSQL. Keep `api_private` out of exposed Supabase Data API schemas because it contains private text and answer keys.

Enable generation only after configuring provider credentials and budget. Long work can use durable jobs and a separate worker:

```sh
uv run python -m app.worker
```

Clerk verification applies only to `/api/social/*`. Configure `CLERK_ISSUER`, exact `CLERK_AUTHORIZED_PARTIES`, and signed `player_id`/`username` session claims. Other routes use an unverified player UUID; no backend login/signup service is provided.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Modules, storage, request flow and trust boundaries |
| [Decisions](docs/DECISIONS.md) | Reasons, tradeoffs and revisit conditions |
| [Operations](docs/OPERATIONS.md) | Configuration, workers, verification and recovery |
| [API index](API.md) | Endpoint families and detailed contracts |
| [Platform API](docs/API_PLATFORM.md) | Learning, chat, home and game endpoints |
| [Database](docs/DATABASE.md) | Migrations, RLS and disposable database tests |
| [Quiz lifecycle](docs/QUIZ_PRACTICE_API.md) | First answers, immutable snapshots, retries and review |
| [Economy](docs/ECONOMY.md) | Wallet, quests, purchases and reward eligibility |
| [Verified social](docs/FRONTEND_SOCIAL_CONTRACT.md) | Friends, presence and live duels |
| [Lobby invitations](docs/FRIENDS_PRESENCE_INVITES.md) | Separate legacy presence/invitation APIs |
| [Saved diagrams](docs/DIAGRAM_ASSEMBLY.md) / [lifecycle](docs/DIAGRAM_LIFECYCLE_READINESS.md) | Frozen diagrams, races and readiness |
| [Learner requests](docs/LEARNER_APPROVAL.md) | Optional receipts/proofs and approval worker |
| [Performance](docs/SERVER_PERFORMANCE.md) | Pools, query projections and worker execution |
| [Frontend context](docs/FRONTEND_INTEGRATION.md) | Repository ownership and integration references |

Older feature documents preserve implementation history. Statements such as “no frontend change” describe that backend patch, not the current whole product. The reviewed frontend now connects core learning, economy, home, social and supported games.

## Checks

```sh
uv sync --locked
uv run ruff check app learning_service scripts tests
uv run ruff format --check app learning_service scripts tests
uv run pytest -q
```

Database cases skip without `TEST_DATABASE_URL`. Full database verification requires a disposable database ending in `_test`; its private schema is reset. Never use a hosted/shared database. CI provisions PostgreSQL 17 and separately checks the optional renderer. Tests fake model calls; they do not prove real model quality or email delivery.

## Current limits

Legacy `X-Player-ID` is a partition selector, not authentication: a reachable caller can select another UUID. Parent/learner labels do not prove family linkage. Content approval records owner confirmation, not independent fact checking. Model-backed safety exists, but age is user-supplied and correctness is not guaranteed.

SQLite stores target a single application host; independent files split state. Cross-store operations are not distributed transactions. Weekly play is a preview/sample feature. Saved-diagram assembly and learner requests default off and need their own dependencies. `/health` reports process health; `/ready` checks the application repository plus enabled diagram prerequisites, not every external service.

Reviewed 25 October 2026 at backend `a85ec74`, with frontend context `67b663f`.
