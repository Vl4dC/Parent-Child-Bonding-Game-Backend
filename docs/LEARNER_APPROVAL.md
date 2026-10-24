# Learner approval request API — additive implementation

Added 24 October 2026. Backend only, based on main `a647491` (which already includes PR #4's
learning safety changes). No frontend file is changed. This document supplements earlier
contracts; it does not replace their text. These routes record a pending request and a guardian
decision. **They never create or activate a learner, assign a family link, or grant learning access.**

## Availability and configuration

The feature is disabled by default. All five routes return 503 while disabled. To enable it in a
reviewed development/staging environment, apply migration `20261024000600_learner_approval.sql`,
set `LEARNER_APPROVAL_ENABLED=true`, and provide `LEARNER_APPROVAL_SECRET` as a private random
secret of at least 32 bytes. Missing/short secrets fail configuration validation when enabled.
Use PostgreSQL for durable operation; the memory adapter is limited to explicit test/demo use.

The secret derives status receipts, fingerprints and mail proofs through purpose-separated HMAC.
Only hashes/fingerprints are stored for credentials; plaintext receipts and proofs are not stored.
All API/worker processes must share the same secret. Rotating it invalidates existing receipts,
proofs and request-idempotency lookups; key-version migration is not implemented in this batch.

No provider settings or hosted migrations have been applied by this implementation. SMTP is
configured only for the explicit worker, separately from Supabase confirmation email delivery.

## Routes

| Method and path | Authorization | Result |
| --- | --- | --- |
| POST `/auth/registration/learners` | Public intake; creation idempotency key required | 202 with request ID, private status receipt and queued-request message |
| GET `/auth/registration/requests/{id}` | `X-Registration-Receipt` | Minimal state/version/expiry; no guardian identity, proof or learner Auth ID |
| POST `/auth/registration/requests/{id}/resend` | Receipt plus idempotency key | 202; queues a replacement notification and supersedes earlier proofs |
| GET `/api/guardian/registration-requests` | Verified Supabase Bearer session and confirmed email | Paginated requests addressed to that verified email |
| POST `/api/guardian/registration-requests/{id}/decision` | Same verified identity, request-specific authority grant, current proof and idempotency key | Approved/declined record, always `activated: false` |

All responses use existing no-store/no-referrer behavior. Errors omit submitted secrets. New
headers are added to the existing exact-origin CORS policy without enabling cookies or wildcard
origins. `Retry-After` and `X-Request-ID` are exposed to clients. Status receipts are not Auth
sessions and cannot authorize `/api/me/profile` or any other owned-data route.

### Create a request

Canonical snake_case fields and the current signup branch's camelCase field aliases are accepted:

```json
{
  "learnerDisplayName": "Sam",
  "parentEmail": "guardian@example.com",
  "learnerBirthYear": 2014
}
```

Send a fresh, cryptographically random `Idempotency-Key` with at least 128 bits of entropy, encoded
as 32–128 characters (for example a generated UUID or random hex). Treat this key as private:
same-client retries with the same key/details return the original private receipt. Do not log it.
Creation retries are scoped to the resolved client IP, not an untrusted forwarded header. A retry
from a different client IP is a new operation; the returned receipt is the durable polling credential.
Changed details with the original key return 409. Unknown fields, including passwords, are rejected.

Response includes `request_id`, `receipt`, `status: "pending"`, `version: 1`, `expires_at`,
`activated: false`, and a generic message. A 202 means queued/accepted, not email delivery.
Store the receipt privately and send it only in `X-Registration-Receipt`, never in a URL.

Display names are trimmed, 1–60 characters, nonempty and single-line. Guardian email is validated
and normalized for matching. Birth year must be an integer within a plausible human lifetime;
it is **intake metadata, not exact age verification or an under-16/under-18 eligibility decision**.
The cutoff, exact birth-date contract and guardian-authority policy remain unresolved. This route
cannot be used to claim legal consent or proof of guardianship.

There is no `/api/registration/learner` alias in this batch. The existing frontend branch's
same-origin request still needs future routing/base-URL and idempotency-header integration.
No frontend wiring is included.

### Status and resend

Requests/receipts have a seven-day lifetime. A missing or wrong receipt cannot inspect a request;
wrong receipts return 404, expired valid receipts return 410. Responses contain only request ID,
status, version, expiry and `activated: false`. They do not report a guardian's account or reveal
mail transport errors. Guardian listing derives `expired` for an undecided request past expiry.

Resend requires an 8–128 character `Idempotency-Key` plus the original receipt. Wait at least 60
seconds. An accepted resend increments request version and proof generation, invalidates every
older proof and queues one replacement email. An identical resend retry does not enqueue again.
Only pending/unexpired requests may resend. Declined/approved requests return 409. Retries of an
already accepted resend remain harmless, but do not recreate expired receipts.

Fixed abuse limits in this initial batch: 120 public operations/client/minute; ten new requests
per client/hour; 1,000 new requests globally/hour; three notifications per recipient/hour and ten
per day; 60 guardian operations/account/minute. Recipient/client bucket keys are HMACs; quota
transactions are short. Invalid receipts/proofs consume the separate request-rate allowance too.
Trusted reverse-proxy setup must preserve a trustworthy `request.client.host`; forwarded headers
are not interpreted directly by this module. Limits are not proof against distributed abuse.

### Guardian review and decision

List with `after` and `limit` (1–50, default 20). Only the verified session's confirmed email can
match a request. Results contain learner display name/year and `can_decide`, but never proofs.
Knowing a request ID, selecting a parent label, or controlling the destination mailbox alone does
not authorize approval. `user_metadata.account_type` is deliberately ignored for authorization.

The decision body is:

```json
{
  "decision": "approve",
  "expected_version": 1,
  "proof": "REPLACE_WITH_CURRENT_EMAIL_PROOF"
}
```

`decline` is also accepted. The worker's actual proof is a 64-character private value. Submit it in
the POST body, not the URL. Require a Bearer session, an 8–128 character `Idempotency-Key`, and
a trusted, unexpired authority grant bound to this request and guardian UUID. Version changes,
terminal/expired state and conflicting replays return 409; wrong/superseded proof or absent authority
returns 403; another recipient's request returns 404. A successful exact retry returns the saved
response while the guardian remains authorized. Concurrent approve/decline produces one winner.

Approval records the decision and verified actor, increments version and returns `activated: false`.
No password, Auth account, session, family membership or permission is produced. There is no
activation endpoint yet. A GET or email link never approves a request.

## Authority grants are a trusted operational step

The actual process for establishing guardian authority is not settled. Until it is, production
self-service approval is intentionally unavailable. No HTTP endpoint lets a caller grant authority.
After an authorized operator has independently reviewed a specific request, the server-only bridge
can record an expiring grant (default one hour, maximum remaining request lifetime):

```sh
uv run python -m app.approval_admin \
  --request-id REQUEST_UUID --guardian-id VERIFIED_GUARDIAN_UUID \
  --evidence-reference OPAQUE_REVIEW_CASE_ID --lifetime-seconds 3600
```

Use an opaque review reference, not personal evidence or credentials in shell arguments. Its HMAC,
not the raw reference, is stored. Do not run this merely because someone selected Parent or can
receive email. The bridge is an integration point for external review, not an identity-check service.
It does not send mail, and has not been executed against a hosted database in this task.

## Explicit email worker

```sh
uv run python -m app.approval_mail --once
```

Requires enabled approval, PostgreSQL and `APPROVAL_SMTP_HOST`, `APPROVAL_SMTP_USERNAME`,
`APPROVAL_SMTP_PASSWORD`, `APPROVAL_SMTP_SENDER`; `APPROVAL_SMTP_PORT` defaults to 465.
The supplied adapter uses authenticated TLS/SMTP_SSL with a ten-second timeout. No credentials
are bundled and no live SMTP service was configured or called by tests. Schedule the command
explicitly; one invocation handles at most one email after scanning at most 100 due rows.

Outbox rows contain request references and generation/lease state, not plaintext proofs. The worker
derives the current request-bound proof at send time. Delivery uses a 60-second lease and at most
three attempts with 60/120-second backoff. Expired leases may be reclaimed; stale workers cannot
acknowledge another lease. Superseded/expired/decided requests are cancelled before sending.
SMTP I/O holds no database connection or approval lock. A resend/decision racing an in-flight send
can still deliver an obsolete email, but its proof cannot change the current request.

SMTP acceptance is not proof of inbox receipt. A crash after SMTP acceptance but before acknowledgement
can cause duplicate delivery; the stable Message-ID is not a guarantee of exactly-once mail. Errors
are not copied to public responses or logs. Delivered/failed rows are excluded by the indexed due
query. Approval control transactions share a short global advisory lock to serialize cross-account
decisions; measure before increasing throughput. This is intentionally a bounded control plane.

## Storage and remaining boundary

Migration 006 adds a new private table and guardian/due-mail indexes. RLS is enabled; public, anon
and authenticated roles have no table privileges or client policies. Only the backend's trusted
control connection operates on it. Owner-scoped resource lists cannot enumerate these records.
Tests use real PostgreSQL and also exercise a separate memory adapter for development behavior.

Expiry prevents pending decisions and receipt reuse; it does not automatically erase stored data.
Retention/pruning of expired request details, hashed audit references, limits and outbox rows still
needs an explicitly reviewed maintenance policy before enabling broad production intake. Existing
`DELETE /api/me/data` does not claim deletion of pre-account registration records. This batch does
not silently choose a retention schedule for schoolwork or change older deletion behavior.

Existing `/auth/register` and direct Supabase signup are unchanged and are not subject to this
approval workflow. Therefore this feature **does not enforce mandatory approval across the app**.
Before public launch, the agreed registration/access policy must cover those paths and existing
users. Activation, no-email learner identity/recovery, username login and family access remain
separate approved work. No hosted migration, deployment, frontend integration or new paid call is
part of this implementation.

## Verification

The added tests cover verified-email isolation, request-specific authority, forged-role rejection,
receipt isolation/expiry, unknown-field/secret-safe validation, idempotent and conflicting retries,
resend cooldown/proof rotation, guardian pagination, concurrent create/approve/decline, PostgreSQL
permissions and restart persistence, SMTP retry/lease recovery, stale mail cancellation and database
availability during sender execution. Supabase identity replies and email delivery are faked.

See `learner-approval.openapi.json` for the additive five-route schema. The existing OpenAPI snapshot
is preserved; runtime `/openapi.json` includes both old and added routes.

### Completed local checks

On 24 October 2026, the full suite passed with **602 passed, 17 skipped** against a disposable
local PostgreSQL 16 database, Python 3.13. The approval suite contributed **34 passed, 1 skipped**.
Sixteen full-suite skips are memory variants of PostgreSQL-only checks; their PostgreSQL cases ran.
The remaining skip is the opt-in live paid safety-model check. Lint passed and all 70 Python files
passed format checking. OpenAPI references resolve. Existing documentation bytes were verified
as unchanged prefixes. No real emails, model calls, hosted migrations or deployments were used.

## Policy decision — under 16 (24 October 2026)

The user confirmed that guardian approval applies to learners **under 16**. A learner who has
reached their 16th birthday is outside this age-based approval requirement. Earlier statements
that the cutoff was undecided are historical; this addition records the confirmed decision
without rewriting them. It does not establish unrelated adult-learner or parent-eligibility rules.

The current birth-year-only intake cannot resolve an exact birthday boundary. Future eligibility
enforcement must use a validated full birth date and an agreed date/timezone convention, and test
the day before, the day of and the day after the 16th birthday, including leap-day dates. Birth
date remains self-reported metadata, not independent identity or guardian-authority verification.

This is a documentation-only policy addition. No frontend, endpoint behavior, Auth provisioning
or activation setting has changed. The feature remains off by default, activation remains absent,
and existing registration paths are not yet subject to mandatory approval enforcement. The prior
602-passed/17-skipped results refer to the unchanged runtime code; no new runtime test result is
claimed for this documentation-only addition.
