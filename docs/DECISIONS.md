# Backend decisions and tradeoffs

Reviewed at `a85ec74`, 25 October 2026. Recorded choices are explicit in repository instructions/configuration. Other rationales below are inferred from code, not invented meeting history.

| Choice | Why it is useful | Cost / revisit condition |
| --- | --- | --- |
| One FastAPI factory and root lockfile | Composes engines and domain APIs with shared configuration, error handling and tests | Large modules need discipline; split packages/interfaces when coupling hinders changes |
| Python generation engines behind HTTP | Reuses document/model tooling while the frontend can evolve independently | Two runtimes/contracts; validate coordinated releases |
| Pydantic request/response models | Define shapes, reject invalid input and generate OpenAPI | Types do not establish factual quality or complete runtime validation of frontend responses |
| PostgreSQL private schema, RLS and request-local transactions | Durable records, atomic domain changes and selected-player partition enforcement | JSON domain records need careful validation/indexing; RLS cannot authenticate an unverified selector |
| Memory repository for explicit demo/test | Cheap deterministic tests and local API exploration | Ephemeral and not a production database; SQLite needs separate ephemeral configuration |
| SQLite for memory/home/social | Quick persistence behind small store interfaces and atomic local wallet changes | Hybrid backups and no distributed commit; consolidate behind the interfaces before multi-host deployment |
| Immutable questions and separate attempts | Historical results remain tied to exactly what was asked; retries can reuse content without AI | More versions/IDs and deletion bookkeeping |
| Explicit extraction/lesson/question approval | Makes source revisions and review state visible before scored reuse | Owner confirmation is not independent teacher review; automatic helper confirmation can be shallow |
| Separate main/visual models | Allows reasoning/planning and code drawing to be tuned independently | Provider configuration, latency and quality vary; measure before claiming superiority |
| Bounded review/retry graphs | Can repair invalid questions/visuals without unbounded loops | Extra calls/cost; some drawable diagrams survive exhausted quality retries |
| Safety prompts plus input/output gates | Applies school-age rules beyond generic provider filtering | Model checks, bounded inspection and user-supplied ages leave uncertainty; evaluate with real acceptance data |
| Leased background jobs with detached generation | Releases PostgreSQL connections during model work; prevents stale worker commits | Worker operations, retries and provider budget needed; not an exactly-once provider-call guarantee |
| Server clocks, expected versions and idempotency | Makes retries/reconnects and stale mutations predictable | Clients must retain keys/body for retries and refetch conflicts |
| Shared wallet ledger and server catalog | One balance, atomic purchases, repeat-safe reward references | Cross-store claims are not distributed transactions; new real-money/trading features need new accounting rules |
| Berlin calendar quests | Matches product calendar expectations, including daylight saving | Cannot model resets as fixed 24-hour intervals; retain timezone tests |
| Freeze/mask diagrams once | Study/game geometry stays identical and replay needs no model generation | Restricted Mermaid/SVG support and a separate browser renderer |
| Feature flags and explicit workers | Normal API startup does not silently send mail, migrate schema or run paid generation | Operators must enable dependencies deliberately |

## Explicit identity decision

Repository instructions record that **only `/api/social` verifies Clerk sessions**, while legacy routes keep caller-selected player partitions. The benefit is compatibility with existing learning/game contracts while adding verified social identity. The cost is substantial: anyone reaching legacy routes can choose another player's UUID, including for data reads/changes/deletion. Frontend sign-in, CORS and RLS do not remove that property.

Revisit this boundary before relying on the backend for verified private school records or family authorization. A broader authentication change requires a deliberate contract/migration decision; it must not be described as already implemented.

## Important limits of the justifications

These are explanations of the current design, not proof that every choice is optimal. The repository contains no comparative benchmark proving the selected models best, no controlled learning-outcomes study, no universal correctness/safety guarantee and no multi-host validation for the SQLite architecture. Prefer measurements and explicit acceptance criteria when revisiting a decision.
