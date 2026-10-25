# Backend contributor instructions

## Scope and source of context

This repository is backend only. Implement API endpoints, models, backend services, tests, and
Markdown documentation. Do not create frontend components here.

For every future feature request, first inspect the relevant frontend and product context in
https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game (the main repository).
Read its applicable AGENTS.md and relevant source files. Fetch its current default branch and
inspect any branch explicitly named by the user. Local uncommitted frontend changes may also be
relevant; preserve them. Do not treat a stale checkout as current remote evidence. If access fails,
say so and identify any local snapshot used instead of inventing frontend contracts.

Read README.md, API.md, docs/FRONTEND_INTEGRATION.md, and the actual source/tests before editing.
The product plan is a backlog, not proof of implemented features. Keep changes to the user's scope.

## Invariants

- Use the `create_app` factory and run commands from this repository's root.
- Existing routes outside `/api/social` have no authentication requirement. By explicit user decision
  on 25 October 2026, `/api/social/*` requires verified Clerk session JWTs. No login/signup or
  provider-account management endpoints are introduced.
- On legacy routes, optional X-Player-ID selects a UUID data partition; absent headers use the shared default player.
  This is not a verified identity or an access-control boundary. Never describe it as one.
- Preserve request-local transactions, gameplay membership/host rules, expiry and idempotency.
- Demo mode stays off by default and cannot start in production. It controls ephemeral storage only.
- Keep token verification scoped to `/api/social`; its signed player_id claim selects existing
  learning/wallet data. Reject a conflicting X-Player-ID. Do not add auth requirements to legacy
  routes or reintroduce login/account-management endpoints or Supabase Auth configuration.
- `learner` is the child account label. Neither this label nor a parent label proves consent or linkage.
- Keep data grouped by the selected player and preserve existing gameplay rules.
- Keep secret values out of validation errors, logs, examples, commits, and URLs.
- Preserve no-store API responses and exact CORS origins.
- Do not configure SMTP, paid services, deploy, or merge other repos merely because a plan lists them.

## Delivery

Inspect git status and preserve unrelated edits. Use `codex/` for feature branches.
Update API.md and relevant README/docs with endpoint or behavior changes in the same patch.
Use typed Pydantic request/response models and meaningful behavioral tests for new routes.

```sh
uv sync --locked
uv run ruff check app learning_service scripts tests
uv run ruff format --check app learning_service scripts tests
uv run pytest -q
```

Regenerate uv.lock with `uv lock` for dependency changes. Inspect the exact staged files before
publishing: only templates may include environment examples, and credentials must be placeholders
or the explicitly synthetic demo fixtures. Report implemented behavior, checks, and remaining limits.

## Unified platform

Read docs/API_PLATFORM.md and docs/DATABASE.md. Generation engines live in learning_service/;
there is one app factory, one root lockfile, and one test tree. Data routes use the selected player
partition and a request-local unit of work; legacy callers are not verified, while /api/social uses
verified Clerk claims as described above. Keep api_private out of
exposed Supabase schemas; it contains answer keys.
Use new migrations rather than editing already-applied SQL. Never run destructive test fixtures
against a hosted/shared database. TEST_DATABASE_URL must name a disposable database ending in _test.
Keep the original learning-service/static artifacts unchanged under backend-only requests.
