# Archived authentication setup from the frontend repository

Moved from the frontend main branch at `bad7e5a` on 24 October 2026.
This is historical context; use the root README.md and API.md for current commands and behavior.
Dashboard observations and email settings below have not been rechecked by this move.

# Authentication backend

FastAPI API backed by Supabase Auth. The existing Next.js frontend is unchanged.
No custom user table, database password, service-role key, or FastAPI password database is needed:
Supabase owns password hashing, users, email confirmation, sessions, and recovery.

## Run locally

Install [uv](https://docs.astral.sh/uv/), then from this directory:

```sh
cp .env.example .env
# Fill SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY from the project's Connect dialog.
uv sync --locked
uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000 --no-access-log
```

The configured local `.env` is ignored by Git; teammates must create their own.
Use Python 3.12 or later. Open http://localhost:8000/docs for the interactive API contract.
`GET /health` checks process health, not Supabase availability. Missing/invalid configuration fails startup.
`uv run pytest -q` runs the mocked integration suite. `uv run ruff check app tests` and
`uv run ruff format --check app tests` check style.

## API contract

| Method | Endpoint | Input / result |
| --- | --- | --- |
| POST | `/auth/register` | `email`, `password`, `display_name`, `account_type` (`learner` or `parent`); returns generic 202 confirmation instructions |
| POST | `/auth/login` | `email`, `password`; returns access/refresh tokens, expiry, and user |
| POST | `/auth/verify` | `type` (`signup` or `recovery`) and either `email` + `token`, or `token_hash`; returns a session |
| POST | `/auth/resend` | `email`; requests another signup confirmation email |
| POST | `/auth/refresh` | `refresh_token`; returns a rotated session |
| GET | `/auth/me` | Bearer access token; returns identity verified by Supabase |
| POST | `/auth/logout` | Bearer access token; revokes the current refresh session; returns 204 |
| POST | `/auth/forgot-password` | `email`; requests recovery email with generic 202 response |
| PUT | `/auth/password` | Bearer access token and `password`; updates that user's password |

Passwords for registration/update must be 8–128 characters; stronger project-level rules still apply.
Passwords are not trimmed. Display names are trimmed. User metadata is a self-declared display
preference, **not** a verified age, guardian relationship, or authorization role. Parent/child linking,
consent, game data, uploads, and profile authorization are separate future work.

Use `Authorization: Bearer <access_token>` for protected endpoints. No auth cookies are set by this API.
A future browser frontend needs an explicit secure session strategy; do not persist refresh tokens in
localStorage by default. A same-origin server-side session layer is recommended when UI work begins.
Replace the refresh token after every successful refresh and serialize refresh requests per session.
On logout, clear client tokens; issued access JWTs can remain valid until expiry even after refresh-token
revocation. Password updates also require the client to handle session/token changes and sign out as appropriate.

No response containing authentication data should be cached. The API adds `Cache-Control: no-store`.
Validation errors and upstream failures omit raw submitted values and provider debug messages.
CORS is restricted to the configured exact origins and does not enable cookie credentials.
CORS is not authentication or a rate limiter. Supabase's upstream auth rate limits remain in force;
add per-client edge limits before public deployment. Do not log request bodies, auth headers, tokens,
or email-link query strings. The example server command disables access logging.

## Supabase project setup and email limitation

Inspected project: **Partygame for ed**. Email provider and new signups are enabled;
email confirmation is required. No security setting was disabled to make testing easier.
The project uses Supabase's default mailer; no custom SMTP credentials were supplied.
Its dashboard requires custom SMTP (or a paid email-template option) for editing templates.
Default mail delivery also has recipient restrictions and low sending limits; public delivery is not
configured by this change. See [Supabase's default SMTP restrictions](https://supabase.com/docs/guides/auth/auth-smtp).

The API supports both OTP verification and token-hash verification. Default confirmation/recovery
emails contain a link, not necessarily a visible OTP. For developer testing, the link's `token` query
parameter is the **token hash**, which can be submitted to `POST /auth/verify` with the matching type.
Do not click the link first: verification consumes it. Treat the link/hash as a credential; never commit it.
A normal user should not need to extract a hash. The final public email/redirect UX is pending SMTP
and the later frontend, not completed by this backend-only change.

Once a sender is available, add the following to the matching email templates to support code entry
without browser callbacks:

```html
<p>Your verification code is: <strong>{{ .Token }}</strong></p>
```

Signup: register → read signup code → verify with `type: signup` → use returned session.
Recovery: forgot-password → read recovery code → verify with `type: recovery` →
PUT `/auth/password` using the returned access token. An ordinary signed-in user may also update
their own password without requesting recovery.

Keep email confirmation enabled. Keep anonymous sign-in and unused OAuth providers disabled.
The existing Site URL is `http://localhost:3000`; a local callback URL is allowlisted for later UI work,
but there is no frontend auth callback implementation in this branch. OTP/hash verification needs
no redirect. Before deployment, configure the actual HTTPS frontend Site URL and exact callback URLs,
SMTP sender, production backend environment, allowed CORS origins, and TLS. No production domain
was provided, so none was guessed or deployed. No additional Supabase GitHub integration is needed
for API connectivity; setting up that integration would grant separate repository permissions.

## What was verified

The test suite mocks Supabase to exercise session isolation, login and refresh grants, registration,
verification methods, token enforcement, logout, password updates, validation redaction, error handling,
and CORS. Live smoke checks validate configuration against Supabase and reject missing/invalid tokens.
No real signup, mailbox confirmation, or successful password reset has been claimed without a test
mailbox/sender. No frontend, game engine, OAuth, family linking, or application tables are implemented.
