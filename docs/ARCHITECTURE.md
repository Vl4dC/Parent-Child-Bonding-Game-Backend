# Backend architecture

QuestNest uses one FastAPI application factory, `app.main:create_app`, one root Python lockfile and one test tree. It combines reusable Python learning engines with deterministic application/game rules. Snapshot: `a85ec74`, reviewed 25 October 2026 against frontend `67b663f`.

## Modules

| Module | Responsibility |
| --- | --- |
| `app/main.py` | Factory, lifecycle, middleware, errors, router composition and health/readiness |
| `app/config.py` | Validated runtime settings and conservative feature defaults |
| `app/player.py` | Unverified legacy UUID selector and shared default |
| `app/clerk_social.py` | Scoped Clerk JWT verification and social identity context |
| `app/context.py`, `app/repository.py` | Request-local unit of work, quotas, PostgreSQL/memory adapters |
| `app/platform.py` | Profiles, extraction correction/confirmation, revisions, approval, completion, resource listing and deletion |
| `app/quiz_practice.py` | Saved first answers, finish, retry, review and immutable question snapshots |
| `app/generation.py`, `app/jobs.py`, `app/worker.py` | Permits, budgets, idempotent queue, leasing and execution |
| `app/rooms.py` | Lobby membership, versions, readiness, host actions and match discovery |
| `app/home_api.py`, `app/economy*.py` | Home, wallet, catalog, inventory, quests and verified-result reward claims |
| `app/frontend_social_api.py` | Frontend-compatible verified friends/presence/live duels |
| `app/social_api.py`, `app/game_invites.py` | Legacy selected-player social settings and lobby invitations |
| `app/diagram_*.py` | Optional saved-diagram rendering, race state and source lifecycle |
| `app/approval_*.py` | Optional request receipts/proofs, authority checks and explicit mail delivery |
| `learning_service/materials.py`, `embeddings.py` | PDF/photo extraction, chapters and keyword/semantic retrieval |
| `learning_service/lesson.py`, `quiz_graph.py`, `flashcards.py`, `weakness.py` | Structured learning and remedial generation |
| `learning_service/visuals/` | Skill-guided visual generation, validation and HTML rendering |
| `learning_service/safety.py` | Age-policy prompts, request/image checks and output gates |
| `learning_service/chat.py`, `image_chat.py`, `memory*.py` | Grounded chat, saved conversations and summaries |
| `learning_service/challenges.py`, `matches.py` and API wrappers | Generated challenge content and server-clocked MCQ/Heads-up/diagram play |
| `learning_service/live_social.py` | Verified profile binding, friendships and live duel state/settlement |
| `supabase/migrations/`, `tests/` | Ordered schema evolution and behavioral verification |

Historical `learning-service/static/` artifacts are not served as the product frontend. Use the unified factory rather than an old standalone learning server.

## Request and identity flow

Most domain routes enter `api_context`: resolve the selected player, reserve request allowance, open a request-local repository session, set context variables, run domain code and commit or roll back. Blocking connection/lock acquisition is moved off the event loop. Writes serialize by owner; GET repository access is read-only.

Legacy routes accept `X-Player-ID` without verification and ignore Bearer authentication. Missing headers use `00000000-0000-4000-8000-000000000001`. PostgreSQL RLS uses the selected UUID in transaction-local claims, so it enforces the selected partition rather than verifying who selected it.

Only `/api/social` requires Clerk JWTs. Verification uses configured issuer JWKS, RS256, required time/subject/session/player claims, pending-session rejection and an authorized-party check when `azp` is present. A supplied player header must match. Social profile storage binds issuer/subject/player and takes usernames from signed claims.

The frontend requires sign-in and adds its own headers, but direct legacy backend access retains the unverified contract. No parent label establishes guardianship. Approval receipts/proofs are separate workflow credentials.

## Persistence

| Store | Contents | Lifetime |
| --- | --- | --- |
| PostgreSQL repository | Materials/revisions, lessons, question sets, attempts, decks, completion/events, jobs, lobbies, request workflow and saved-diagram races | Durable with retained database and migrations |
| MemoryRepository | Demo/test alternative for repository records | Resets with process |
| SQLite `MEMORY_DB_PATH` | Profiles, conversations/messages, challenges, room matches and weekly samples | File by default; in-memory in tests unless overridden |
| SQLite `HOME_DB_PATH` | Homes, wallet/ledger, inventory/equipment, friendships, presence, social subject bindings and live duels | File by default; in-memory in tests unless overridden |

The two SQLite stores use process locks and explicit transactions. HomeStore uses WAL for files and `BEGIN IMMEDIATE` for mutations. Its unique `(player_id, reason, ref)` ledger key prevents duplicate credits/debits. The layout is suitable for the current single-host application; independent copies do not share state.

PostgreSQL and SQLite do not commit as one transaction. Reward claims read saved source outcomes and use durable ledger references for retry safety. Data deletion spans stores too; recovery/backups must account for all of them. The repository `durable` flag is not a full-system persistence guarantee.

## Learning pipeline

1. Upload a PDF or photos. Read embedded text locally and use vision for image pages. Retain extracted text rather than original image files.
2. Inspect/correct pages and confirm the current revision. Explicit pages/chapters or keyword plus optional semantic retrieval select context.
3. Generate a structured draft lesson. Save source revision/pages, sections, key points and visual plans.
4. Generate selected visuals separately. The harness loads kind-specific instructions, validates code and can retry with feedback.
5. Review/approve the relevant lesson/question snapshot for scored workflows.
6. Create practice/cards, save first answers and completion, then offer deterministic retries or generated weak-spot follow-ups.

Question sets are immutable snapshots separate from mutable attempts. Editing questions/lessons creates new versions/IDs so old results retain their original meaning. Approval means owner confirmation; helper-driven confirmation does not prove careful human review.

Text/photo chat can save a conversation, summarize long context and report book usage/pages. `auto` can answer without a book; `pdf_only` restricts it. Grounding improves traceability without proving correctness.

## Generation and safety

Main generation defaults to `openai/gpt-6-luna`; visual code defaults to `openrouter:anthropic/claude-haiku-5.5`. Bare vendor/model names go through the OpenRouter adapter. Names describe repository configuration, not independently benchmarked quality. Provider secrets stay server-side.

Quiz generation plans slots, fans out question generation and reviews/reworks rejected items with bounded loops. Visuals use planned diagram/chart/interactive skills; the harness can keep a drawable diagram after quality-review retries run out. Validation is not factual certification.

Safety applies age-banded prompt rules, request/image screening and generated-output checks. Rejected output can be rebuilt once, then blocked. Checker failures block policy ages up to 18. Age is user-supplied and capped at 18 by default; this does not verify a birth date. The text checker examines a bounded text window. Prompt/model checks cannot guarantee suitability or resist every attack.

## Inline versus background generation

Inline routes keep compatibility but can retain source locks during slow work. Durable jobs are preferred for long operations:

```text
submit validated job with idempotency key
  → worker claims expiring lease
  → snapshot sources and release PostgreSQL connection
  → generate in isolated buffer
  → short transaction rechecks lease and source versions
  → commit result or reject/retry stale work
```

Submission itself makes no model call. Ten pending jobs per owner, bounded retries and generation/request quotas limit work. Failed attempts consume budget; one workflow can make multiple model calls, so provider-side spending controls are still needed.

## Games and economy

Rooms hold lobby membership/readiness. Challenges hold generated game content. Matches execute MCQ, Heads-up or diagram rules on the server clock with captured participants and reconnect snapshots. These are separate from verified social live duels and same-device tournament duels.

The optional saved-diagram game renders reviewed Mermaid once, saves a reference and masks labels in a clone without rerunning layout. Source updates cancel active races; finished/expired outcomes remain protected under the lifecycle rules. Node/Chromium are required only when enabled.

Wallet actions use server prices and saved outcomes. Quiz first-answer credit is separate from the 20-coin participation component. Duel settlement includes that same component, avoiding a second participation payout. Quests derive progress from server events and use Europe/Berlin calendar periods. Exact eligibility is in [ECONOMY.md](ECONOMY.md).

## Transport and operations

API responses are no-store with request IDs and sanitized validation/storage failures. Body limits count actual received bytes. CORS uses exact origins; the Next.js proxy avoids browser-to-backend CORS for normal product calls.

`/health` checks process liveness. `/ready` checks repository readiness and enabled saved-diagram dependencies. It does not certify Clerk, model providers, mail delivery or both SQLite backup paths. See [operations](OPERATIONS.md), [decisions](DECISIONS.md) and [API contracts](../API.md).
