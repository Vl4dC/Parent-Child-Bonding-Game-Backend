# Diagram lifecycle and readiness — first backend batch

Added 25 October 2026. This supplements the merged diagram guide and the
[next-step plan (PR #8)](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/pull/8).
Earlier documentation remains historical and unchanged. No frontend, proxy, AI, harness,
generation policy or model dependencies were changed. No hosted migration or deployment was run.

## Lifecycle rules

| Current phase | Host cancellation | Source withdrawal/change |
| --- | --- | --- |
| Waiting, countdown, running | Cancel with version and idempotency checks | Atomically cancel on lesson update |
| Finished | 409; preserve results | Preserve summary; block diagram review while source invalid |
| Expired | 409; preserve results | Preserve summary; block diagram review while source invalid |
| Cancelled | Stable cancelled response with current version | Never reopen on reapproval |

Race snapshots add `reason` and `source: {available, reason}`. Results add the same `source`
object. Reasons for source availability are `source_unapproved`, `source_changed` and
`source_unavailable`; `null` means available. Cancellation records `host_cancelled` or the
source reason. Materializing an elapsed deadline on a source update records `deadline_elapsed`.
All-source deletion still cascades the derived template/races/results; readable summaries do
not override deletion, room expiry or current-membership authorization.

The approved lesson's selected visual and revision must match its frozen fingerprint. Editing
the selected visual, removing it, changing the lesson revision or withdrawing approval terminates
active races. Unrelated lesson-field updates do not invalidate an unchanged approved source.
Reapproving/restoring source can make a completed diagram review available again; it cannot
restart a cancelled race. Create a new race after review. No redraw or model call occurs.

Summary access and management no longer depend on source playability. Study, board and new
placement/finish/forfeit actions still require a valid source. Exact successful idempotency
replays return their historical receipt, including after cancellation; fetch a fresh snapshot
for current state. No additional scoring is performed by a replay.

Network loss keeps the common clock running. Explicit room leave/rejoin retains the existing
policy: leaving removes current access but does not automatically forfeit the captured attempt.
A still-valid same-account rejoin can resume before the deadline. This batch does not change
that rule or add a per-race readiness acknowledgement.

## Host management endpoint

`GET /api/diagram-assembly/rooms/{room_id}/management` requires no login or token. The selected
X-Player-ID must match the current room host; this caller-selected UUID is not verified.
It returns only the latest race's management fields:

```json
{
  "room_id": "ROOM_ID",
  "race_id": "RACE_ID",
  "version": 2,
  "status": "running",
  "reason": null
}
```

A late joiner who becomes host can discover this metadata and use the existing
`POST /races/{race_id}/cancel` with `{"expected_version":2}` and an `Idempotency-Key`.
They cannot read the board, study copy, results, solution or other players' answers unless
they were captured participants. The creating owner must also still be a current room member
to cancel. Non-host room members receive 403 from management; outsiders receive 404.
Finished/expired races cannot be cancelled by either role. A version conflict requires a fresh
snapshot/management read; replay the same successful request with its original key/body.

## Migration 009

Apply `supabase/migrations/20261025000900_diagram_lifecycle.sql` after migration 008 through the
normal reviewed migration process. Startup and preflight never apply SQL. Migration 009 adds a
template-to-race index and a private source-update trigger. It locks affected races in ID order,
serializes against race writes, preserves effective finished/expired outcomes, and persists an
active cancellation in the same transaction as the lesson change. The explicit memory adapter
mirrors these lifecycle rules under its existing unit-of-work lock.

The trigger handles future updates, not a historical reconciliation sweep. Before enabling a
previously used deployment, drain active matches and cancel any already-stale waiting/running
race using its authorized owner/host management path. Inspect legacy inconsistent games before
enabling; migration 009 does not silently cancel every existing match. Existing foreign keys and
account/source deletion cleanup remain in force. No new public Supabase table access is granted.

For rollback, disable the diagram feature first, then deploy the prior application if necessary.
Retain the additive migration and cancelled state; do not remove the trigger or rewrite outcomes
while a newer application is serving matches. Re-enablement requires a passing preflight.

## Readiness and operator check

`GET /health` remains process health. `GET /ready` retains `ready` and `durable`, adds
`features.diagram_assembly`, and sends `Cache-Control: no-store`:

```json
{
  "ready": true,
  "durable": true,
  "features": {
    "diagram_assembly": {
      "enabled": true,
      "ready": true,
      "state": "ready",
      "reason": null,
      "schema_ready": true,
      "renderer_ready": true,
      "checked_at": 1792886400000,
      "versions": {
        "node": "v22.23.0",
        "mermaid": "12.2.0",
        "playwright": "1.64.1",
        "browser": "155.0.0.0"
      },
      "sandbox": true
    }
  }
}
```

Versions/timestamps above are illustrative; read deployed values. When disabled, the feature
reports `state: "disabled"`, `enabled: false`, `ready: true` and null schema/renderer/check time.
No Node, browser or diagram schema check runs for a disabled feature. Overall readiness still
depends on the existing application storage check. When enabled, unhealthy storage, missing
migrations/indexes/source trigger, missing Node/packages/browser or a failed browser probe makes
overall readiness 503. A replica-only/disabled/wrong-function source trigger is not ready.

Static dependency/schema checks run on each health call. Browser checks render a fixed two-label
synthetic flowchart, with no learner content, records, AI or external network. The probe validates
Node >=22, exact Mermaid 12.2.0 and Playwright Core 1.64.1, successful browser execution and the
configured sandbox flag. Production refuses a sandbox opt-out. Browser success is cached for
60 seconds, failure for 10 seconds, per process/configuration. Concurrent probes share one flight
and the game's existing renderer semaphore. When busy, a same-configuration success gets at most
60 extra seconds of grace, marked `probe_deferred`/`deferred_reason`; a cold or older check is
503 with `state: "checking"`. Fresh schema/dependency failure bypasses positive runtime cache.
The first cold readiness call may take up to the configured render timeout. Size deployment
readiness timeouts accordingly; normal polls do not launch a browser every time.

Operator command, from repository root with the normal backend environment:

```sh
uv run python -m app.diagram_preflight --probe
```

`--probe` checks a locally copied enabled configuration even while the actual feature remains
off. It prints sanitized JSON and exits 0 when ready, 1 when unavailable. Without `--probe`, a
disabled feature is reported without rendering. The command reads schema and runs a synthetic
browser only; it never writes records, applies migrations or changes feature flags. Configuration
validation still requires the normal backend settings. It opens/closes its own repository pools.
Error reasons omit local paths, raw subprocess/SQL output, credentials and source text.

Readiness is dependency evidence, not a guarantee of concurrency capacity, production sandbox
isolation or multi-phone acceptance. Verify the intended Linux deployment with sandbox enabled.
The renderer CI fixture opt-out remains limited to trusted synthetic CI cases.

## Validation and remaining work

The batch tests terminal protection; source edits/withdrawal at waiting/countdown/running/finished/
expired phases; non-reopening on source restoration; late host authorization/content isolation;
concurrent finish/cancel and source/cancel; privacy deletion; disabled/failed/cached preflight;
schema/trigger/index detection; single flight; busy-renderer grace; and real browser provenance.
PostgreSQL tests use a disposable local database. Synthetic game fixtures forbid model calls.

Local evidence: `TEST_DATABASE_URL=postgresql:///questnest_diagram_fixes_test LIVE_SAFETY= uv run
pytest -q -rs` passed **736 tests**, with **27 intentional skips** (19 memory-only variants of
PostgreSQL checks, one paid-model opt-in, seven browser opt-ins). The separate opted-in
`tests/test_diagram_render.py tests/test_diagram_render_live.py` run passed **16 tests**, using
local Chrome with sandbox configured on. Ruff lint and formatting checks passed (83 Python files).
These runs made no paid model calls and do not prove target Linux sandbox or hosted acceptance.

Still pending: fresh per-race ready acknowledgement; broader structured error/retry codes;
chosen retention and batched maintenance; per-owner cache/storage budgets and multi-process
render capacity; aggregate operational metrics; target Linux sandbox and 2/4/8-client staging
acceptance; future realtime/history/format work. Frontend wiring and accessible drag/drop remain
separate. Feature enablement and hosted migration 009 have not been performed by this batch.

The supplemental [OpenAPI snapshot](diagram-lifecycle-readiness.openapi.json) records all 15
diagram operations and `/ready` from this batch. Existing OpenAPI snapshots remain unchanged.

### Final lock-wait regression addition

Race writes and source invalidation now evaluate the deadline after acquiring the race lock,
so lock contention cannot turn an already elapsed race into an ordinary cancellation. The final
full local run passed **737 tests with 28 intentional skips** after adding a PostgreSQL row-lock
timing regression (the extra skip is its memory adapter variant). The earlier 736-test run above
is retained as historical evidence. Renderer behavior is unchanged from the 16-test browser run.
