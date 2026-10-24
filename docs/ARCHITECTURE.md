# Architecture and operations

## Layout

| Path | Responsibility |
| --- | --- |
| `app/main.py` | Factory, lifespan, routes, provider adapter, auth dependency, CORS, error handling |
| `app/config.py` | Environment parsing and demo/production safeguards |
| `app/models.py` | Validated requests and public response models |
| `app/demo.py` | Synthetic users and in-memory expiring sessions |
| `tests/test_auth.py` | Mocked provider integration and error/validation behavior |
| `tests/test_demo.py` | Demo isolation, expiry, refresh, logout, and mode boundaries |
| `uv.lock` | Reproducible dependency resolution |
| `.github/workflows/backend.yml` | Lint, formatting, and tests |

The authentication foundation was extracted from the main project's backend and its existing
`codex/backend-demo-auth` work (local documentation snapshot `3ec77a7`). This is a standalone
repository with a new Git history; it does not merge or modify those upstream branches.

## Configuration

Settings read `.env` relative to the current working directory. Run from the repository root.

| Variable | Meaning |
| --- | --- |
| `APP_ENV` | `development`, `test`, or `production`; default `production` |
| `DEMO_MODE` | Default false; true requires development/test |
| `SUPABASE_URL` | Supabase project URL; required outside demo mode |
| `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_…` key; required outside demo mode |
| `CORS_ORIGINS` | JSON array of exact origins; default `["http://localhost:3000"]` |

Demo requests never fall back to Supabase. Supabase mode rejects demo-prefixed tokens and does not
register `/auth/demo-accounts`. Health reports process/mode health; it is not a database or provider
readiness check. Supabase mode shares an HTTP pool, with each request's token supplied separately.

## Before a real deployment

Configure HTTPS, exact frontend origins, provider confirmation/recovery URLs, an email sender,
per-client rate limiting, and monitoring. Keep authentication bodies and headers out of logs.
Validate real signup, confirmation, recovery, refresh, and logout with an authorized test account.
The mocked suite does not establish live email delivery. No deployment is included in this repo setup.

Future private child/family data needs explicit ownership, verified linkage/consent rules,
persistence, migrations, and access tests. No family relationship or consent workflow is implemented.

## Repository access

Anyone can read or clone this public repository. Maintainers can invite contributors through
GitHub repository Settings → Collaborators → Add people. An actual invitation needs the recipient's
GitHub username/email; the collaborator settings page is not itself an invitation token.
Frontend-repository access must be granted separately by its owners.
