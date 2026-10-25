# Duel/card-attempt release preparation

Added 25 October 2026. This is a handoff for an unmerged backend implementation. The user
explicitly requested **no merge to main and no deployment**. No hosted SQL, feature flag,
Clerk tenant, email service or production process was changed by this delivery.

## Required operator configuration

- A reviewed backend hosting target, HTTPS URL and frontend origin(s).
- Server-only PostgreSQL connection and appropriately bounded API/worker pools, using the
  existing Supabase PostgreSQL/private-schema setup. Keep `api_private` unexposed.
- Clerk issuer and authorized frontend origins; optional audience and RSA public verification
  key. See [the current identity contract](CLERK_BACKEND.md). Live tenant settings/tokens have
  not been verified by synthetic tests.
- Backups, observed logs/metrics, trusted proxy/request limits and a reviewed retention/capacity
  policy. Do not copy local test/default-player data into real accounts without an ownership plan.

## Migration and enablement order

1. Review the final PR/CI and approve a merge separately. Do not deploy this branch by assuming
   that a draft PR is main. Existing source-data backups and prior schema version should be known.
2. Apply existing migrations through 009, then the new **010 card attempts** and **011 duels**
   through the reviewed migration workflow in the selected staging environment. Startup and
   readiness never apply migrations. These additive migrations do not rewrite legacy records.
3. Keep `DUELS_ENABLED=false` and `FLASHCARD_ATTEMPTS_ENABLED=false` while configuring Clerk and
   storage. Generation remains off unless separately authorized; these new actions need no model.
4. Run the read-only preflight from repository root:

```sh
uv sync --locked
uv run python -m app.release_check
# Include the existing diagram renderer when that feature will be used:
uv run python -m app.release_check --renderer
```

The command forces only a copied probe configuration for the two new feature schemas, even
while their actual flags are false. It prints sanitized JSON and exits 0/1. It reads migration
objects and public Clerk keys, never writes learner records or applies SQL, and never changes
flags. The renderer option uses a fixed synthetic diagram. A green local synthetic-key run is
not evidence that the intended Clerk tenant or hosting environment is configured correctly.

5. In authorized staging, enable `DUELS_ENABLED=true` and `FLASHCARD_ATTEMPTS_ENABLED=true`.
   Check `/ready` and perform the acceptance cases below with distinct real Clerk users.
   New routes also check their migration readiness before handling requests, so missing/disabled
   required triggers cannot silently create unlinked attempt records.
6. Enable the reviewed release in the intended environment only after staging acceptance and
   separate authorization. None of these hosted steps has been executed here.

Migration 010 installs source/lesson deletion links and triggers for owned `card_attempts` and
their creation receipts. Migration 011 installs private match/player/request tables, source
invalidation and participant erasure. New tables have RLS and no browser-role table access;
normal card-attempt records keep existing owner RLS. Relevant source/index/trigger checks are
part of `/ready`. The upgrade test covers adding 010/011 to the pre-feature schema while retaining
legacy records and existing object identities, and rolls its test DDL back afterward.

## Staging acceptance checklist

- A real Clerk session maps to the expected player UUID; another valid user's token remains
  isolated. Missing, expired, wrong-issuer/audience/party tokens fail. Forged X-Player-ID cannot
  select another account. Check key rotation, an unavailable JWKS service and provider revocation.
- Create a reviewed-source local duel: different sheets, no early score/key reveal, first-sheet
  finality, ties, forfeits, correct reveal even without submissions, history and rematch.
- Two real devices join a room with separate Clerk sessions. Fresh per-match readiness, shared
  start, simultaneous submissions, duplicate/conflicting retries, leave/rejoin and host transfer
  behave as documented. A late host can cancel but cannot read participants' sheets.
- Source withdrawal/change cancels active matches and cannot reopen them. Terminal summaries,
  source-invalid review, source/material deletion and participant data erasure behave correctly.
- Concurrent diagram/duel creation accepts one active game per room. Drain/cancel existing games
  before changing feature flags or deployments; disabled games still reserve their active rooms.
- Restart cards from saved content, including a difficult subset and a mid-attempt restart.
  Earlier results/deck progress remain intact; identical retries do not advance twice. Reload and
  a second API process preserve state. Source/data deletion erases derived attempt content.
- Browser proxy forwards the new prefixes, Bearer and Idempotency-Key, and supports PUT. Verify
  exact CORS origins for any direct cross-origin calls. No frontend wiring is included here.
- Observe quotas, pool/lock waits, typical payload sizes, retained history and retention behavior.
  No coins/XP/reward grants or automatic history pruning are included in this batch.

## Disable and rollback

Prefer draining/cancelling active matches before disabling flags. Keep Clerk verification
configured; do not roll back to an anonymous production API. Leave additive migrations and
terminal outcomes in place. A code rollback must preserve authentication and understand any
new retained records; verify compatibility before selecting the prior artifact. If a feature
is unavailable, repair its dependency/configuration rather than relaxing identity checks or
granting broad SQL access. Take backups before any separately reviewed data migration/cleanup.

New routes remain feature-gated. Existing APIs are regression-tested, but older production
instructions that said authentication was unnecessary are historical; Clerk is now required.
The supplementary OpenAPI and API guide describe the new bodies and version scopes. Test evidence
and any remaining live-environment limits are recorded in the implementation's final handoff.


## Completed local verification

Final full suite against disposable local PostgreSQL 16: **1,037 passed, 36 skipped** in 60.40
seconds. The skips are 28 memory-adapter variants of PostgreSQL-only checks, one paid-model
opt-in, and seven browser opt-ins. Their PostgreSQL variants ran. Separate actual browser suite:
**16 passed** using local Chrome with sandbox configured on. Ruff lint and formatting passed
for 106 Python files. No live paid model or email was used.

Coverage includes real generated RSA session signatures, production startup, owner/header forgery,
wrong/missing claims, key rotation/outage/redirect/size/weak-key limits, both persistence adapters,
independent sheets, reveal gating, canonical solutions after forfeits, readiness, concurrency,
expiry under row-lock waits, source updates/deletion, participant and late-host receipt erasure,
card attempt preservation/restart/subsets, durable reload, SQL privileges and additive upgrade.

The read-only release gate also passed locally with a generated synthetic public key and the
disposable database, including a synthetic renderer probe. Original feature flags stayed false
and application-record counts were unchanged. This does not validate an actual Clerk tenant,
Linux production sandbox, hosted database or public deployment; those staging steps remain above.


### Integration refresh after upstream PR 11

Main advanced to `31f2d48e10d81295a8f0e2122015f186c816c167` while this feature's checks ran.
That upstream change was merged into the feature branch only, preserving its existing image-chat
and lesson-topic updates. The combined full local suite passed **1,051 tests with 36 intentional
skips** in 59.61 seconds. Ruff lint/format remained clean (106 Python files). The earlier
1,037-test result above is historical evidence for the prior base. Relative to current main,
this feature still has no AI/harness/static-source changes and preserves every existing Markdown
file as an exact prefix. PR 12 remains unmerged; no deployment was performed.
