# Interactive MCQ Web App — Design

Date: 2026-10-24
Status: Approved (pending spec review)

## Goal

Turn the `Quiz` produced by the LangGraph agent in `quiz_graph.py` into real,
clickable multiple-choice questions in a browser. A player types a prompt,
the agent generates the quiz, and the player answers by clicking.

This mirrors how Claude's harness renders interactive UI (AskUserQuestion,
Artifacts, MCP Apps): the model emits **structured data**, and a separate
**renderer** turns it into UI. The agent already emits structured data
(`Quiz` / `Question` Pydantic models); this project adds the renderer.

## Decisions

| Topic | Decision |
|---|---|
| Surface | Local web app: FastAPI + uvicorn, one static HTML/JS page, no build step |
| Feedback | Player chooses before generating: **Practice** (instant per question) or **Exam** (submit at end) |
| Question types | Web app requests only `multiple_choice` and `true_false`; CLI is unchanged and still supports `short_answer` |
| Answer secrecy | Answers and explanations stay on the server until the player commits |
| Unresolved questions | Questions still flagged by review (`quiz.unresolved`) are dropped from the web quiz, with a visible note |
| Storage | In-memory dict keyed by quiz id; lost on restart |

## Architecture

```
Browser (static/index.html + static/app.js)
   │ POST /api/quiz {prompt, num_questions, mode}
   │ GET  /api/quiz/{id}            → questions without answers
   │ POST /api/quiz/{id}/check      {index, choice}   (practice only)
   │ POST /api/quiz/{id}/submit     {answers: [choice,...]}  (exam only)
   ▼
app.py (FastAPI) — in-memory store {quiz_id: StoredQuiz(quiz, mode)}
   │
   ▼
quiz_graph.make_quiz(QuizRequest(..., allowed_types=["multiple_choice", "true_false"]))
```

### Files

- `quiz_graph.py` — small change (see below). Everything else untouched.
- `app.py` — new. FastAPI app, request/response models, in-memory store, routes, static mount.
- `static/index.html` — new. Markup and CSS for the three screens.
- `static/app.js` — new. Screen switching, API calls, practice/exam state.
- `tests/test_app.py` — new. API tests with `make_quiz` faked.
- `tests/test_quiz_graph.py` — new. `allowed_types` filtering test with a fake LLM.

Run: `python -m uv run uvicorn app:app --reload`, then open http://localhost:8000.

## Change to `quiz_graph.py`

- `QuizRequest` gains `allowed_types: list[QuestionType] | None = None`.
- In `parse_prompt`, after the LLM returns the spec, if `allowed_types` is set:
  `spec.question_types = [t for t in spec.question_types if t in allowed_types] or ["multiple_choice"]`.
  Filtering is done in code, not by prompt, so it cannot be ignored by the model.
- CLI behaviour is unchanged (it never sets `allowed_types`).

## API

All bodies are JSON. Choices are **option indices** (0-based), never option text.

### `POST /api/quiz`

Request: `{ "prompt": str (non-empty), "num_questions": int (1–15, default 5), "mode": "practice" | "exam" }`

Runs `make_quiz` synchronously (FastAPI runs a sync `def` route in its threadpool).
Drops unresolved questions, re-numbers the rest 0..n-1, stores the result under a
new `uuid4().hex` id.

Response `201`:
```json
{
  "id": "…",
  "title": "AWS VPC networking (medium)",
  "mode": "practice",
  "dropped": 1,
  "questions": [{ "index": 0, "type": "multiple_choice", "question": "…", "options": ["…","…","…","…"] }]
}
```

### `GET /api/quiz/{id}`

Same shape as the `POST` response. Never includes `answer` or `explanation`.

### `POST /api/quiz/{id}/check` (practice only)

Request: `{ "index": int, "choice": int }`
Response: `{ "correct": bool, "answer_index": int, "explanation": str }`

### `POST /api/quiz/{id}/submit` (exam only)

Request: `{ "answers": [int, ...] }` — one choice per question, in order.
Response:
```json
{
  "score": 5, "total": 6,
  "results": [{ "index": 0, "choice": 1, "correct": true, "answer_index": 1, "explanation": "…" }]
}
```

## UI flow

One page, three screens switched by JS.

1. **Start** — prompt textbox, question count (1–15, default 5), Practice/Exam
   toggle, Generate button. While generating, the button becomes a spinner:
   "Writing your quiz… (≈30 s)". Errors appear in red below the form; the
   typed prompt is kept.
2. **Quiz** — one question card at a time, "Q2 / 6" progress. Option buttons
   (4 for multiple choice, 2 for true/false). Keyboard: 1–4 or A–D selects.
   If `dropped > 0`, show "N question(s) removed after failing review".
   - *Practice:* click → `/check` → card locks; chosen option green or red,
     correct option green, explanation shown, Next button. Running score visible.
     No skipping: Next appears only after the question is answered, and the
     last card's Next goes to Results.
   - *Exam:* click highlights the choice and can be changed; Back/Next
     navigation; Submit on the last card, enabled when every question has a choice.
3. **Results** — score ("5 / 6 — 83%") and a review list: question, your answer,
   correct answer, explanation. "New quiz" returns to Start.

In practice mode the results screen is built from the `/check` responses
already received; in exam mode from the `/submit` response.

## Error handling

| Case | Response | UI |
|---|---|---|
| LLM/API failure (bad key, rate limit, bad model id) | 502 `{"detail": "<short message>"}` | Red message on Start, prompt kept |
| Every question unresolved | 502 "Couldn't produce reliable questions — try rephrasing" | Same |
| Empty prompt, count out of 1–15, bad mode | 422 (Pydantic) | Same |
| Unknown quiz id | 404 | "Quiz expired" + New quiz button |
| `/check` on an exam quiz, or `/submit` on a practice quiz | 403 | Not reachable from the UI; guards against direct API use |
| `index`/`choice` out of range, wrong `answers` length | 422 | Not reachable from the UI |

## Testing

- `pytest` added as a uv dev dependency (`python -m uv add --dev pytest`).
- `tests/test_app.py` uses FastAPI `TestClient` with `app.make_quiz` monkeypatched
  to return a fixed `Quiz` (one MC, one T/F, one unresolved). No network. Covers:
  - POST/GET responses contain no `answer` or `explanation`
  - unresolved question dropped, `dropped == 1`, indices re-numbered
  - `/check` correct and incorrect picks, returns explanation and answer index
  - `/check` on exam quiz → 403; `/submit` on practice quiz → 403
  - `/submit` scoring and per-question results
  - out-of-range choice → 422; unknown id → 404; `make_quiz` raising → 502
- `tests/test_quiz_graph.py` calls `parse_prompt` with `_llm` monkeypatched to a
  fake whose structured output returns a spec containing `short_answer`, and checks
  `allowed_types` removes it (and falls back to `multiple_choice` if nothing is left).
- Manual: one real end-to-end run in the browser with the OpenRouter model from `.env`,
  in both modes.

## Out of scope

Persistence across restarts, user accounts, short-answer grading, streaming
progress of graph nodes, deployment, MCP App packaging.