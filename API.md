# Backend API reference

The authentication reference below is retained. The unified application also exposes learning,
review, profiles, attempts, progress, jobs, and private rooms: see [Platform API](docs/API_PLATFORM.md).
Python 3.13+ and the root environment are now required. All data routes use verified Bearer identity.

Source of truth: `app/main.py`, `app/models.py`, and the running `/openapi.json`.
Base URL in the documented local command: `http://127.0.0.1:8000`.
Swagger docs: `/docs`; ReDoc: `/redoc`. All requests below use JSON unless noted.

## Modes and endpoint availability

| Method | Path | Success | Supabase mode | Demo mode |
| --- | --- | --- | --- | --- |
| GET | `/health` | 200 | Process/mode health | Process/mode health |
| GET | `/auth/demo-accounts` | 200 | Route absent (404) | Lists synthetic users, no passwords |
| POST | `/auth/register` | 202 | Request registration/confirmation | Unavailable (409) |
| POST | `/auth/login` | 200 | Password login | Seeded-account login |
| POST | `/auth/verify` | 200 | Verify signup/recovery code or hash | Unavailable (409) |
| POST | `/auth/resend` | 202 | Request signup confirmation again | Unavailable (409) |
| POST | `/auth/refresh` | 200 | Refresh Supabase session | Rotate demo tokens |
| GET | `/auth/me` | 200 | Verified current user | Current demo user |
| POST | `/auth/logout` | 204, empty body | Revoke current refresh session | Revoke both current tokens |
| POST | `/auth/forgot-password` | 202 | Request recovery email | Unavailable (409) |
| PUT | `/auth/password` | 200 | Update authenticated user's password | Unavailable (409) |

Unavailable demo actions return 409 for otherwise valid requests; invalid input still returns 422,
and protected routes require a Bearer token. No demo endpoint creates real users or sends emails.

## Request bodies

Registration:

```json
{
  "email": "learner@example.com",
  "password": "ExamplePassword123!",
  "display_name": "Sam",
  "account_type": "learner"
}
```

`account_type` accepts `learner` or `parent`. The child demo uses `learner`.
Display names are 1–60 characters, require non-whitespace content, and are trimmed on registration.
Registration/update passwords are 8–128 characters; provider policies may impose more constraints.
Login accepts existing passwords of 1–128 characters. Password whitespace is preserved.
Unknown JSON properties are rejected. Metadata is not an authorization role.

Login (published offline demo credentials; only when demo mode is enabled):

```json
{"email": "parent@demo.example.com", "password": "DemoParent123!"}
```

Refresh:

```json
{"refresh_token": "REPLACE_WITH_CURRENT_REFRESH_TOKEN"}
```

`refresh_token` must be 1–4096 characters. Serialize refresh operations per session and replace
both returned tokens; never reuse a rotated token.

Verification accepts exactly one of these forms:

```json
{"email": "learner@example.com", "token": "123456", "type": "signup"}
```

```json
{"token_hash": "REPLACE_WITH_EMAIL_TOKEN_HASH", "type": "recovery"}
```

`type` is `signup` or `recovery`. Email+token must be supplied together, without `token_hash`;
otherwise supply `token_hash` without email or token. Tokens are 6–10 characters; hashes are
1–512 characters. These endpoints do not provide an email sender or frontend callback.

Resend and forgot-password:

```json
{"email": "learner@example.com"}
```

Password update (also requires the authenticated session's access token):

```json
{"password": "DifferentExamplePassword123!"}
```

## Responses and authentication

Protected routes use `Authorization: Bearer <access_token>`: `/auth/me`, `/auth/logout`, and
`/auth/password`. No auth cookies are set; a future browser UI still needs a secure storage/session
strategy. Identity is established by the token, never by a caller-supplied user ID or metadata.

Login, verification, and refresh return the same session shape:

```json
{
  "access_token": "OPAQUE_ACCESS_TOKEN",
  "refresh_token": "OPAQUE_REFRESH_TOKEN",
  "token_type": "bearer",
  "expires_in": 3600,
  "expires_at": 1800000000,
  "user": {
    "id": "00000000-0000-4000-8000-000000000001",
    "email": "parent@demo.example.com",
    "email_confirmed_at": null,
    "user_metadata": {
      "display_name": "Alex (Demo Parent)",
      "account_type": "parent",
      "demo": true,
      "demo_family_id": "demo-family"
    }
  }
}
```

Token/expiry values above are illustrative. `expires_at` can be null when absent upstream.
`/auth/me` and `/auth/password` return the `user` object alone. User `email` and
`email_confirmed_at` are nullable. `user_metadata` defaults to an empty object and may contain
other untrusted values in Supabase mode. The demo does not claim verified email addresses.

Register/resend/forgot-password return `{"message": "..."}` with generic wording; 202 is not
proof an email reached a recipient. Duplicate-registration and missing-user mail responses are
sanitised. `/auth/logout` returns an empty 204 response; do not try to parse it as JSON.

Health example:

```json
{"status": "ok", "auth_mode": "demo"}
```

The other mode value is `supabase`. Health does not call the upstream service.
All `/auth/` responses carry no-store/no-cache and no-referrer headers, including error responses.

## Session lifecycle differences

| Behaviour | Demo | Supabase |
| --- | --- | --- |
| Identity storage | Two synthetic fixtures | Supabase-managed users |
| Token validation | Random tokens looked up in current process | Supabase verifies access token via `/auth/v1/user` |
| Access expiry | One hour from issuance | Provider session response |
| Refresh expiry | 24 hours from latest issuance | Provider configuration |
| Refresh effect | Immediately invalidates both old tokens | Provider-managed refresh rotation; old access JWT may remain valid |
| Logout | Immediately removes the current demo session | Local-scope provider logout; access JWT may remain valid until expiry |
| Restart/reload | All sessions disappear | Real sessions remain provider-managed |
| Multiple workers | Unsupported | Independent requests share only an HTTP pool per process |

A new demo login is a separate session. Logout does not log out the other demo account or all
sessions for the same user. At most 1,000 non-expired refresh sessions are retained per demo process.
Supabase mode rejects demo access/refresh prefixes; demo mode never falls back to Supabase.

## Errors

Typical error response:

```json
{"detail": "Invalid demo credentials or expired demo session."}
```

Validation response (submitted values and secret contexts are omitted):

```json
{"detail": [{"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"}]}
```

Wording above is illustrative; clients should primarily handle status and response shape.

| Status | Meaning |
| --- | --- |
| 400 | Provider rejected a request/password update; public message is sanitised |
| 401 | Missing/invalid credentials, expired token/code, refresh replay, or demo token in Supabase mode |
| 403 | Provider reports email not confirmed |
| 404 | Demo account-list route does not exist in Supabase mode |
| 409 | Email/account mutation unavailable in demo mode |
| 422 | Invalid JSON/body fields or unsupported verification combination |
| 429 | Provider rate limit or maximum active demo sessions reached |
| 502 | Successful upstream response could not be parsed as JSON |
| 503 | Upstream transport failure or provider server error |

CORS permits configured exact origins, GET/POST/PUT/PATCH/DELETE, Authorization and Content-Type headers;
it does not enable cookie credentials or replace authentication. Upstream Supabase rate limits
exist; application/edge per-client limiting has not been implemented. Never log real passwords,
Bearer/refresh tokens, email hashes, or request bodies containing them.


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
activate a learner or grant family access. Earlier auth contracts and existing signup behavior
remain unchanged. Creation requests require a private random 32–128 character Idempotency-Key.
