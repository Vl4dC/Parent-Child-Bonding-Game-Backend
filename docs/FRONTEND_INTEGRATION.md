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

## Authentication and models

Supabase verifies real identity. Offline demo sessions are synthetic and are rejected in Supabase
mode. Use `/auth/login`, `/auth/me`, `/auth/refresh`, and `/auth/logout` as documented, or an approved
Supabase session integration supplying a real access token to this API. Serialize refresh operations
and replace rotated tokens. Choose one deliberate browser session strategy; this API sets no cookies.
Never copy the prototype's localStorage pattern for real refresh credentials by default.

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

Data IDs are owned. Sharing a quiz or material ID does not grant access. Separate accounts cannot
currently share lesson content through these private learning routes. Private lobbies grant access
to lobby snapshots only; they do not implicitly grant access to members' learning data.

## Chat with a PDF

`POST /api/chat` (see [API_PLATFORM.md](API_PLATFORM.md#chat-with-a-pdf)) is the contract for a chat screen with an
attach-PDF button and an "only answer from the PDF" switch (`mode: "pdf_only"`). Show `label` under an answer when
`used_pdf` is true. Upload with `POST /api/materials`, confirm the revision, then pass `material_id`; keep the
conversation on the client and send the last turns in `history`. Pictures: `POST /api/chat/image` (multipart `message`, `age`, `images`, optional `history`) lets the model look at a
photo, diagram or handwritten problem directly. Send the picture again with each follow-up, shrink large photos in
the browser first (the server accepts up to 8 MiB each), and show `label` under the answer. Pictures that fail the
child-safety check come back as the usual friendly 422. No frontend for this exists in this repository.

## Visuals and rooms

Visual HTML belongs inside a sandboxed frame with scripts allowed and no same-origin permission.
`/view` requests need Bearer authentication; fetch them with Authorization and use the response body.
Never put tokens into URLs. Sandbox/structural validation does not prove scientific correctness.

Private lobbies have eight-character invite codes, readiness, host transfer, closure, expiry, and
versioned snapshots. They do not start an imposter game or family duel. On Supabase, optional private
notifications say a room changed; refetch the authorized snapshot after notifications or reconnects.
No Presence publication or active-game synchronization is implemented yet.

A future game server must keep secret cards and answer keys off shared room payloads. The prototype's
full `RoomState.secret` must never become a public API response. Derive actors from verified identity,
validate phase/turn actions on the server, and choose/freeze rules before implementing each mode.
