# Platform API contract

All `/api/` endpoints work without authentication. Optional `X-Player-ID` selects a UUID data
partition; no header uses `00000000-0000-4000-8000-000000000001`. Requests are limited per selected
player per minute. This is unverified state selection, not account isolation: anyone can choose
another player's UUID. Gameplay membership, host actions, review and idempotency rules remain.
All API responses are non-cacheable and carry an `X-Request-ID`. See [API.md](../API.md).

Existing learning paths remain compatible except the explicit changes below. OpenAPI is generated
from the actual routes and typed models at `/openapi.json`.

## Endpoint inventory

| Method | Path | Behavior |
| --- | --- | --- |
| GET / PUT | `/api/me/profile` | Read or replace display name, goal, avatar reference, and accessibility preferences |
| DELETE | `/api/me/data` | Delete owned application content/jobs/progress and leave all rooms; auth account remains active |
| GET | `/api/me/progress` | Factual completion, first quiz-answer, correct-answer, card-review, and submission totals |
| GET | `/api/resources/{kind}` | Owned metadata summaries; `after` cursor and `limit` 1–100; no text or answer keys |
| POST | `/api/materials` | Upload one PDF or 1–20 JPG/PNG/WebP photos; extract text, then embed it for search by meaning |
| GET / DELETE | `/api/materials/{id}` | Read metadata or delete material and its derived learning records |
| GET | `/api/materials/{id}/pages` | Read extracted pages, revision, and confirmation state |
| PUT | `/api/materials/{id}/pages` | Replace corrected pages with an expected revision; clears confirmation |
| GET | `/api/materials/{id}/revisions/{revision}` | Read the immutable extracted-text revision referenced by an older lesson |
| POST | `/api/materials/{id}/confirm` | Explicitly confirm the current extracted-text revision |
| POST | `/api/chat` | Chat, optionally with a confirmed upload: answers from the PDF when it helps and says so (`used_pdf`, `pages`, `label`) |
| GET / PUT | `/api/memory/profile` | The player's role label (`parent` or `learner`) and display name |
| POST / GET | `/api/memory/conversations` | Start a remembered chat / list this player's chats with a count per subject |
| GET / PATCH / DELETE | `/api/memory/conversations/{id}` | Open a chat with every message / rename it or change its subject / forget it |
| GET | `/api/memory/digest` | Subjects and topics this player has studied, with no chat text |
| POST | `/api/chat/image` | Chat about 1-4 pictures (multipart): the model looks at the picture itself; nothing is stored |
| GET | `/api/materials/{id}/find?topic=…` | Preview selected pages/chapter/source text; no lesson is written (`by_meaning` says whether embeddings took part) |
| POST / GET | `/api/lessons`, `/api/lessons/{id}` | Generate draft lesson / read owned lesson |
| POST | `/api/lessons/{id}/revisions` | Create a new draft lesson ID with edited lesson content |
| POST | `/api/lessons/{id}/approve` | Record owner confirmation of a nonempty revision |
| POST | `/api/lessons/{id}/visuals` | Generate planned or requested visuals; new visuals invalidate approval |
| GET | `/api/lessons/{id}/visuals/{visual_id}/view` | Authorized sandboxed visual document |
| POST | `/api/lessons/{id}/quiz` | Generate private practice questions and initial attempt |
| GET | `/api/quiz/{id}` | Read public questions/options, excluding answer keys |
| POST | `/api/quiz/{id}/attempts` | Start an independent attempt using the same immutable question set |
| POST | `/api/quiz/{id}/check` | Check a choice; only the first answer contributes to saved progress |
| GET | `/api/quiz/{id}/questions/{index}/view` | Question diagram; completed diagram requires an answer first |
| POST | `/api/quiz/{id}/followup` | Generate new practice from recorded mistakes |
| POST | `/api/quiz/{id}/flashcards` | Create cards from missed/all questions without a model call |
| POST | `/api/lessons/{id}/flashcards` | Generate private lesson flashcards |
| GET | `/api/flashcards/{id}` | Read cards and individual review state |
| POST | `/api/flashcards/{id}/review` | Mark a card `know` or `again` |
| GET | `/api/flashcards/{id}/cards/{index}/view` | Sandboxed diagram side of a card |
| POST | `/api/lessons/{id}/complete` | Record a completion once; repeated calls return the same completion |
| POST | `/api/tournament` | Generate a solo scored question set from an approved lesson |
| GET | `/api/tournament/{id}` | Answer-free play representation |
| GET / POST | `/api/tournament/{id}/review`, `/api/tournament/{id}/approve` | Explicit author review and owner confirmation |
| POST | `/api/tournament/{id}/submit` | Save one complete scored submission; identical retries return the same result |
| POST / GET / DELETE | `/api/jobs`, `/api/jobs/{id}` | Queue generation / inspect status / cancel queued work |
| POST | `/api/rooms` | Create private lobby |
| POST | `/api/rooms/join` | Join by invite code; repeat joins do not duplicate membership |
| GET | `/api/rooms/{id}` | Authorized snapshot for initial load/reconnection |
| POST | `/api/rooms/{id}/commands` | Versioned readiness, leave, or host-close command |

`resources` kinds: `materials`, `lessons`, `quizzes`, `tournaments`, `decks`, `jobs`, `completions`.
Pagination order is ascending opaque ID; pass `next_cursor` as `after`. Stop when it is null.

## Review and versioning examples

Corrected text uses consecutive 1-based page numbers, up to 600 pages and one million characters:

```json
{"expected_revision":1,"pages":[{"number":1,"text":"Corrected source text.","via":"text"}]}
```

Replacing text increments the revision and resets `confirmed`. Original revisions remain readable;
lessons record `source.material_revision` as well as material ID and page numbers. Confirm with:

```json
{"expected_revision":2,"confirmed":true}
```

The same approval shape applies to lesson revisions; initial lessons are revision 1. Stale revisions
return 409. Lesson edits use `{"lesson":{"title":"…","summary":"…","sections":[{"heading":"…","body":"…"}],"key_points":["…"]}}`.
A revision returns a new ID and links to its predecessor. It never edits earlier lesson IDs or attempts.

Material-backed lessons require confirmation before generation. General-knowledge lessons can be
created without an upload and remain drafts. Practice is private formative work; scored solo sets
require lesson approval followed by explicit question review and approval. The author-review route
intentionally contains answers, so these self-reviewed results are not trusted ranked competition.

Profile PUT replaces the complete profile: omitted values reset to defaults. Avatar references are
display preferences and grant no cosmetic ownership or purchases.

## Attempts and progress

The initial `/quiz` ID is an attempt with an immutable question-set reference. `/attempts` creates
another attempt with the same questions and no answers recorded. Attempts are private to their owner;
selecting the same player UUID accesses the same state. Practice `/check` can be repeated for feedback, but later choices never
replace the recorded first choice. Flashcard reviews should send `Idempotency-Key` and `expected_reviews` (the current review counter).
Identical retries return their saved response; changed payloads or stale counters return 409. Each
deck retains up to 1,000 review receipts. Legacy reviews without these fields remain accepted and
do not provide replay protection. Completion markers are idempotent. No coins, XP, badges, mastery
claims, or inferred time-spent are granted by progress endpoints.

Tournament submission needs an approved question set. A repeated identical submission returns the
original score; changed answers return 409. This remains a solo scored practice foundation, not a
multiplayer tournament. Review is owner confirmation, not independent verification.

## Generation jobs

Send `Idempotency-Key` (8–128 characters) with:

```json
{"kind":"lesson","parameters":{"topic":"photosynthesis","age":12,"material_id":"OWNED_MATERIAL_ID"}}
```

Other kinds: `quiz`, `flashcards`, `visuals`, `followup`. Supply the owned lesson ID as `resource_id`
for quiz/cards/visuals; supply an owned quiz ID for followup. Parameters use the matching synchronous
endpoint's request model. Unknown parameters are rejected. Responses contain an ID, status, attempts,
created time, sanitized error, and result references. Inputs and lease credentials are not returned. Source ID/kind/revision are returned when applicable;
a worker rejects a changed material revision rather than silently generating from different text.

States: `queued` → `running` → `succeeded`, with bounded retries back to `queued` or terminal `failed`.
A stopped worker's lease expires after 15 minutes; another worker can reclaim the job. A stale worker
cannot commit output after its lease has been replaced. Job retries may repeat paid provider calls;
they do not promise exactly-once provider billing. Only queued work can be cancelled; repeating cancellation is safe. Cancelling running or completed work returns 409.

Generation is opt-in. Workflow-attempt limits count failed calls too, independently of request rollback.
`GENERATION_LIMITS_ENABLED=false` lifts the daily attempt budget, the per-minute request budget and
the inline "generation is busy" gate (usage is not counted while off). It is for testing only: with it
off nothing caps provider spend or concurrent model calls. The diagram renderer's own busy limit stays.
`GENERATION_CONCURRENCY` limits inline calls per API process and running durable jobs across workers.
The inline and worker pools are separate; use durable jobs for production scheduling. Durable workers
release database connections during model calls and verify source versions before committing, so
profile/lobby updates and status reads can continue while generation runs. Provider calls
have explicit timeouts and no automatic SDK retries; workflow retries are bounded in the engines.

Uploads currently run synchronously because file streams are not retained. Text PDFs are extracted
locally; photo/scanned extraction can call a vision provider. Generation enablement/budgets apply to
uploads conservatively even when a particular PDF needs no model call.

### Search by meaning (embeddings)

After extraction, `POST /api/materials` cuts the text into pieces of at most 1,000 characters (never across
a page; contents/index pages are skipped), embeds them with `EMBEDDING_MODEL` (default
`openrouter:openai/text-embedding-3-small`, `EMBEDDING_DIMENSIONS` 512; `openai:<model>` also works) and stores
one unit vector per piece as an owned `material_vectors` record keyed `<material id>:<revision>`. The response's
`indexed_chunks` is the number of pieces; 0 means keyword search only. No extra database extension or
migration is needed: the index is per book, so a plain cosine scan over a few thousand vectors is the search.

A topic request (not `pages 40-55`, not `chapter 3`) runs two searches and merges them by reciprocal rank
fusion: BM25 words and the pages whose best piece has cosine similarity of at least 0.25 to the topic. A topic
the words miss ("how do plants make food") or misspell ("photosynthsis") is found by meaning; a topic neither
search finds still returns 422 before any lesson is written. The selected pages' full text, not the pieces,
is what the lesson sees, so `source.pages` and truncation behave as before.

Embedding is best effort and never blocks anything. With no model, key, or a provider outage, the upload still
succeeds (`warnings` explains an outage) and topic search uses keywords. Correcting pages (`PUT .../pages`)
deletes the stored vectors; the next lesson from the confirmed text embeds it again, as does the first lesson
from a book that was uploaded while embeddings were off. `GET .../find` only uses an existing index (it costs
one tiny embedding call for the topic, only when generation is enabled) and never builds one. Vectors are deleted
with the material and by `DELETE /api/me/data`, and are never returned by any endpoint.

## Chat with a PDF

`POST /api/chat` takes `{"message", "age", "material_id"?, "mode", "history"}` and returns
`{"answer", "used_pdf", "pages", "label", "mode"}`. It is stateless: send the recent turns in `history`
(`{"role":"user"|"assistant","content"}`, the last 10 are used). Without `material_id` it is an ordinary chat.
With one (a confirmed upload, else 409) the question is searched in the book by words and by meaning at the
level of 1,000-character pieces, and the best few pieces plus a short outline of the whole file (name, chapters,
samples from across it) go to the model, so "what is on the PDF?" works as well as a specific question.

- `mode: "auto"` (default): the model uses the PDF when it helps and otherwise answers from general knowledge.
- `mode: "pdf_only"`: answers only from the PDF. If the PDF does not contain the answer the reply is a fixed
  "I couldn't find that in your PDF" with `used_pdf: false`; outside knowledge never leaks through. Needs a
  `material_id` (422 otherwise).

`used_pdf` says whether the answer drew on the PDF, `pages` which pages (only pages the model was actually
shown can be cited) and `label` is ready-to-show text ("Used the PDF for this answer (pages 2-3)"), null when the
PDF was not used. The same child-safety rules as lessons apply: the question is screened, the answer is checked for
the age, and a blocked one is a friendly 422. Each message is one generation attempt against
`GENERATION_DAILY_LIMIT`, so raise that limit for a chat-heavy demo. The PDF text, history and message are data,
never instructions to the model. The first chat with a book that was uploaded before embeddings were on embeds it.

## Chat about an image

`POST /api/chat/image` is multipart: `age`, `images` (1-4 files: JPG, PNG or WebP, up to 8 MiB each), an optional
`message` (up to 1000 characters; leave it out or blank and the picture is simply explained, otherwise the question is
what gets answered) and optional `history` (a JSON string of `{"role","content"}` turns, as for `/api/chat`). It returns
`{"answer", "used_image", "images", "label"}` (`label`: "Looked at your image for this answer"). The model is shown the
picture itself, with no text extraction and no search index: a labelled diagram, a graph or a handwritten problem
keeps what matters. A scanned book is different and still goes through `POST /api/materials` and `/api/chat`.

- **Nothing is stored.** Each picture is re-encoded (upright, flat, at most 1600 px; camera location and other
  metadata are dropped), used for that request and discarded. For a follow-up the client sends the picture again; the
  conversation is kept by the client in `history`. A PDF here is a 415 pointing to `/api/materials`.
- **Child safety, before anything is written:** the question is screened, then the picture(s) are checked for the
  learner's age with the checker model (nudity, graphic violence, drugs, self-harm, hate symbols, identity documents,
  bank cards or private addresses; ordinary schoolwork and everyday pictures pass). A blocked picture is the same
  friendly 422 as other guardrails and never repeats what was in it. If the checker cannot run, young learners get a
  502 and nothing is answered. The answer is then checked like every other generated text. A picture already approved
  for the same age policy is remembered by its fingerprint only (never the picture, bounded, in memory) so follow-ups
  about it are not checked again. Text written inside a picture is treated as data, never instructions.
- One generation attempt per message against `GENERATION_DAILY_LIMIT`; the request body limit for this route is 36 MiB.

## Saved chats (memory)

Chats can be remembered per player and continued later. This is one SQLite file (`MEMORY_DB_PATH`, default
`memory.db`, in memory for tests), separate from the records store, so it survives a restart of the demo server.
Every row carries the player id (`X-Player-ID`); one id can never read another's chats. A parent and a child just use
two ids. `PUT /api/memory/profile` stores a **role label** (`parent` or `learner`, the repo's word for the child) and
a display name for each id. The role is a label for the app and for later features, **not** a login or proof of who is
using the id, and there is no parent-to-child link.

**Using it.** `POST /api/memory/conversations` (optionally with a `material_id`) returns a chat id. Send it as
`conversation_id` to `POST /api/chat` or `POST /api/chat/image` (a form field there). The server then loads the chat's
own memory for the model (so `history` is ignored), saves the question and answer together, and returns
`conversation_id`, `title`, `subject` and an optional `notice`. A chat about a PDF carries on with that PDF without
`material_id`; if the PDF is gone (uploads are not part of this store) `notice` says so and `pdf_only` is a 422.
A picture is never saved: only the text, flagged `used_image` (a picture sent without a message is saved as the note
"(sent a picture)"). Without `conversation_id` chat stays stateless.

**Subjects.** After the first answer the model files the chat under one of Science, Math, History, Geography, Languages,
Computing, Arts or Other and names it after its topic (2 to 6 words). `PATCH` changes the title or subject; if
filing fails the chat goes under Other. The list endpoint returns the chats, newest first, and a count per subject for a
sidebar. Empty chats count under Other.

**Long chats.** The whole transcript is always kept and shown. What the model is given is bounded: once the unsummarised
part holds more than 6000 characters or 24 messages, all but the last 6 messages are folded into a rolling summary
(plain text, at most 1500 characters, written by the model, no personal details). The model then sees that summary
plus the recent turns. `GET .../conversations/{id}` returns `summary` and `summarized_messages`. Filing and folding
cost one small extra model call each, only when they happen, and never block an answer: if memory fails the answer is
still returned with a `notice`.

**Digest.** `GET /api/memory/digest` returns the player's subjects and, under each, the topics studied with
timestamps and message counts: no chat text, no model call. It exists so friend competitions can later match two
players on subjects they have both studied; reading another player's digest is not implemented and would be a
separate product decision.

**Limits and deletion.** 500 chats per player, 2000 messages per chat (a full chat says so in `notice`; creating
more is a 409). `DELETE /api/memory/conversations/{id}` forgets one chat; `DELETE /api/me/data` forgets all of a
player's chats and profile.

## Private room contract

Create: `{"display_name":"Alex","capacity":4}`. Join: `{"code":"INVITECODE","display_name":"Sam"}`.
Codes are eight uppercase characters; send the actual returned code. Default capacity 4, range 2–8.
Rooms expire after 24 hours. Snapshots contain ID, code, host, capacity, version, expiry, closed flag,
and member display names/readiness. They contain no auth tokens, emails, lesson text, or answer keys.

Commands: `{"expected_version":2,"action":"ready","ready":true}`; actions also include `leave` and
`close`. Stale versions return 409; reread before retrying. Only the host closes a lobby. When the host
leaves, the earliest remaining member becomes host. An empty lobby is deleted. A closed room rejects
new joins and readiness changes. Lobby readiness never automatically starts a game.

On Supabase, the migration installs private `room:<id>` receive authorization and optional
`room_changed` notifications. Subscribe privately and refetch the authorized snapshot on each event
or reconnect. Room membership is checked by the API on every snapshot. Notifications carry only the
room ID; do not rely on a transport subscription remaining authorized after someone leaves. Presence
publishing and active-game synchronization are not implemented by these lobby endpoints.

## Limits and errors

JSON bodies: 4 MiB. Multipart bodies: 61 MiB, including framing; each file 25 MiB, combined file bytes
60 MiB, 20 photos, 600 PDF pages, at most 100 vision pages per upload. Actual received bytes are bounded,
including chunked or falsely declared requests. The generation workflow can return fewer reviewed
questions than requested; inspect the returned count and dropped count.

403: forbidden host action. 404: absent or unowned record. 409: stale state,
review required, or conflicting replay. 413/415: size/type. 422: invalid request. 429: request, attempt,
or concurrency limit. 502: sanitized provider failure. 503: storage unavailable or generation disabled.

## Deletion and remaining decisions

Deleting material removes its derived lessons/revisions, quizzes, question sets, decks, completions,
and associated jobs. `/me/data` removes owned application records and lobby membership without
changing any external account. Minimal anti-abuse counters remain; no raw provider prompts,
images, or extracted pages are retained in those counters. Original uploads/images are not retained
by this API after extraction; extracted text persists until explicit deletion.

Automated content retention periods, guardian permissions, reward economies, public matchmaking,
rankings, gameplay, and the proposed age-safety output gate remain pending. No frontend files or
hosted database were changed by this API implementation.
