# End-to-end edge-case test report

Date: 24 October 2026. Base: `main` at `a647491` (child-safety guardrails merged). Branch: `codex/e2e-edge-case-tests`.
Scope: backend only. Nothing under `app/`, `learning_service/`, `supabase/` or the frontend was changed to make
this suite pass; every defect found is recorded below with a proposed fix and a strict-xfail test that turns
green-to-red the moment someone fixes it (so the marker gets removed).

## 1. What is implemented today

| Area | Endpoints | What it does |
| --- | --- | --- |
| Authentication | `/auth/register`, `login`, `verify`, `resend`, `refresh`, `me`, `logout`, `forgot-password`, `PUT password`, `demo-accounts` (demo only) | Supabase verifies every token; an offline demo mode (off by default, refused in production) has two synthetic accounts, rotating refresh tokens and replay rejection |
| Health and docs | `/health`, `/ready`, `/docs`, `/redoc`, `/openapi.json` | Process health; storage readiness and durability; generated schema |
| Profile, progress, data | `GET/PUT /api/me/profile`, `GET /api/me/progress`, `DELETE /api/me/data`, `GET /api/resources/{kind}` | Replace-style profile, factual counters only (no coins/XP), cursor-paged metadata listings, total owned-data deletion that also leaves lobbies |
| Your book | `POST/GET/DELETE /api/materials`, `GET/PUT …/pages`, `GET …/revisions/{n}`, `POST …/confirm`, `GET …/find` | PDF or up to 20 photos, local text extraction plus vision for pictures, corrections with expected-revision locking, explicit confirmation, immutable revisions, free page preview |
| Lessons and review | `POST/GET /api/lessons`, `…/revisions`, `…/approve`, `…/complete` | Age-pitched lessons (general knowledge or only from a confirmed book), draft → owner-approved, edits create a new lesson id, idempotent completion |
| Visuals | `POST …/visuals`, `GET …/visuals/{id}/view` | Mermaid, Vega-Lite and sandboxed interactive pages with a strict CSP; new visuals withdraw approval |
| Quizzes | `POST /api/lessons/{id}/quiz`, `GET /api/quiz/{id}`, `…/attempts`, `…/check`, `…/questions/{i}/view` | Private practice, immutable question sets, independent attempts, first answer is the only one recorded |
| Diagram recall | built into quizzes and decks | Hide one box of a lesson diagram and ask what belongs there (multiple choice or true/false); the code, not the model, does the hiding |
| Weak spots | `…/followup`, `POST /api/quiz/{id}/flashcards` | New questions on what was missed; mistake decks |
| Flashcards | `POST /api/lessons/{id}/flashcards`, `GET/POST /api/flashcards/{id}[/review]`, `…/cards/{i}/view` | Leitner-style loop, idempotent reviews (`Idempotency-Key` + `expected_reviews`) |
| Solo tournament | `POST/GET /api/tournament`, `…/review`, `…/approve`, `…/submit` | Scored set that needs lesson approval, then explicit question review, then one submission |
| Generation jobs | `POST/GET/DELETE /api/jobs` and `python -m app.worker` | Idempotent queue, ten pending per account, leases, bounded retries, source-version checks |
| Private rooms | `POST /api/rooms`, `…/join`, `GET …/{id}`, `POST …/commands` | Lobbies only: invite code, readiness, host transfer, close, 24 h expiry, versioned snapshots |
| Cross-cutting | — | Per-account request limit, body-size limits, exact CORS, request ids, no-store, memory and PostgreSQL adapters (RLS), daily generation budget and concurrency gate, child-safety guardrails (age policy in every prompt, request screening, output gate that fails closed) |

43 `/api` operations in total (read from the generated schema by the suite).

**Not in this checkout yet:** the "memory" feature. It was not in `main`, any local branch, or a stash when this
report was written; it is described as pending. Everything else pending is unchanged from the README
(guardian linking, rewards, gameplay on top of rooms, public matchmaking, rankings).

## 2. What we can do next

Bug fixes first (section 5), in this order:

1. **Quick, mechanical:** BUG-02, 04, 05, 10, 12, 15, 16, 17 (one line to a few lines each).
2. **Input robustness:** BUG-01 (NUL handling), BUG-07 (strict integers), BUG-14 (malformed provider 200), BUG-03 (500 headers).
3. **Uploads and jobs:** BUG-06, 09, 08, 13.
4. **Security hardening needing a migration:** BUG-11 (room invite codes in PostgreSQL), plus join-attempt throttling.

Then, in rough order of value for the hackathon:

- **Per-account storage quotas.** Only generation is budgeted; lesson revisions, attempts and rooms are bounded only by 120 requests per minute (section 6).
- **Spaced repetition on the data already stored.** Flashcard `again/know` counters and first-answer quiz history are enough to schedule "review these today" without new tables or rewards.
- **Guardian link model** (pending product decision) so a parent can see a child's progress deliberately instead of by label.
- **Freeze one room game's rules** (the Forbidden Clues roadmap already lists the server contract and required tests).
- **Smoke-test a hosted Supabase project** (Auth email flows and Realtime are the two things this suite cannot prove).

### Checklist for the memory feature when it lands

Run `tests/e2e` first (it should stay green), then give the new routes the same treatment. Each item has a
ready-made pattern in the suite:

- Verified identity on every route (`test_http_surface.py::test_every_data_route_rejects_missing_or_bad_credentials` walks the schema, so new routes are covered automatically).
- Ownership and cross-kind id confusion (`test_cross_user_isolation.py`): add the new ids to the `world` fixture and the `attacks` list.
- Boundary and type tables for every field (`test_profile_progress_resources.py`, `test_learning_flow_edge_cases.py`): min, max, max+1, wrong types, extra fields, NUL, emoji, whitespace-only.
- Answer/secret leakage: nothing playable may contain an answer key before the learner answers (`ANSWER_KEYS` scan).
- Idempotency and races: same key twice, changed body, parallel identical requests, parallel different requests.
- Deletion: add its record kind to `DELETE /api/me/data` and `delete_material_data`, and to the listing kinds if it should be listed.
- Progress counters: decide what it adds (if anything) and assert nothing else moves.
- Child safety: any text a learner will read goes through `ensure_safe`; any text the learner types goes through `screen_request`.
- Both adapters: write the test against the `repository` fixture so it runs on memory and PostgreSQL.

## 3. The new test suite (`tests/e2e/`)

1,326 new cases (each runs on the in-memory adapter and, when `TEST_DATABASE_URL` is set, on PostgreSQL).
The model, vision and safety calls are faked, so nothing is billed.

| File | Cases | Covers |
| --- | ---: | --- |
| `test_http_surface.py` | 155 | credentials on all 43 operations, cookie/query/header spoofing, no-store and request ids on every error class, validation never echoing input, malformed/nested/duplicate-key JSON, body limits (streamed and at the exact boundary), hostile path ids on every id route, NUL bytes, CORS, docs leakage, health/readiness, request-limit boundaries and window reset, 500 handling, generation gates and budgets |
| `test_auth_edge_cases.py` | 117 | demo login and token tampering, refresh races, logout, session cap and pruning, secrets in logs; Supabase mode: input rules, upstream error mapping, malformed provider replies, forged JWTs never trusted, id-spelling normalisation, concurrent tokens never mixed, settings and production start-up guards, example env files |
| `test_profile_progress_resources.py` | 126 | every profile bound, replace semantics, concurrency, progress counting rules, paging walk across page sizes, listing never exposing text/answers/inputs, total data deletion |
| `test_materials_edge_cases.py` | 164 | 13 refused uploads (image bomb, 64 MP, truncated, disguised, HEIC, SVG, two PDFs …), every picture format, hostile file names, 21 page-correction rules, 600/601 pages, 1 M characters, confirmation, revisions, correction races, 21 hostile preview queries, the deletion cascade |
| `test_learning_flow_edge_cases.py` | 292 | age/topic/size bounds, mass assignment, approval rules and withdrawal, revisions, completion races, visual-page escaping parsed like a browser, answer-key scan of every playable quiz, first-answer rule, per-attempt answer gating, diagram leak checks, follow-ups, mistake decks, flashcard idempotency/receipt cap/races, tournament scoring extremes and a single winner under races |
| `test_jobs_edge_cases.py` | 86 | validation matrix for all five kinds, key length, idempotency and scope, the ten-pending ceiling, cancellation, response shape, every job kind run through the real worker on PostgreSQL, budget exhaustion |
| `test_rooms_edge_cases.py` | 148 | creation bounds, codes, capacity (including filling eight seats and a race for the last one), version rules, host transfer order, close/leave rules, expiry, membership privacy |
| `test_cross_user_isolation.py` | 12 | a second account and an anonymous caller try 40 operations on every id the first account owns; the owner's data must be byte-identical afterwards; foreign and non-existent ids must be indistinguishable; ids of one kind never work as another |
| `test_visual_recall_hardening.py` | 222 | 190 random diagrams prove a hidden label never stays readable, regex/markup-hostile labels, pathological diagram code in bounded time, what the model may contribute |
| `test_repo_hygiene.py` | 4 | no credential-shaped strings, env or key files tracked; only the two synthetic demo passwords published |

Run it:

```sh
uv run pytest tests/e2e -q                       # in-memory adapter, no setup
TEST_DATABASE_URL=postgresql://localhost/questnest_api_test uv run pytest tests/e2e -q   # both adapters
```

### Results

| Run | Passed | Skipped | Expected failures (strict xfail) |
| --- | ---: | ---: | ---: |
| Before this work, no database | 425 | 159 | 0 |
| Before this work, PostgreSQL | 568 | 16 | 0 |
| With the new suite, no database | 1,228 | 653 | 29 |
| With the new suite, PostgreSQL 18.4 | 1,837 | 19 | 54 |

`ruff check` and `ruff format --check` are clean for `app learning_service scripts tests`. The skips without a database are the PostgreSQL
halves of each case, exactly as the existing suite behaves. PostgreSQL was an embedded local server on a throwaway
port; CI uses PostgreSQL 17.

## 4. What held up

Every one of these was attacked and behaved correctly on both adapters:

- **Identity:** missing, malformed, forged (`alg: none`), demo-prefixed or expired tokens; tokens in query strings, cookies or look-alike headers; the provider is asked about every token and local decoding is never trusted; provider users are separated even under 40 concurrent requests; metadata labels (`parent`, `admin`, same family id) grant nothing.
- **Isolation (IDOR):** all 40 operations on all nine id kinds, from another account and from nobody; refusal is a uniform 404/401 and mutates nothing.
- **Mass assignment:** `status`, `approval`, `id`, `host_id`, `code`, `version` and friends in request bodies are ignored or rejected; a lesson cannot be created approved.
- **Injection and traversal:** SQL-shaped, path-traversal, script and over-long identifiers are plain 404s; markup in names, titles and file names is returned as inert JSON; visual pages parse (as a browser would) with no injected element and a locked-down CSP and sandbox.
- **Oversize and decompression:** 4 MiB JSON limit exact at the boundary and enforced on streamed bodies; image bombs and 64-megapixel pictures are 413; truncated and disguised files are 415/422.
- **Replay and races:** refresh-token replay, double refresh, duplicate job keys, flashcard review retries, tournament resubmission, parallel approvals/completions/answers/room joins all end in one consistent state.
- **Information leaks:** no echoed input in validation errors, no upstream text, no stack trace on 500, no secrets in logs, no answers in any playable quiz, tournament or follow-up, no hidden diagram label in the picture, schema free of internals, no credential-shaped strings in tracked files.
- **Limits:** request limit boundary/reset/per-account, ten-pending jobs, daily generation budget, generation concurrency, 1,000-session demo cap, 1,000-receipt flashcard cap, room capacity 2–8.
- **Start-up safety:** production refuses memory storage and missing `DATABASE_URL`; demo mode refuses anything but development/test; secret and service-role keys and wildcard CORS are refused.

## 5. Bugs found

All are covered by `xfail(strict=True)` tests whose reason starts with the id. "PG" means the defect appears only on
PostgreSQL. Severity is for this product (a children's learning app), not generic.

| ID | Severity | Where | What happens | Proposed fix |
| --- | --- | --- | --- | --- |
| BUG-01 | Medium-low | any user text, path id or cursor | A NUL (`\u0000`) is **stored** on the memory adapter and becomes a **503 "storage unavailable"** on PostgreSQL (jsonb rejects it): lesson topic, room and profile names, page text, `…/a%00b` ids, `?after=` | Reject NUL in one shared validator (or strip it like `materials.clean` does); validate id shape; map `psycopg.DataError` to 422/404 |
| BUG-02 | Low (PG) | `POST /api/rooms/{id}/commands` | `expected_version` beyond the SQL integer range is a 503 instead of 409/422 | `le=2**31-1` on `RoomCommand.expected_version` (and on the other counters for hygiene) |
| BUG-03 | Low-medium | any unhandled exception | The 500 response has no `Cache-Control: no-store` and no `X-Request-ID`, contradicting the docs ("all responses") and removing the support correlation id | Add an `Exception` handler that returns JSON with the headers, or set them in an outermost pure-ASGI middleware |
| BUG-04 | Low | request-limit 429 | No `Retry-After` header | Compute seconds to the next minute window in `reserve_request` |
| BUG-05 | Low | room create/join | Display names are stored untrimmed (`"  Alex  "`); profiles and registration trim | `.strip()` in `create_room` and `join_room` |
| BUG-06 | Low-medium | `POST /api/materials` | GIF, BMP, TIFF, ICO, PPM and PCX are accepted although only JPG/PNG/WebP are documented; Pillow's many other decoders stay reachable | `Image.open(…, formats=("JPEG","PNG","WEBP"))` |
| BUG-07 | Low | all integer fields | `true`, `"12"`, `12.0`, `1` are accepted for integers and for `confirmed: Literal[True]` (e.g. `choice: true` is option 1, `card: true` is card 1) | Strict ints/bools in the request models (`ConfigDict(strict=True)` or `StrictInt`) |
| BUG-08 | Low-medium | `POST /api/jobs` | A lesson job for an **unconfirmed** material is accepted, then fails in the worker and burns daily generation attempts on every retry | Mirror `create_lesson`'s 409 at submission |
| BUG-09 | Low | `POST /api/materials` | The client's file name, including `../../` paths and HTML, is stored and returned verbatim as `name` | Keep only the base name, strip control characters, cap the length |
| BUG-10 | Low | `/health`, `/ready` | `HEAD` is 405; many uptime monitors use `HEAD` | Register both routes for `GET` and `HEAD` |
| BUG-11 | **Medium** (PG, security) | PostgreSQL room invite codes | Codes come from `gen_random_uuid()` and are **8 hex characters (32 bits)**; the memory adapter uses a 32-symbol alphabet (40 bits). Joining is limited only by 120 requests/minute/account, so codes of live lobbies are guessable at scale | New migration (never edit applied SQL): generate codes from the 32-symbol alphabet, consider 10 characters, and throttle failed joins per account |
| BUG-12 | Low | settings | `CORS_ORIGINS=["null"]` passes validation as an "exact" origin and would trust sandboxed iframes and `file://` pages | Reject `"null"` in `explicit_origins` |
| BUG-13 | Medium-low | diagram recall | A model-written wrong option containing a newline is substituted into the learner's Mermaid code and injects statements (`click`, extra nodes) that bypass `validate()`, which only ran on the original diagram. Mermaid's strict mode blocks scripts, but the picture is no longer the validated one. Needs a hostile model reply (for example via a poisoned book) | In `_clean_distractors` drop options with control characters, quotes or brackets, and re-run `validate("diagram", …)` on the filled code |
| BUG-14 | **Medium** | `/auth/login`, `refresh`, `verify`, `me`, `password` | A 200 from Supabase with an empty body, a JSON list or no `user` becomes an unhandled **500** (plain text, no headers) instead of the sanitised 502 used for non-JSON bodies | Validate the upstream payload inside `upstream()` and answer 502 "Unexpected authentication response." |
| BUG-15 | Low | visual pages | `_script_json` escapes `</` but not `<!--`, so `<!--<script>` in a visual's `alt` can swallow the page's closing `</script>` and break rendering | Also replace `<!--` |
| BUG-16 | Low | 401 for an invalid token | `WWW-Authenticate` is sent for a missing credential but not for an invalid or expired one (RFC 6750) | Add `headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""}` to those 401s |
| BUG-17 | Low (cosmetic) | approval timestamps | The same `reviewed_at` instant is `…Z` in the approve response and `…+00:00` in `GET /api/lessons`; PostgreSQL also returns room `expires_at` in the server's UTC offset | One serializer for all timestamps |

## 6. Hardening notes (not failures of the current contract)

- **No storage quotas.** `POST /api/lessons/{id}/revisions` accepts a 3 MB lesson (only the 4 MiB body cap applies); attempts, revisions and rooms (30 created by one account without complaint) are unlimited apart from 120 requests/minute.
- **`X-Content-Type-Options: nosniff` is missing** on the HTML `/view` pages (they already carry `Content-Security-Policy: sandbox allow-scripts`).
- **No application-level login throttling** (documented; Supabase rate-limits upstream). Failed demo-login attempts are unlimited.
- **Room snapshots expose each member's `user_id`** (the Supabase auth UUID) to the other members; the docs list only names and readiness. Decide whether to expose an opaque per-room member id instead.
- **Logout needs a live access token.** An expired access token returns 401 and the refresh token stays valid until its own expiry (demo mode; Supabase behaves the same way upstream).
- **Demo session cap.** Anyone who knows the published demo password can fill all 1,000 slots and get 429 for everyone for 24 hours. Demo only.
- **The memory adapter is weaker under races** (a lost race can answer 409, or 200 for a duplicate with identical state); PostgreSQL serialises per owner. It is demo/test only, but test expectations in `tests/e2e` accept both.
- **`parent_id` is overloaded.** A new attempt of a quiz sets `parent_id` to that quiz, the same field follow-ups use for "the quiz whose mistakes this came from", and an attempt of a follow-up loses its own lineage. A client treating `parent_id != null` as "follow-up" will mislabel attempts.
- **Legacy learning routes silently ignore unknown JSON properties**, while auth, platform, jobs and rooms reject them. Harmless (they are not applied) but inconsistent.
- **`avatar_reference` is free text** (up to 128 characters, `javascript:` accepted). It is inert JSON, but the frontend must never treat it as a URL.

## 7. Limits of this run

- **Frontend repo:** `RahulRohidasPatil/Parent-Child-Bonding-Game` returned "not found" for the signed-in GitHub account, so
  the local snapshot described in `docs/FRONTEND_INTEGRATION.md` (`c1663bc`) was the only frontend context used.
- **Supabase** was simulated with a mock transport: no real tokens, e-mails or Realtime events were involved.
- **No real model calls.** Prompts are asserted on, not model quality.
- **PostgreSQL** was a local embedded 18.4 server; CI uses 17.
- Timing-sensitive assertions (request-limit windows) use a patched clock rather than waiting.
