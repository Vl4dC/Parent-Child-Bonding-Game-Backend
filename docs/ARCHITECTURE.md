# Architecture

One FastAPI factory, one Python 3.13+ dependency lock, and one authenticated data boundary.
Frontend source remains in the main repository and is not modified here.

| Module | Responsibility |
| --- | --- |
| app/main.py | Factory, Supabase/demo auth, middleware, health/readiness, router composition |
| app/context.py | Verified identity and request-local transaction/context |
| app/repository.py | Owned typed records, immutable question snapshots, PostgreSQL/memory adapters |
| app/platform.py | Profiles, extraction confirmation, lesson revisions/review, progress/deletion |
| app/generation.py | Concurrency permits, durable attempt/request limits, model runtime context |
| app/jobs.py, app/worker.py | Validated idempotent jobs, leasing, bounded retries, durable results |
| app/rooms.py | Private lobby creation, membership, readiness, snapshots, host transfer |
| learning_service/ | Existing lesson/quiz/flashcard/visual/weakness engines and integrated routers |
| supabase/migrations/ | Private schema, RLS, transactional room commands, worker/usage functions |
| tests/ | Authentication, engine regression, HTTP, PostgreSQL policy/concurrency/worker verification |

API requests verify Supabase identity, then switch the database transaction to the authenticated role
with that identity. Records are owned; account metadata is never permission. The private schema is
not a browser API. Read [database boundaries](DATABASE.md) and [contracts](API_PLATFORM.md).

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
