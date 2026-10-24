# Learn → Quiz → Tournament - Design

Date: 2026-10-24
Supersedes the flow (not the API conventions) in `2026-10-24-interactive-mcq-web-design.md`:
exam mode moved out of the quiz and into a separate tournament module.

## Flow

1. **Learning**: learner gives a topic and their age; the agent explains the topic at that age's reading level.
2. **Quiz**: "Create quiz" on a lesson builds a practice quiz *from that lesson's text*, pitched at the same age.
   Instant feedback per question (`/check`), no skipping.
3. **Tournament**: separate mode, to be gamified. For now it is the old exam flow
   (answer all, submit once, score) so there is a working base to change.

## Modules

| File | Responsibility |
|---|---|
| `lesson.py` | `LessonRequest(topic, age)` → `Lesson(title, summary, sections, key_points)`. Age → reading-level guidance table (`READING_LEVELS`). One structured LLM call. CLI: `python -m uv run lesson.py "topic" --age 10` |
| `quiz_graph.py` | Quiz agent. `QuizRequest` gains `age` and `source` (lesson text); `_context()` adds them to the plan, generate and review prompts. Reviewer also flags questions the lesson doesn't cover or that don't suit the audience. |
| `store.py` | In-memory `LESSONS`, `QUIZZES`, `TOURNAMENTS`; public (answer-free) models; `grade()`; `quiz_from_lesson()` (runs the agent, drops unresolved questions). |
| `app.py` | Learning + practice-quiz routes; mounts the tournament router and the static page. |
| `tournament.py` | `APIRouter` at `/api/tournament`. Owns everything about tournament play; app.py only mounts it. |
| `static/` | Thin renderer (to be redesigned). |

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/lessons` | `{topic, age 5–99}` | `PublicLesson` (201) |
| GET | `/api/lessons/{id}` | | `PublicLesson` |
| POST | `/api/lessons/{id}/quiz` | `{num_questions 1–15}` | `PublicQuiz` (201) |
| GET | `/api/quiz/{id}` | | `PublicQuiz` |
| POST | `/api/quiz/{id}/check` | `{index, choice}` | `{correct, answer_index, explanation}` |
| POST | `/api/tournament` | `{lesson_id, num_questions}` | `PublicQuiz` (201) |
| GET | `/api/tournament/{id}` | | `PublicQuiz` |
| POST | `/api/tournament/{id}/submit` | `{answers: [choice…]}` | `{score, total, results[]}` |

Errors: 404 unknown id (incl. a practice id used on tournament routes and vice versa),
422 validation, 502 model failure or no question passed review.

## Extending the tournament

All changes stay inside `tournament.py` (plus new fields on its own models).
It can reuse `store.quiz_from_lesson`, `store.grade` and `store.TOURNAMENTS`.
Practice and tournament ids live in separate stores, so tournament rules can't leak into practice.

## Tests

`python -m uv run pytest` runs 234 tests with all model calls faked:
`test_lesson.py`, `test_quiz_graph.py`, `test_app.py`, `test_tournament.py`, `test_visuals.py`, `test_visual_recall.py`, `test_flashcards.py`, `test_learning_modes_api.py`, `test_weakness.py`, `test_followup_api.py`, `test_swagger_docs.py`, `test_serve.py`, `test_materials.py`, `test_materials_api.py` (fixtures in `conftest.py`).

## Visualization harness (`visuals/`)

Lets the learning step show diagrams, charts and interactive widgets, deciding by itself
when a picture actually helps. Modelled on Claude's own skills + artifacts harness:

**Visuals are planned while the lesson is written, not added afterwards.** `lesson.py` gives
each section an optional `visual` (kind, purpose, brief) and writes that section's text to go
with the picture. How visual a lesson is adapts automatically (`visual_policy`):

| Signal | Effect |
|---|---|
| Learner's wording (`VISUAL_WORDS`: "visually", "show me", "diagram", "draw"…) or `prefers_visuals` | Visual in every section except at most a pure intro |
| Age ≤ 11 | Visual in most sections (any process, structure, comparison, cause and effect) |
| Older learners | Visual only where the idea is a process, structure, quantity or cause and effect |
| Always | Kind matched to the idea (try-it → interactive, numbers → chart, steps/structure → diagram); max 5 |

`prefers_visuals` is set by the page once the learner has asked for a visual ("Show me…");
it is remembered in that browser, so later lessons lean visual without being asked.

| Piece | File | Role |
|---|---|---|
| Skills | `visuals/skills/{diagram,chart,interactive}.md` + `visuals/skillbook.py` | Frontmatter `description` = **when** to use it (shown to the lesson writer and the ask-planner); body = **how** to draw it. Edit these to tune behaviour, no code changes. |
| Planner | `lesson.py` (automatic) / `harness.plan_visuals` (`ask`) | Lesson-planned visuals go straight to drawing, with no extra call. A learner's own request is planned into exactly one visual. |
| Drawer | `harness.draw_visual` | Runs in parallel per plan item; loads the chosen skill's full body; writes `title/caption/alt/code`. |
| Validator | `render.validate` | Rejects broken or unsafe code with a reason; the drawer retries with that reason (max 3 attempts), then drops the visual. |
| Renderer | `render.document` | Standalone page for `<iframe sandbox="allow-scripts" srcdoc>`: CSP with no network except pinned Mermaid 11.4.1 / Vega 5.30 / Vega-Lite 5.21 / vega-embed 6.26 on jsDelivr; theme from CSS variables (light/dark); reports height to the parent; readable fallback if drawing fails. |

Kinds: **diagram** = Mermaid (processes, cycles, hierarchies), **chart** = Vega-Lite JSON with inline
data (real quantities), **interactive** = HTML+SVG+JS fragment (sliders, labelled illustrations).

API: `POST /api/lessons/{id}/visuals {ask?}` → `{visuals: [{id, kind, section_index, title, caption, alt, html}]}` (201);
visuals accumulate on the lesson and are returned by `GET /api/lessons/{id}`.

Without `ask` it draws the lesson's planned visuals, once per lesson (repeat calls return `[]`).
The page shows a "Drawing…" placeholder in each planned section immediately, swaps in the visuals
when they arrive, and calls again with `ask` for each "Show me…" request.

CLI preview: `python -m uv run python -m visuals.harness "photosynthesis" --age 10 [--ask "..."]` writes `visuals_preview.html`.

## Flashcards (`flashcards.py`)

A second practice mode next to the quiz, built from the same lesson.

- **Deck** = concept cards written from the lesson text for the learner's age (term, how/why,
  cause and effect, example, fill-in-the-blank) + diagram cards (see below). Exactly `num_cards`
  (3-20, default 8): concept cards are over-written and trimmed if a diagram card falls through.
- **Review loop** (server-side, so the tournament can reuse it): `know` retires a card; `again`
  brings it back after `REVIEW_GAP` (3) other cards. Deck is done when every card is known.
  State: `next`, `known`, `total`, `reviews`, `hard` (cards marked `again`, hardest first).

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/lessons/{id}/flashcards` | `{num_cards}` | `PublicDeck` (201): cards with `front/back` and optional `front_visual_html/back_visual_html`, plus `state` |
| GET | `/api/flashcards/{id}` | | `PublicDeck` |
| POST | `/api/flashcards/{id}/review` | `{card, result: "know" \| "again"}` | `DeckState`; 422 if the card isn't waiting |

## Diagram recall (`visual_recall.py`)

Quizzes and decks automatically reuse the lesson's **diagrams**: one box is hidden ("?") and the
learner recalls what belongs there. One box per diagram, at most 2 per quiz/deck, always leaving
at least one text item.

- **Model decides, code enforces:** the model picks which box is worth recalling and writes 3
  believable wrong labels; code parses the Mermaid flowchart, hides the box (dashed border), and
  validates the pick (unknown ids, distractors already on the diagram, fewer than 3 → dropped).
- **No leaks:** a box is only hideable if its label appears nowhere else in the diagram (edge
  labels, other boxes); titles/captions are replaced on the hidden version.
- **Quiz:** alternates *"What belongs in the dashed box?"* (multiple choice) and *"Is the dashed
  box labelled correctly?"* (true/false; the box shows the real label or a distractor). After
  answering, `/check` returns `answer_visual_html` = the completed diagram.
- **Flashcards:** front = diagram with the box hidden, back = the answer + the completed diagram.

## Mistakes -> flashcards, and the weakness follow-up quiz

The server records each learner's **first** answer to every quiz question (`StoredQuiz.attempts`),
so it knows what was missed. Two things are built from that, both offered on the results screen:

**1. Flashcards of the mistakes** - `POST /api/quiz/{id}/flashcards {which: "missed" | "all"}` (no model call).
A missed question becomes a recall card without options. True/false reads "True or false: ...";
diagram questions keep the hidden-box picture on the front and the completed one on the back;
each card notes what the learner chose. Same deck store and review loop as lesson flashcards.
409 if nothing was missed.

**2. Follow-up quiz on weak spots** - `POST /api/quiz/{id}/followup {num_questions?}` -> `PublicQuiz`
with `focus` (what is being practised) and `parent_id`. It is a normal quiz (records answers, can have
its own follow-up). 409 if nothing was missed. Built by `weakness.py`, a LangGraph:

    diagnose -> plan -> [make_variant x N, parallel] -> review -> assemble
                              ^                            |
                              +---- rewrite flagged -------+

| Step | Who | What |
|---|---|---|
| diagnose | model | Groups the misses into the ideas not yet understood and what each wrong pick suggests the learner believes. Code validates the grouping; any miss the model skipped gets its own weakness. |
| plan | code | Size = 2 questions per weakness (min 3, max 10). Round-robin over ideas, most-missed first; each question gets a different *angle* (new example / ask it backwards / spot the mistake / what if one thing changed / compare with the confusable idea), a type, and for true/false whether the statement is true. |
| make_variant | model + code | Writes one question. Code rejects it (writer retried with the reason, 3 tries) if: wrong type or truth value; correct answer repeats the old one; >=2 options reused; word overlap with the missed question > 0.5; a true/false statement that announces its own verdict ("This is a mistake", "True or false?"). A missed **diagram** question instead hides a *different box of the same diagram* (`visual_recall`, `exclude`). |
| review | model | Checks answer keys of the written questions; flagged ones are rewritten once, then dropped. |
| assemble | code | Drops near-duplicates, numbers the questions, lists the focus. |

Answer positions are shuffled in code. Found while building this: the model put the correct MCQ option
first 12 times out of 12, so any text quiz could be passed by always clicking A. `quiz_graph.generate_question`
now shuffles.

Not covered yet: tournament answers are not recorded (only the practice quiz feeds follow-ups).

## Swagger UI (`/docs`) as the way to drive the backend

FastAPI's Swagger UI is the hand-testing front end; `api_docs.py` holds what makes it usable:
a walkthrough ("Try it in this order") and tips on the page, five tags in teaching order
(Learn, Quiz, Flashcards, Weak spots, Tournament), documented error codes per operation
(404 unknown id, 409 nothing to practise, 502 model failure), id path-parameter hints,
and named request examples (`Body(openapi_examples=...)`) so "Try it out" is pre-filled.
"Try it out" is on by default and request durations are shown, since model calls take 10-40 s.

Diagrams are HTML pages inside JSON strings, which Swagger can't render, so three `/view` endpoints
return them as pages (execute one, open its Request URL in a tab):

| Path | Shows |
|---|---|
| `GET /api/lessons/{id}/visuals/{visual_id}/view` | a lesson visual |
| `GET /api/quiz/{id}/questions/{index}/view?show=question\|answer` | a diagram question's hidden-box picture; `answer` (completed diagram) only after that question was answered, else 409 |
| `GET /api/flashcards/{id}/cards/{index}/view?side=front\|back` | a diagram card |

They are served with `Content-Security-Policy: sandbox allow-scripts`, giving model-written code the same
locked-down, opaque origin it has inside the app's iframe.

`tests/test_swagger_docs.py` keeps the docs from rotting: every operation tagged and described, every request body
has examples, 404 on every id path, 502 on every model call, `/view` pages sandboxed and not leaking answers.

## Running it for other people on the network (`serve.py`)

    python -m uv run serve.py            # everyone on the network; prints the address to share
    python -m uv run serve.py --local    # this PC only
    python -m uv run serve.py --port 9000

It binds `0.0.0.0` and prints the address of the interface that reaches the internet first, e.g.
`http://10.247.10.19:8000` (page) and `/docs` (Swagger UI). Plain `uvicorn app:app` still listens on
this PC only. The page and Swagger use relative URLs, so they work from any address.

- **Firewall:** Windows allows inbound connections per program. Python already has TCP-inbound rules for the
  Public profile (created by Windows' first-run "Allow" prompt), which covers the Hackathon Wi-Fi. If another
  device can't connect, run in an **administrator** PowerShell:
  `New-NetFirewallRule -DisplayName "Quiz app 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private,Public -RemoteAddress LocalSubnet`
  (`LocalSubnet` keeps it to the same network; remove the rule afterwards with `Remove-NetFirewallRule -DisplayName "Quiz app 8000"`).
- **Wi-Fi client isolation:** some networks stop devices from reaching each other at all; nothing on this PC can fix that.
- **Devices need internet:** Swagger UI, Mermaid and Vega load from a CDN (jsDelivr).
- **No login:** anyone who can reach the address can use it, and each lesson, quiz, follow-up and deck spends the
  OpenRouter key in `.env`. Set a credit limit on that key in OpenRouter before sharing widely.
- **Shared state:** lessons and quizzes are in memory and keyed by unguessable ids, so users don't see each other's,
  but a restart wipes everyone's.

## Studying from your own book (`materials.py`)

A learner can upload a digital book (PDF) or photos of pages, name the part to study, and get the lesson, visuals,
quiz, flashcards, mistake cards and follow-ups built **only from those pages**. A typed topic with no upload still
works as before (the model's general knowledge).

| Method | Path | |
|---|---|---|
| POST | `/api/materials` | multipart: one PDF, or 1-20 photos (JPG/PNG/WebP) -> `MaterialInfo` (pages, how each was read, chapters found, warnings) |
| GET | `/api/materials/{id}` | the same details |
| GET | `/api/materials/{id}/find?topic=` | which pages a request would use, with a preview; local search, no model call |
| DELETE | `/api/materials/{id}` | forget it now (lessons already made from it keep working) |
| POST | `/api/lessons` | now takes `material_id`; `topic` says which part. Response gains `source` (title, pages used) |

- **Reading:** text PDFs are read locally with PDFium (free). A page that has under 40 characters of text *and contains
  a picture* (scans, photos) is rendered and transcribed by the vision model (the main model reads images), 8 at a time,
  including any diagram labels. Short text pages without a picture are left alone.
- **Finding the part:** `pages 40-55` -> those pages; `chapter 3` -> that chapter from the headings found (contents pages and
  running headers ignored); anything else -> BM25 word search (light plural stemming, works for non-Latin scripts) over the
  pages, plus neighbouring pages that mention the topic or sit in the same chapter. Contents and index pages are never
  search results. A request the book doesn't contain is refused (422) before any model call.
- **Strict grounding:** the writer may use only the pages (`GROUNDING_RULES`), must say `covered: false` when the pages
  don't teach the topic (-> 422 with what's missing), and treats the pages as data, not instructions.
- **Limits:** 25 MB per file, 60 MB per upload, 600 PDF pages, 100 picture pages read per upload, 20 photos, 30 uploads kept
  (oldest dropped). Only text is kept, in memory. No page images are stored.
- **Not covered:** EPUB, rebuilding book figures as hide-a-box diagrams, password-protected PDFs, HEIC photos.
- **Open network:** anyone who can reach the server can upload, and picture pages spend the model key (a fraction of a cent per page).
  The size check on the request header is not hardened against a client that lies.

Separately, `VISUAL_MODEL` in `.env` can point the drawing of visuals at another model; see
`2026-10-24-visual-model-bakeoff.md` for why it is unset.
