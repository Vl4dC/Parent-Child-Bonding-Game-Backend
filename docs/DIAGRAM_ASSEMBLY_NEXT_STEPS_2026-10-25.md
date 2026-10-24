# Diagram game backend — gaps and next steps

Planning addendum, **25 October 2026, Europe/Berlin**. Documentation only: no backend runtime,
frontend, AI/harness, migration, deployment or feature-flag change is made by this review.
Existing documentation remains intact. Proposed routes/rules below are not implemented contracts.

## Current baseline

Reviewed backend main `8b3c132ab0de1b8068f3449888e918f821c3737b`, merged PR #7. Frontend main
remains `1805c8bd87c77e973da9ea45715289698fea38ed`; its diagram-game/proxy integration is separate.

The backend already has fourteen operations for frozen source templates, private-room discovery,
study, readiness/host start, a common countdown, per-player boards/placements, first-perfect timing,
forfeit/cancel, results/review and repeat rounds. Cached SVG geometry, identity/roster isolation,
idempotency, version checks, private storage and source/account-deletion cleanup are implemented.
Do not rebuild those endpoints or add placeholder duplicates.

Last merged-main CI evidence: **686 passed, 25 intentional skips**, plus **15 renderer checks** in
the dedicated browser job; lint and 79-file formatting passed. This is the 24 October merge result,
not a fresh full-suite run for this documentation-only task. The six browser cases skipped in the
core suite ran in the separate renderer job. No hosted setup/real-device acceptance is inferred.

## Confirmed behavior that needs a next-step decision or fix

Four small diagnostics used the existing API fixture, synthetic accounts and **MemoryRepository**.
They made no persistent application-data or source edits, paid model calls or real email requests. They confirm API
branch behavior, not new PostgreSQL concurrency or hosted acceptance results.

| Finding | Observed result | Recommended next behavior |
| --- | --- | --- |
| A completed race can be cancelled | Race was `finished`; results 200; cancellation 200; results then 409 | Make finished/expired outcomes terminal for ordinary cancellation. Preserve explicit source/account-deletion rights separately. This changes existing cancellation semantics and needs a stated rule. |
| Invalid source blocks recovery too | After the source lesson became draft, race snapshot and cancellation both returned 409 | Separate safe match management/summary access from playable-source checks. Invalidate/cancel an active stale-source race explicitly; do not leave it inaccessible but apparently active. |
| Readiness misses a broken renderer | With the feature enabled and a nonexistent Node executable, `/ready` returned 200 while template creation returned 503 | Add feature-aware preflight/readiness. A configured-on but unusable diagram feature must not be reported ready. |
| A late host cannot manage an abandoned running race | A late joiner became the actual room host after original players left; room access 200, race cancellation 404 | Give the current verified host cancel-only management authority without granting spectator access to boards, solutions or participant data. |

Source evidence: `DiagramGame.load`, `status`, `cancel`, `start`, `results`, and
`PostgresRepository.ready`. Source validation currently runs through `load` before both gameplay
and cancellation. `load` also requires a captured player before host cancellation is considered.
The general readiness query checks the original schema/functions, not migration 008 or Chromium.

## Phase 1 — lifecycle correctness (P0, recommended first implementation batch)

### 1.1 Define terminal and invalidation rules

Document a transition table for waiting → countdown → running → finished/expired/cancelled, with
reason codes. Recommended rules:

- Ordinary cancel applies only to waiting/countdown/running. Exact prior cancellation receipts
  remain replayable; a new cancel after finished/expired returns 409 and cannot hide the result.
- Source deletion retains the existing privacy-first cascade. Never reconstruct deleted source
  data to keep a scoreboard alive.
- Source content changes or withdrawn approval before/during play explicitly terminate that active
  race with `source_changed`/`source_unapproved`; no silent redraw or continued scoring against a
  different diagram. Preserve the frozen artifact as immutable, subject to existing deletion rights.
- Decide whether an already-finished factual score summary remains readable after a source edit;
  recommendation: keep authorized summary access, but deny withdrawn/unapproved diagram review.
  Do not widen access after room expiry or source deletion without a separate retention/access rule.
- Management authorization must work without a currently playable source. A legitimate host must
  be able to stop a broken race while boards/solutions remain protected.

Acceptance: finished/expired cancel attempts do not hide results; source edits during countdown,
play and after finish have explicit outcomes; source deletion still erases derived data; exact
retries and cancel-vs-finish races produce one coherent terminal outcome in PostgreSQL.

### 1.2 Recover from host departure and abandoned participation

Keep ordinary gameplay restricted to captured, current room members. Separate this from room-host
management: the current host may cancel an orphaned match even if they joined too late to play.
Do not return that host's unauthorized diagram/solution/other-player payload as part of cancellation.

Freeze a departure policy before implementing it. Recommended distinction: a network disconnect
keeps the existing clock running and permits resume; an explicit room leave forfeits/withdraws
that attempt. That recommendation changes today's documented rejoin behavior and needs approval.
At minimum, no abandoned race should block the room until its 15-minute deadline solely because
its original host/roster disappeared. Keep “room closed to new joins” distinct from cancelling a race.

Acceptance: host transfer to a captured player and to a late joiner; all original participants leave;
reconnect without leave; explicit leave/rejoin; cancel after source invalidation; deletion concurrent
with every recovery path. No erased player row may be recreated.

The host-recovery interface also needs room-addressed management or minimal host-only race
ID/version discovery. Otherwise the late host cannot identify the race they are authorized to
cancel. This must not widen participant board/review snapshots or reveal another player's data.

## Phase 2 — runtime readiness and operational release gate (P0)

Add a **read-only preflight** that checks migration 008 tables/function/indexes, Node/helper package,
pinned Mermaid/Playwright versions, installed browser and production sandbox operation. Use a fixed
synthetic flowchart without models or user material. Run it once at startup/operator request or
cache the result; do not launch Chromium on each health poll.

Extend existing readiness additively, preserving `ready` and `durable`: when the feature is enabled,
its dependency failure must be visible and affect readiness. When disabled, Node/Chromium absence
must not prevent the rest of the API from serving. Return sanitized status/reason codes, not local
paths, credentials or raw renderer output. A separate capabilities/preflight route is optional;
another full set of game endpoints is unnecessary.

Acceptance: missing migration/function, Node/browser missing, renderer timeout/crash, malformed
output, unavailable sandbox and healthy setup; failed checks do not mutate schema or private data.
Validate actual Linux staging with sandbox **on**: current renderer CI opts out for trusted fixtures
and therefore does not establish a production sandbox configuration.

## Phase 3 — fresh race readiness and consumer error contract (P1)

Current start checks the room's reusable `ready` flags. After one round, a new diagram can inherit
those flags without a fresh study/ready acknowledgement. Recommended addition: race-scoped ready
acknowledgement bound to race ID and immutable template/snapshot version. A new race starts unready;
start requires the captured roster's current acknowledgement. Keep existing room readiness APIs
compatible. A proposed `POST /api/diagram-assembly/races/{id}/ready` is one option, not a live route.

Add stable machine-readable errors for stale placements, phase closed, unsupported visual, source
withdrawn, host/roster problems, renderer busy/unavailable and quota exhaustion. Preserve existing
HTTP status/detail compatibility; add reason/retry fields instead of exposing source or secrets.
Specify which exact receipts return historical state and when callers must refetch current versions.

Acceptance: a rematch/new template cannot start on old acknowledgement; one player cannot ready
another; source/roster changes invalidate the relevant acknowledgement; countdown and duplicate
start remain idempotent. Test 409/429/backoff behavior without any frontend code changes.

## Phase 4 — maintenance, capacity and diagnostics (P1)

### 4.1 Expiry and retention

There is no cleanup/pruning worker or reviewed retention schedule. Expiry/room disappearance blocks
access but does not delete private race/template/request rows. Define waiting, abandoned, cancelled
and finished-match retention first; template lifetime may follow its source. Do not invent a new
schoolwork retention period.

Provide a server-only maintenance command with dry-run, explicit cutoff, bounded batches and
active-match safeguards. Persist terminal expiry before pruning; preserve live/current references,
idempotency requirements and account/source erasure. Never expose a broad delete-admin API to
ordinary players. Hosted maintenance execution is a separate operator action, not this plan.

### 4.2 Capacity and renderer recovery

Existing limits are per-process renderer concurrency, per-account API rate, one active race per
room and 1,000 participant action receipts. Template/race creation receipts, cached artifacts and
cross-room totals have no complete storage-budget policy. Define bounded per-owner creation/cache
budgets and whether multiple API workers need a shared render reservation. Avoid holding a database
connection while queueing/rendering. A new async render-job service is optional only if measured
request timeouts justify it; do not modify the AI generation-job engine.

### 4.3 Operational diagnostics

Add aggregate render duration/cache-hit/busy/error, DB wait/timeout, race expiry/completion and
cleanup counters using request IDs. Do not log source, labels, SVG, placements, names, tokens or
idempotency keys. Retain sanitized error categories rather than full provider/browser exceptions.

Acceptance: dry-run deletes nothing; active races survive maintenance; expired data prunes in
batches; missing/crashed browser processes recover without leaks; limits work across processes;
normal 8-player polling/drop/finish traffic stays within chosen request/pool budgets.

## Phase 5 — backend staging acceptance (P1, before enabling public play)

After the correctness/preflight work, use approved synthetic saved diagrams and authorized test
accounts against real Supabase identity/PostgreSQL. Exercise 2, 4 and 8 independent clients and
multiple rooms. Use API clients, not frontend modifications or new AI generation.

Verify shared start, slow delivery, out-of-order/duplicate saves, last-second finishes, ties,
disconnect/resume, host departure, process restart, source edits/deletion and account-data deletion
while opponents act. Verify renderer timeout/kill cleanup, maximum labels/long labels/nested groups,
multiline/Unicode/styles and sanitizer rejection with sandbox on. Measure rather than promise a
latency SLA; recorded times include network/server effects. The historic-image limitation remains.

Release gate: regressions and both CI jobs green; migration/runtime/sandbox preflight green in the
target environment; no result/state corruption or data resurrection; reviewed cleanup/budgets;
documented rollback/disable procedure. Do not run hosted migrations, deploy or enable the feature
merely because this plan lists them.

## Later enhancements, not blockers for the first round (P2)

- Optional room-scoped realtime invalidations carrying only IDs/version, while preserving polling,
  reconnection and fresh API authorization. No secret payloads in notifications.
- Private own-attempt history and per-slot mistake/improvement summaries for repeated practice.
  Agree retention and access after room expiry first; do not grant permanent access to another
  person's shared source. No speed-derived mastery or reward claims.
- More deterministic diagram formats only after fixtures and label/geometry policies exist.
  Keep source unchanged and do not add AI fallback/redraws.

The current exact guarantee is already defined: frozen study copy and game clone share geometry.
Pixel-identical recovery of an older browser-only drawing requires a saved SVG from its producer;
that dependency cannot be solved by claiming text-only source contains the old coordinates.
Changing the AI/harness producer or frontend is outside this plan.

## Recommended next code PR

First implement **Phase 1 plus feature-aware preflight from Phase 2**, with explicit regression
tests for the four confirmed diagnostics. Keep it backend-only and preserve current documentation.
Then race-scoped readiness/errors, maintenance/budgets/metrics, and staging acceptance. Treat
realtime/history/format expansion as later independent changes.

No implementation starts in this documentation task. Policy decisions still needed: terminal
cancel semantics, source-withdrawal treatment of finished summaries/review, explicit-leave policy,
fresh per-race acknowledgement, and retention/capacity values.

## Evidence links

- [Merged implementation and tests](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/pull/7)
- [Reviewed lifecycle source](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/blob/8b3c132ab0de1b8068f3449888e918f821c3737b/app/diagram_game.py)
- [Reviewed renderer](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/blob/8b3c132ab0de1b8068f3449888e918f821c3737b/app/diagram_render.py)
- [Reviewed readiness](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/blob/8b3c132ab0de1b8068f3449888e918f821c3737b/app/repository.py)
- [Merged-main CI baseline](https://github.com/Vl4dC/Parent-Child-Bonding-Game-Backend/actions/runs/40136116738)
- [Backend Drive document](https://docs.google.com/document/d/1i9_xaAnHKp2Fs3A6TcnZ2hi-HmDhiEKFqsexFEDJROA/edit)
