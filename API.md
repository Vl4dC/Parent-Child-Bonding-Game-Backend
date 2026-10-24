# Backend API reference

The backend exposes owned learning, review, profiles, attempts, progress, jobs, and private rooms:
see [Platform API](docs/API_PLATFORM.md). Python 3.13+ and the root environment are required.

Base URL for local development: `http://127.0.0.1:8000`. Swagger: `/docs`; ReDoc: `/redoc`.
The running `/openapi.json` is generated from the actual routes and typed models.

## Identity boundary

Login, signup and session management are handled through the client's Supabase integration.
The backend no longer accepts passwords, issues access/refresh tokens, sends account confirmation
or recovery emails, or exposes account/session endpoints. The former `/auth/register`, `/auth/login`,
`/auth/verify`, `/auth/resend`, `/auth/refresh`, `/auth/me`, `/auth/logout`, `/auth/forgot-password`,
`/auth/password` and demo account-list routes return 404 and are absent from OpenAPI.

Protected data routes still require `Authorization: Bearer <access_token>`. In Supabase mode,
the backend verifies each supplied token using Supabase's `/auth/v1/user`; it never trusts an
unverified JWT, caller-supplied user ID or account-type metadata. The shared HTTP pool does not
store user sessions. No auth cookies are set. Client login/signup, token refresh, sign-out and
recovery must use the identity provider directly.

Explicit local demo mode accepts only the fixed synthetic tokens `demo_parent` and `demo_child`.
It has no passwords or session lifecycle, uses ephemeral data and cannot start in production.
Supabase mode rejects every `demo_` token before contacting the provider.

Learner approval remains a separate opt-in pending-request/guardian-decision workflow, including
its `/auth/registration/*` routes. It does not create accounts or establish a login session.

## Health, errors and transport

`GET /health` returns `{"status":"ok","auth_mode":"supabase"}` (or `"demo"`) without
calling the provider. `GET /ready` checks storage and reports whether it is durable.

Missing/invalid/expired data-route credentials return 401. Provider rate limits return 429;
transport or provider server failures return 503; malformed provider identity responses return 502.
Errors omit raw provider messages and submitted values. Validation errors return 422 without
submitted-value or sensitive-context fields.

All `/api/` and `/auth/` responses carry no-store/no-cache and no-referrer headers, including errors.
CORS permits configured exact origins, GET/POST/PUT/PATCH/DELETE, Authorization and Content-Type
headers. It does not enable cookie credentials or replace identity verification. Data remains
isolated by verified owner. Never log Bearer tokens or include them in URLs.

## Non-AI quiz lifecycle — 24 October 2026 addition

Eleven deterministic operations now supplement existing quiz contracts: resume/results, strict
idempotent answers, finish/review, all/missed retries, snapshot history, idempotent review cards,
author review, question revisions and snapshot-bound approval. See
[QUIZ_PRACTICE_API.md](docs/QUIZ_PRACTICE_API.md) and the additive
[OpenAPI supplement](docs/quiz-practice.openapi.json). AI generation/safety code is unchanged.


## Learner approval addition — 24 October 2026

Five opt-in request/status/resend/guardian-decision routes have been added. See
[LEARNER_APPROVAL.md](docs/LEARNER_APPROVAL.md) and the additive
[OpenAPI schema](docs/learner-approval.openapi.json). The feature defaults off; approval does not
activate a learner or grant family access. This workflow is independent of the removed login/signup
and session endpoints. Creation requests require a private random 32–128 character Idempotency-Key.


## Diagram assembly race — 24 October 2026 addition

Fourteen authenticated operations reuse saved lesson flowcharts as frozen SVG label puzzles for
simultaneous private-room play. See [DIAGRAM_ASSEMBLY.md](docs/DIAGRAM_ASSEMBLY.md) and the
[additive OpenAPI](docs/diagram-assembly.openapi.json). Model source is unchanged; game grading
and timing are deterministic/server-owned. Feature defaults off; frontend and deployment are pending.
