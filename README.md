# Parent–Child Bonding Game API

Backend-only FastAPI service for the Parent–Child Bonding Game. This repository owns API endpoints,
validation, authentication, backend tests, and Markdown documentation.

Frontend source and product context live in the [main repository](https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game).
Read that repository before implementing new API requirements. It currently requires GitHub access.

## Quick start: offline demo

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run from this repository's root:

```sh
cp .env.demo.example .env
uv sync --locked
uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000 --no-access-log
```

Use one worker. Demo sessions are held in memory and disappear on restart. No Supabase account,
SMTP sender, or database is required. Demo mode refuses production startup.

| Account | Email | Synthetic password |
| --- | --- | --- |
| Parent | `parent@demo.example.com` | `DemoParent123!` |
| Child | `child@demo.example.com` | `DemoChild123!` |

```sh
curl -s http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"parent@demo.example.com","password":"DemoParent123!"}'
```

Use the returned access token as `Authorization: Bearer <access_token>` for `/auth/me` and
`/auth/logout`. Refresh with `/auth/refresh`; replace both tokens after every successful refresh.

## API documentation

- [API.md](API.md): endpoints, JSON examples, responses, errors, and session lifecycle.
- [Frontend integration](docs/FRONTEND_INTEGRATION.md): source locations, role mapping, and future contracts.
- [Backend product requirements](docs/PRODUCT_BACKEND_REQUIREMENTS.md): moved backend planning context.
- [Original authentication setup](docs/ORIGINAL_AUTH_SETUP.md): archived provider/email setup notes.
- [Architecture and operations](docs/ARCHITECTURE.md): configuration, boundaries, and release checks.
- [Contributor instructions](AGENTS.md): backend-only scope and main-repository lookup workflow.
- Local interactive documentation: [Swagger](http://127.0.0.1:8000/docs),
  [ReDoc](http://127.0.0.1:8000/redoc), [OpenAPI JSON](http://127.0.0.1:8000/openapi.json).

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Process health and auth mode |
| GET | `/auth/demo-accounts` | Demo-only synthetic account list |
| POST | `/auth/register` | Request signup confirmation |
| POST | `/auth/login` | Sign in |
| POST | `/auth/verify` | Verify signup/recovery OTP or hash |
| POST | `/auth/resend` | Resend signup confirmation |
| POST | `/auth/refresh` | Rotate session tokens |
| GET | `/auth/me` | Verified current identity |
| POST | `/auth/logout` | Revoke current session |
| POST | `/auth/forgot-password` | Request recovery email |
| PUT | `/auth/password` | Update authenticated user's password |

Demo mode supports login, refresh, identity, and logout. Registration and email/password mutations
return 409 in demo mode for valid requests. Input and token validation still apply.

## Supabase mode

```sh
cp .env.example .env
# Set your own SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in .env.
uv sync --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The template defaults to `APP_ENV=production` and `DEMO_MODE=false`. Use a publishable key;
secret/service-role keys are rejected. Set `CORS_ORIGINS` to a JSON array of exact frontend origins.
Supabase manages real accounts, passwords, email confirmation, and sessions. Configure a real
sender and confirmation/recovery flow before promising public email delivery; SMTP setup and live
email verification are not included here. Keep confirmation enabled.

## Quality checks

```sh
uv sync --locked
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
```

GitHub Actions runs these checks on pushes and pull requests. Tests exercise mocked Supabase
requests and isolated offline sessions, without sending emails or requiring secrets.

## Current scope

This is a tested authentication foundation. Multiplayer rooms, game state, learning content,
uploads, progress, rewards, persistent profiles, verified family relationships, and guardian consent
are future API work. Frontend prototypes do not establish that these backend features exist.
Account labels and demo family metadata do not grant access to another user's data.

The repository is public; the API has not been deployed. Real deployments need HTTPS, per-client
rate limiting, a browser session strategy, configured email delivery, and operational monitoring.
Never commit local `.env` files, real credentials, tokens, logs, or user data.
