# Frontend integration boundary

Frontend/product repository: https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game.
The inspected main snapshot is `c1663bc` (24 October 2026): QuestNest exports, reusable older
Kay/Splash components, and a starter homepage. Older page routes remain on `user_interface_v1`.
Fetch current source before implementing future requirements. No frontend code is changed here.

## Source context

| Main-repository path | Relevant context |
| --- | --- |
| `App-screen v2/` | QuestNest phone/tablet/website visual designs; not integrated app routes |
| `lib/kay/content.ts` | Older lesson, quiz, flashcard, and scripted tutor prototype data |
| `lib/kay/store.ts` | Prototype local preferences and profile storage |
| `lib/game/types.ts`, `lib/game/mock-room.ts` | Local imposter-game simulation and phase types |
| `components/kay/`, `components/game/` | Reusable UI components; presence does not prove routes work |
| `docs/PRODUCT_PLAN.md` | Product ideas; distinguish them from implemented API behavior |

The canonical current contracts are [API_PLATFORM.md](API_PLATFORM.md), [API.md](../API.md), and
`/openapi.json`. The backend supports owned learning, review, profiles, progress, jobs, and private
lobbies. Full game rules, public matchmaking, rewards, and family permissions remain pending.

## Public API and player state

FastAPI requires no login, access token, session cookie or Supabase Auth configuration. Frontend
login/register code is unchanged. Omit Authorization; old values are ignored. The optional
`X-Player-ID: <UUID>` selects player state; no header uses the shared default UUID documented in
[API.md](../API.md). Use a different ID for each multiplayer participant. These are unverified
selectors: callers can choose another ID, so they provide no account privacy or impersonation defense.

The learner-approval list/decision routes take `guardian_email` as a query filter, plus optional
X-Player-ID. The email is not verified. Existing receipt/proof and decision-state checks remain.

Backend account labels are `parent` and `learner`. Older Splash labels are `grownup`/`kid`, Kay labels
are `parent`/`kid`/`solo`, and the signup branch uses `student`/`parent`. Map display terms explicitly.
None is proof of a verified family relationship, age, consent, or permission to read another account.

Profiles use `preferences.read_aloud`; older client stores use `readAloud`. Profile PUT is replacement,
not a merge patch. Avatar references are display metadata and grant no inventory ownership.

## Learning integration

1. Upload material and retrieve its extracted pages.
2. Correct pages with an expected revision; confirm that exact revision explicitly.
3. Queue lesson generation with an Idempotency-Key and poll the owned job result, or use the legacy
   synchronous route for development. Generation must be explicitly enabled by the operator.
4. Review drafts; approval records owner confirmation, not independent factual verification.
5. Create private quizzes/flashcards. A new quiz attempt reuses an immutable question snapshot while
   keeping answers independent. Use Idempotency-Key and expected review counters for card retries.
6. Read real progress; there are no coin, badge, ranking, or inferred-mastery awards.

Data IDs are grouped under the selected player UUID. Selecting another UUID can access that
partition; this is no longer account isolation. Private lobbies grant access
to lobby snapshots only; they do not implicitly grant access to members' learning data.

## Chat with a PDF

`POST /api/chat` (see [API_PLATFORM.md](API_PLATFORM.md#chat-with-a-pdf)) is the contract for a chat screen with an
attach-PDF button and an "only answer from the PDF" switch (`mode: "pdf_only"`). Show `label` under an answer when
`used_pdf` is true. Upload with `POST /api/materials`, confirm the revision, then pass `material_id`; keep the
conversation on the client and send the last turns in `history`. Pictures: `POST /api/chat/image` (multipart `age`, `images`, optional `message` and `history`; no `message` means "explain this picture") lets the model look at a
photo, diagram or handwritten problem directly. Send the picture again with each follow-up, shrink large photos in
the browser first (the server accepts up to 8 MiB each), and show `label` under the answer. Pictures that fail the
child-safety check come back as the usual friendly 422. No frontend for this exists in this repository.

## Visuals and rooms

Visual HTML belongs inside a sandboxed frame with scripts allowed and no same-origin permission.
`/view` requests need no credentials; include X-Player-ID when selecting nondefault player state.
Use the response body in the sandbox. Sandbox/structural validation does not prove scientific correctness.

Private lobbies have eight-character invite codes, readiness, host transfer, closure, expiry, and
versioned snapshots. They do not start an imposter game or family duel. On Supabase, optional private
notifications say a room changed; refetch the authorized snapshot after notifications or reconnects.
No Presence publication or active-game synchronization is implemented yet.

A future game server must keep secret cards and answer keys off shared room payloads. The prototype's
full `RoomState.secret` must never become a public API response. Use the selected player ID for game actors,
validate phase/turn actions on the server, and choose/freeze rules before implementing each mode.


## Quiz lifecycle integration — 24 October 2026 addition

Use the new [deterministic quiz contract](QUIZ_PRACTICE_API.md) to resume first answers, display
factual results, finish/review, create independent retries and reuse question answers as cards.
Question corrections return a new quiz/snapshot; approvals are owner confirmations bound to that
snapshot. Future frontend wiring may supply X-Player-ID and must supply idempotency keys and current answer
counts. No frontend file was changed. Generation, safety checking and AI content production are
untouched; points/coins/badges, multiplayer and family access remain separate pending rules.


## Learner approval endpoints — additive note, 24 October 2026

Opt-in canonical routes now cover pending learner registration, private receipt polling,
notification resend and request-specific guardian decisions. See [LEARNER_APPROVAL.md](LEARNER_APPROVAL.md).
No frontend file was changed. The signup branch still needs future API-base/proxy wiring and
required headers; no same-origin /api/registration/learner alias is introduced. Parent/learner
labels are not authority. Birth-year intake does not settle the under-16 versus under-18 policy.
No activation, username login or automatic family-link behavior is included.


## Simultaneous diagram assembly — additive handoff, 24 October 2026

[The new backend contract](DIAGRAM_ASSEMBLY.md) supports selected saved flowcharts, study, room
discovery/readiness, a shared five-second countdown, private label placements and first-perfect
server timing. Frontend work is not included: proxy the new prefix with X-Player-ID/Idempotency-Key and
PUT support, map drops to SVG slot IDs, provide keyboard/accessible alternatives, and render
progress/results/review. The fixed prompt is “How does this work?”; no model generation is invoked.


## Diagram lifecycle API handoff — 25 October 2026 addition

Backend only: snapshots now include source availability and cancellation reason; finished results
remain readable after source withdrawal, while review may return 409. Refresh snapshots after
mutations/retries because an idempotency receipt is historical. The current room host can discover
a race ID/version via `/api/diagram-assembly/rooms/{room_id}/management` and cancel it without
participant content access. Finished/expired cancellation is 409. Source-change cancellation
does not reopen on reapproval. Readiness now exposes enabled diagram dependency failures.
See [new contracts and unchanged leave/rejoin policy](DIAGRAM_LIFECYCLE_READINESS.md). No client,
proxy, UI, AI or harness files were modified. Fresh per-race acknowledgement remains future work.
