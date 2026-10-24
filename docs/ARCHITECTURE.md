# Architecture

One FastAPI factory, one Python 3.13+ dependency lock, and a public API with player-scoped data partitions.
Frontend source remains in the main repository and is not modified here.

| Module | Responsibility |
| --- | --- |
| app/main.py | Factory, middleware, health/readiness, router composition |
| app/player.py | Optional unverified X-Player-ID selector with a shared default UUID |
| app/context.py | Selected player and request-local transaction/context |
| app/repository.py | Owned typed records, immutable question snapshots, PostgreSQL/memory adapters |
| app/platform.py | Profiles, extraction confirmation, lesson revisions/review, progress/deletion |
| app/generation.py | Concurrency permits, durable attempt/request limits, model runtime context |
| app/jobs.py, app/worker.py | Validated idempotent jobs, leasing, bounded retries, durable results |
| app/rooms.py | Private lobby creation, membership, readiness, snapshots, host transfer |
| learning_service/ | Existing lesson/quiz/flashcard/visual/weakness engines and integrated routers |
| supabase/migrations/ | Private schema, RLS, transactional room commands, worker/usage functions |
| tests/ | Public API, engine regression, HTTP, PostgreSQL policy/concurrency/worker verification |

FastAPI has no account/session endpoints, token dependencies, provider identity calls or security
schemes. The optional player UUID selects data/game state. PostgreSQL still uses its existing SQL
role/claims and partition rules, but these do not verify caller identity. Anyone can choose another
player UUID. Learner-approval receipts, proofs and state rules are separate workflow checks, with
caller-supplied guardian mailbox context. Read [current API](../API.md) and [storage](DATABASE.md).

Generated learning content starts as draft. Approval is explicit owner confirmation, not an
independent correctness guarantee. Edits create new lesson IDs. Immutable question sets are stored
separately from private attempt state. The visual harness validates code structure and containment;
it does not prove the generated content's factual correctness. The age-safety proposal remains unimplemented.

Durable jobs are processed by an explicitly started worker. Model calls are opt-in and budgeted;
failed workflows consume attempt budget too. Private rooms are lobbies only. No automatic gameplay,
public matchmaking, reward economy, family authorization, or hosted deployment is claimed.

## Server execution

PostgreSQL data/control connections use bounded pools with transaction-local identity. GETs use
read-only MVCC access. Progress and metadata projections are computed by SQL. Durable workers
generate into an isolated buffer after releasing source reads, then validate row versions and
the lease in a short commit transaction. See [server performance](SERVER_PERFORMANCE.md).
