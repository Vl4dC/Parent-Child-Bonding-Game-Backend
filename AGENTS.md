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
- Supabase verifies identity; never trust an unverified decoded JWT or account-type metadata.
- Pass each user's Bearer token explicitly; the shared HTTP pool must not hold shared user sessions.
- Demo mode stays off by default and cannot start in production. It uses synthetic, process-local data.
- Preserve session isolation, expiry, refresh rotation/replay rejection, and logout revocation.
- `learner` is the child account label. Neither this label nor a parent label proves consent or linkage.
- Every future data endpoint needs its own ownership/authorization rules.
- Keep secret values out of validation errors, logs, examples, commits, and URLs.
- Preserve no-store auth responses, exact CORS origins, and rejection of service-role keys.
- Do not configure SMTP, paid services, deploy, or merge other repos merely because a plan lists them.

## Delivery

Inspect git status and preserve unrelated edits. Use `codex/` for feature branches.
Update API.md and relevant README/docs with endpoint or behavior changes in the same patch.
Use typed Pydantic request/response models and meaningful behavioral tests for new routes.

```sh
uv sync --locked
uv run ruff check app learning_service tests
uv run ruff format --check app learning_service tests
uv run pytest -q
```

Regenerate uv.lock with `uv lock` for dependency changes. Inspect the exact staged files before
publishing: only templates may include environment examples, and credentials must be placeholders
or the explicitly synthetic demo fixtures. Report implemented behavior, checks, and remaining limits.

## Unified platform

Read docs/API_PLATFORM.md and docs/DATABASE.md. Generation engines live in learning_service/;
there is one app factory, one root lockfile, and one test tree. All /api routes use verified identity
and an owned unit of work. Keep api_private out of exposed Supabase schemas; it contains answer keys.
Use new migrations rather than editing already-applied SQL. Never run destructive test fixtures
against a hosted/shared database. TEST_DATABASE_URL must name a disposable database ending in _test.
Keep the original learning-service/static artifacts unchanged under backend-only requests.
