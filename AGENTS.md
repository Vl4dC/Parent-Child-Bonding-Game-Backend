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
- Every `/api` request needs a valid sign-in token: a Clerk session JWT sent as `Authorization: Bearer`,
  verified in `app/auth.py` (RS256, issuer, expiry, the provider's public keys). The player is the token's
  `player_id` claim and nothing else; the `X-Player-ID` header is not read. The only unauthenticated `/api`
  routes are the learner-registration intake (a child signing up has no token yet; it works on a private
  receipt). `tests/test_auth.py` pins that list, so a new public route fails a test.
- Preserve request-local transactions, gameplay membership/host rules, expiry and idempotency.
- Demo mode stays off by default and cannot start in production. It controls ephemeral storage only.
- The API verifies sign-in tokens; it never issues them. Do not add login, registration, session, password
  or token-issuing endpoints, or Supabase Auth configuration. Sign-in belongs to the provider.
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
partition and a request-local unit of work; no caller identity is verified. Keep api_private out of
exposed Supabase schemas; it contains answer keys.
Use new migrations rather than editing already-applied SQL. Never run destructive test fixtures
against a hosted/shared database. TEST_DATABASE_URL must name a disposable database ending in _test.
Keep the original learning-service/static artifacts unchanged under backend-only requests.
