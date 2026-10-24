# Non-AI quiz backend — lifecycle and question review

Added 24 October 2026. Backend-only additions based on main `a647491`. The source requirements are
the Drive **Feature / Frontend** inventory (quiz feedback, explanations, saved results/progress,
review cards) and **Parent–Child Bonding Game — Backend** contract (private snapshots, independent
attempts, first-answer progress, reviewed questions and safe retries).

This module consumes existing private question sets. It does not call or change the AI generator,
model providers, safety checker, prompts, visual-generation harness or generation jobs. Question
revisions are explicit owner-supplied corrections, not AI rewrites. The existing producer routes
remain unchanged and can keep supplying quizzes through their established storage contract.

No frontend is changed. Existing documentation is preserved and supplemented with this file,
appendices and a separate OpenAPI supplement. Learner approval remains a separate PR/workstream.

## Added endpoints

All paths below start with `/api/quiz/{quiz_id}` and require a verified Bearer session. Passing
another account's ID returns 404; rooms, account labels and family display names grant no access.

| Method | Suffix | Behavior |
| --- | --- | --- |
| GET | `/progress` | Saved first choices, answered/total/score, next unanswered index and completion state |
| GET | `/results` | Factual partial/final score, percentage, accuracy and completed time; no answer keys |
| POST | `/answers` | Strict first-answer recording, optimistic count check and idempotent feedback |
| POST | `/finish` | Finish a fully answered attempt once; return its stored final summary on retry |
| GET | `/review` | Full answer/explanation review after explicit finish |
| POST | `/retry` | Independent all-question or missed-question attempt, with idempotent creation |
| GET | `/attempts` | Paginated owner-only history for the exact same question snapshot |
| POST | `/review-deck` | Idempotently create review flashcards from missed/all existing questions |
| GET | `/author-review` | Explicit answer-containing view for the owning content author |
| POST | `/revisions` | Correct questions into a new draft quiz/snapshot; preserve earlier attempts/results |
| POST | `/approve` | Record owner confirmation bound to an exact question snapshot |

Existing `GET /api/quiz/{id}`, `POST /api/quiz/{id}/check`, `POST /api/quiz/{id}/attempts`,
and deterministic `POST /api/quiz/{id}/flashcards` keep their contracts. Generated new-question
follow-ups remain in the separate AI workflow; `/retry` deliberately reuses existing questions.

## Identity, storage and HTTP behavior

Routes use the existing verified `api_context` and owner-scoped PostgreSQL unit of work. The
in-memory adapter remains explicit development/test behavior. Private keys remain in the private
schema; it must not be exposed through the Supabase Data API. No cookie session or new credential
store is introduced. All responses use the existing no-store/no-referrer headers.

The composition code adds `Idempotency-Key` to allowed CORS headers without changing exact origins
or enabling cookies. `X-Request-ID` and `Retry-After` are exposed to browser clients. Cross-origin
preflight is tested. UI wiring and the main frontend's missing routes remain future frontend work.

Practice metadata lives inside the existing private quiz record's `submission.practice_v1` object.
Original question snapshots and the producer's typed models are unchanged. No new data kind is
introduced, so existing material/account-data deletion cascades remove retries, completion records,
review decks and receipts with their quiz/lesson data. Tests cover these cascades.

Migration `20261024000700_quiz_attempt_history.sql` adds only an owner/snapshot/cursor index for
history. It does not depend on learner approval migration 006. Migrations are never applied by API
startup. Only a disposable local test database was changed during development; no hosted database
was changed. History queries project away large receipt payloads and page at a bounded limit.

## Resume and first-answer behavior

`GET /progress` returns `id`, `question_set_id`, `lesson_id`, title/parent reference, status,
`answered`, `total`, `score`, `next_index`, `completed_at`, and saved `{index, choice, correct}`
entries. Only already-recorded choices are included. No unattempted key or explanation is returned.

Statuses are `not_started`, `in_progress`, `ready_to_finish`, and `completed`. Answering every
question makes an attempt ready; it does not silently complete a lesson or award anything.

For a new strict answer, send a fresh 8–128 character `Idempotency-Key` and:

```json
{"index":0,"choice":1,"expected_answered":0}
```

Indexes/choices/counts are strict nonnegative integers; booleans and numeric strings are rejected.
The expected count must match current saved progress. A later new-key request cannot replace a
first choice. Responses contain recorded choice, deterministic correctness/explanation/answer
diagram feedback, and progress. Conflicting payloads under the same key return 409.

An exact retry returns the original saved response even if subsequent answers or completion have
occurred. Its embedded progress is therefore a receipt-time snapshot: refetch `/progress` for current
state. There are at most 1,000 receipts per operation group per source attempt. The limit rejects
additional writes rather than silently dropping replay protection.

The legacy `/check` still permits repeated formative feedback for alternative choices. Its first
recorded choice remains authoritative, and both new and old progress totals use that first choice.
The new strict route does not convert private practice into trusted competitive scoring.

## Results, finish and learner review

`GET /results` is available before completion and returns factual counters without answer keys.
`percentage` divides correct first answers by the total question count; `accuracy_on_answered`
divides by answers actually recorded, or is null before any answer. Integer percentages round halves
upwards. `scoring_basis` is `first_recorded_answers`; `result_kind` is `private_formative_practice`.
There are no invented points, coins, pass thresholds, badge awards, mastery claims or elapsed-time
estimates.

Finish with:

```json
{"expected_answered":3}
```

Every question must have a saved answer and the count must match; otherwise return 409. The first
finish records a server UTC completion timestamp and final summary. Repeated finish calls with the
same valid count return the stored summary, without duplicate progress. New answer writes after
finish return 409, while an exact prior answer receipt remains replayable.

`GET /review` requires finish and returns each question, options, recorded choice, correct option,
explanation and any existing answer diagram. `lesson_id` links to the already-owned lesson and its
source material/revision/page references through the existing lesson API. No new explanation or
diagram is generated. Existing visual output still requires the documented sandboxed frontend view.

## Independent retries and attempt history

Send `Idempotency-Key` with `{"which":"all"}` (default) or `{"which":"missed"}` to `/retry`.
All-question retries reuse the immutable snapshot with independent empty answers. Missed-question
retries freeze the currently recorded mistakes into a new immutable subset, renumbered from zero.
They do not generate different questions or claim adaptive re-teaching. No recorded mistakes gives
409. Subsets do not inherit approval for the original full snapshot.

Retries never copy previous answers, completion time or answer receipts. The parent/source ID is
retained privately. Exact creation retries return the same target attempt; changed retry mode under
the same key returns 409. The existing memory adapter may report an optimistic 409 under a race;
repeat the exact key to converge. PostgreSQL serializes the owner's writes. Tests verify no orphan
or duplicate attempt is created.

`GET /attempts?limit=20&after=...` lists attempts for the exact same question-set ID, in ascending
opaque-ID order. Limit is 1–100. Return `next_cursor` until null. This is not chronological ordering.
Different corrected versions and missed subsets have different snapshot IDs and are intentionally
not mixed into the exact-snapshot history. Responses contain summary counts/status only, not sibling
answers, answer keys, receipts or other users' data.

## Review flashcards without generation

`POST /review-deck` accepts `{"which":"missed"}` (default) or `{"which":"all"}` plus an
idempotency key. It calls only the existing deterministic question-to-card conversion helper.
There is no model or safety-checker invocation. Empty mistake sets return 409.

The same key returns the same deck resource, with its current review state if it has since been
studied. Changed mode under the same key returns 409. Resume/review that deck through the existing
flashcard APIs. As with the legacy all-question card route, choosing `all` is a study aid that reveals
existing answers; it is not an anti-cheat exam mode or a trusted ranking mechanism.

## Author review, question corrections and approval

`GET /author-review` intentionally contains answer indexes/explanations. It is an explicit owning
author view, separate from play/progress and the learner's finish-gated full review. Owning a quiz
does not establish verified teacher/guardian authority or independent factual checking.

Send `POST /revisions` an idempotency key and:

```json
{
  "title":"Corrected arithmetic",
  "questions":[{
    "type":"multiple_choice",
    "question":"What is one plus one?",
    "options":["One","Two","Three","Four"],
    "answer_index":1,
    "explanation":"One and one make two."
  }]
}
```

The new draft has its own quiz and question-set IDs, empty answers, no completion and no approval.
The original quiz, snapshot and results remain unchanged. Input is limited to 1–50 text questions,
four distinct options for multiple choice, or `["True","False"]` for true/false, a valid answer
index and nonempty bounded explanation. Question order determines consecutive indexes. Existing
diagrams are not silently regenerated or copied into a changed text question. The lesson reference
is retained, not independently fact-validated. Revision counts track the correction chain; the
question-set ID is the authoritative immutable version reference.

Approve with `{"expected_question_set_id":"CURRENT_SNAPSHOT_ID","confirmed":true}`. A stale
snapshot returns 409. Approval records verified owner ID, server time and snapshot ID; repeated
approval is stable. A correction resets approval. This is owner confirmation only, not automated
fact checking, child-safety verification or an independent assessor. Practice remains formative even
when approved; the separate solo tournament review/scoring contract is unchanged.

## Scope still pending

AI generation, new-question follow-ups, model review/safety logic and provider configuration are
untouched. Full competitive matches, multiplayer, rankings, public matchmaking, shared/family
permissions, reward economy and module/game unlock rules need their separate settled contracts.
Frontend code/routes, account activation and exact under-16 eligibility enforcement are not changed
by this quiz-focused delivery. Runtime schema is available from `/openapi.json`; the additive
`quiz-practice.openapi.json` contains these eleven operations and referenced models only.

## Verification approach

Tests seed existing lesson/material/question records directly. Generation is disabled and model
entry points are replaced by failures: the deterministic workflow must succeed without invoking
them. Tests cover memory and real PostgreSQL storage, resume/results, first-choice protection,
receipt conflicts/caps, finish stability, half-up percentages, owner isolation, cursor history,
concurrent answers/retries, frozen mistake subsets, review-card idempotency, question corrections,
snapshot approval, source/result preservation, deletion cascades, restart persistence and CORS.
The full existing regression suite is also run with live paid model checks disabled.

### Completed local checks — 24 October 2026

Full suite: **607 passed, 17 skipped** in 20.02 seconds on Python 3.13 and disposable local
PostgreSQL 16. New quiz tests: **39 passed, 1 skipped**. Sixteen full-suite skips are memory
variants of database-only tests; the corresponding PostgreSQL cases ran. One skip disables
the opt-in paid live safety-model check. Lint passed and all 66 Python files passed formatting.
The eleven-operation OpenAPI supplement resolves all referenced schemas. Existing documentation
bytes remain unchanged prefixes; AI generation/safety/provider/harness files are unchanged.

Requirements references:
- https://drive.google.com/file/d/15GcZQK5aTKz4pb4coxDaMZSv9l_L7OTe/view
- https://docs.google.com/document/d/1i9_xaAnHKp2Fs3A6TcnZ2hi-HmDhiEKFqsexFEDJROA/edit
