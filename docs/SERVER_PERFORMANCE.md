# Server performance and resource limits

This optimization preserves the API routes and payload contracts. It changes storage access and
worker execution; no frontend, hosted database, or running production server is changed.

## Changes

- Bounded Psycopg connection pools reuse connections. Data transactions and short control operations
  (quotas, worker claims, readiness) use separate pools so a request holding a data connection cannot
  deadlock waiting for a quota connection from the same exhausted pool.
- Role and verified JWT claims remain transaction-local. Commit/rollback resets them before reuse.
  API-owned pools close at shutdown; workers close their pools on exit.
- GET transactions are read-only. Record and room snapshots use MVCC reads instead of taking row
  locks, so readers can see committed state while another transaction writes.
- Progress totals are calculated by one SQL aggregate. Resource lists project only metadata, and
  enqueue limits count pending jobs in SQL without downloading historical inputs/card payloads.
- Migration 005 adds partial indexes for worker status/scheduling and owner pending-job counts.
- Workers capture the minimal owned source graph, release the database transaction, generate into
  a detached buffer, then commit results in a short transaction. That transaction rechecks the worker
  lease and every source row version. Changed/deleted sources or a replaced lease discard stale output.
  This also prevents account-data deletion during generation from resurrecting content afterwards.

Legacy synchronous generation routes still keep a request transaction until they finish. Use durable
jobs for interactive applications; these optimizations do not shorten the model provider's generation time.

Pool checkout/return follows the [Psycopg connection-pool lifecycle](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).

## Pool settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_POOL_SIZE` | 8 | Maximum data connections per API/worker process |
| `DATABASE_CONTROL_POOL_SIZE` | 2 | Maximum short-control connections per process |
| `DATABASE_POOL_TIMEOUT` | 5 | Maximum seconds waiting for a pooled connection |
| `DATABASE_LOCK_TIMEOUT_MS` | 3000 | Transaction-local database lock timeout |
| `DATABASE_STATEMENT_TIMEOUT_MS` | 30000 | Transaction-local SQL statement timeout |

Pools start empty and open connections on demand; each queue is capped at 64 waiters. Exhaustion or
SQL timeout produces the existing sanitized storage-unavailable error. Pool settings multiply by
process count: the default ceiling is ten connections per process, not ten per deployment. Budget
API and worker processes together against the database connection limit. Start with one API process
and one worker, then measure before increasing process counts. No identity verification cache is added.

## Measured local database paths

Representative development-machine results, in milliseconds:

| Path | Before median | After median |
| --- | ---: | ---: |
| Empty profile lookup | 2.29 | 0.48 |
| Progress totals | 13.65 | 2.50 |
| Resource page (50 decks) | 3.78 | 1.07 |
| Pending-job count | 6.50 | 0.41 |

The synthetic workload has 200 decks with 20 cards each (400-character backs), 500 quiz records with
ten answers each, 100 completion records, and 500 jobs (three pending). Each path has three warmups
and 20 samples. The optimized run reused one data connection across all 92 operations. The baseline
opened a connection for every transaction. The after run includes migration 005.

These measurements exclude HTTP, Supabase identity verification, TLS/network latency to a hosted
project, model calls, and production contention. They are not a production latency or throughput SLA.

Reproduce against a dedicated local database (the script refuses nonlocal hosts and names without
an `_bench` suffix):

```sh
createdb questnest_server_bench
BENCH_DATABASE_URL=postgresql://localhost/questnest_server_bench \
  uv run python scripts/benchmark_server.py --seed
```

`--seed` installs the test-compatible schema into an empty benchmark database and writes synthetic
records for a reserved demo owner. On an existing benchmark schema, apply later migrations explicitly.
Omit `--seed` to rerun the timings without changing the dataset. Never use a real application database.

## Verification

The suite covers connection reuse, role/claims reset after rollback, read-only reset, reads during a
writer row lock, projected pagination/ownership, progress parity, pool backpressure/recovery, and
single-connection responsiveness during a slow model call. Worker tests cover source changes,
concurrent deletion, and replaced leases while generation is in progress.

Hosted Supabase Realtime and live model acceptance remain separate staging checks. Pools and SQL
optimizations do not select a retention policy for schoolwork or automatically prune expired lobbies
and operational counters. Operators still need a reviewed maintenance schedule for those tables.


## Diagram assembly renderer — additive limits, 24 October 2026

The optional renderer is isolated/time-bounded and concurrency-limited; it runs outside DB
transactions and caches frozen output by source fingerprint. Game reads project away unrelated
SVG/receipt/tray payloads. Mutations use short room/race locks and the existing bounded control
pool. Node/Chromium, sandbox and deployment requirements are in [DIAGRAM_ASSEMBLY.md](DIAGRAM_ASSEMBLY.md).
No provider call, runtime network fetch, frontend change or production-load claim is added.
