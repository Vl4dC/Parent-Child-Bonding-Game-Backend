# Clerk verification for FastAPI

Added 25 October 2026 following the user's switch to Clerk. This active production contract
supersedes older anonymous-API wording retained in existing documents. No login/signup/password,
account provisioning, session-cookie or frontend flow is implemented here. No AI source changed.

## Configuration

Production API startup requires durable PostgreSQL plus `CLERK_ISSUER` and nonempty
`CLERK_AUTHORIZED_PARTIES`. Example placeholders:

```env
APP_ENV=production
CLERK_ISSUER=https://your-instance.clerk.accounts.dev
CLERK_AUTHORIZED_PARTIES=["https://your-frontend.example"]
CORS_ORIGINS=["https://your-frontend.example"]
# Optional when your tokens have an audience:
# CLERK_AUDIENCE=your-api-audience
# Optional PEM public verification key instead of fetching issuer JWKS:
# CLERK_JWT_KEY="-----BEGIN PUBLIC KEY-----\nREPLACE_WITH_PUBLIC_KEY\n-----END PUBLIC KEY-----"
```

Use the exact issuer from the frontend's Clerk instance and the exact origins permitted to
obtain its session tokens. Issuer must be an HTTPS origin; authorized parties cannot contain
paths, credentials, wildcards, query or fragment. HTTP localhost origins are accepted only in
explicit development/test environments. The issuer's fixed JWKS URL is
`<CLERK_ISSUER>/.well-known/jwks.json`; JWT header URLs never choose a destination.

Only a public verification key or public JWKS is needed; no Clerk administration secret or
Supabase Auth configuration is introduced. A configured PEM must be an RSA public key of at
least 2048 bits. Escaped newline sequences are accepted for environment-file convenience.
With a static PEM, key rotation requires updating/restarting the service; JWKS mode refreshes
the bounded key cache automatically.

## Request verification

Send `Authorization: Bearer <Clerk session JWT>`. Cookies, query parameters and `X-Player-ID`
are not Clerk credentials. The verifier enforces RS256, signature, exact issuer, exp/nbf/iat,
session/user ID structure, the explicit authorized-party list, supported session-token version,
and an optional configured audience. Pending/unknown session status is rejected. Clock-skew
allowance is five seconds. A token containing an audience requires the matching audience setting.

Missing/invalid/expired sessions return generic 401 plus `WWW-Authenticate: Bearer`. Public
key-service failures return sanitized 503 rather than treating a forged token as a player. No
provider response, token, raw claim data, private path or credential is copied to the error.

JWKS is cached for five minutes per API process, with single-flight retrieval and at most one
refresh attempt per ten seconds for unknown keys/failures. Redirects, duplicate/weak keys,
malformed responses, more than 20 keys and responses larger than 1 MiB are rejected. Connect/read
timeouts are 3/5 seconds, with a ten-second streamed-response budget. A matching cached key may
serve until its original expiry; failure does not extend the cache lifetime. Only the configured
issuer URL is fetched. Token size is bounded at 16 KiB.

This validates short-lived signed session tokens; there is no per-request session-admin lookup
or immediate revocation service. Configure the provider's token lifetime and assess live session
revocation behavior during authorized staging acceptance. Token verification is not guardian
authority, family membership, factual-content approval or role authorization. Unsafe/public
metadata and display labels do not grant data access.

## Mapping Clerk subjects to existing UUID-based storage

Clerk subjects such as `user_...` are mapped deterministically to UUID v5 using the URL namespace
and the name `questnest:clerk:<normalized issuer>:<subject>`. The verified issuer/subject pair,
not a caller-selected UUID, selects records and room membership. Existing PostgreSQL transactions
keep their RLS role/UUID claims after verification; no provider token is passed to the database.

`GET /api/me/player` returns `{player_id, provider:"clerk"}`. Use that UUID when comparing
`room.host_id` or member IDs; the frontend's raw Clerk `userId` is not a database player UUID.
An `X-Player-ID` header is ignored in Clerk mode, even if it contains a valid different UUID.

There is no automatic claim/merge of old public/default-player records. Anonymous data cannot
establish ownership of a new Clerk account. If old records must be retained, plan an explicit
trusted ownership migration/backup separately; do not let a public header claim someone else's
partition. Changing issuer changes the mapping namespace. Direct Supabase browser/Realtime
authorization is not configured by this FastAPI bridge; do not assume a Clerk subject is a UUID.

## Public and local boundaries

Protected application-data routes, including existing profile/learning/jobs/rooms, diagram,
duel and card-attempt routes, share the verified player dependency. `/health`, `/ready` and the
existing public registration request/receipt/resend workflow retain their distinct contracts;
registration implementation and policy are not part of this feature batch. Existing guardian
routes using the player dependency also receive Clerk verification, without inventing family
authority. No removed `/auth/*` account endpoints are restored.

Only explicit `APP_ENV=test` or `development` with no Clerk issuer retains the old UUID selector
for isolated local regression/demo work. It is unverified and not a deployment security mode.
Production startup refuses that configuration. The deployment preflight requires Clerk and
durable storage even if run from a development environment.

Clerk-enabled OpenAPI declares a Bearer security scheme/401 responses for protected paths and
omits the misleading player-selector input there. Anonymous local schemas remain available for
legacy isolated tests. The configured provider is determined at process creation; restart after
changing issuer, key or identity settings.

## Readiness and handoff

`/ready` checks configured Clerk keys plus storage and enabled feature dependencies. With JWKS,
the first check may retrieve public keys; later checks reuse the bounded cache. It never mints a
token or proves a live user's session. Local tests use generated RSA keys and mocked JWKS only.

The frontend already obtains a Clerk session token. Its server proxy still needs the new route
prefixes, `PUT`, and `Idempotency-Key` forwarding. Preserve Bearer forwarding and use
`/api/me/player` for UUID comparisons. Those are frontend tasks, not changes in this branch.
Backend CORS already permits Authorization and Idempotency-Key for configured exact origins.

References: [Clerk manual verification](https://clerk.com/docs/guides/sessions/manual-jwt-verification),
[Clerk session claims](https://clerk.com/docs/guides/sessions/session-tokens),
[PyJWT verification API](https://pyjwt.readthedocs.io/en/stable/usage.html).


### Frontend cache boundary

Frontend caches/recents must be scoped to the signed-in identity or cleared on identity changes.
The existing browser-only card/quiz state is not an authoritative saved attempt and must not
expose a previous player's cached content on a shared device. Backend ownership checks cannot
erase already-downloaded browser data. Update that integration separately; no frontend changed here.
