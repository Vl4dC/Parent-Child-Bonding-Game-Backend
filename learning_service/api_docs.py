"""
What makes /docs (Swagger UI) usable as the way to drive this backend: the walkthrough shown at the
top, tag descriptions, documented error responses, and hints on the id path parameters.
Shared by app.py and tournament.py so the two stay consistent.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path
from pydantic import BaseModel

UPLOAD, LEARN, QUIZ, CARDS, WEAK, TOURNAMENT = (
    "Your book",
    "Learn",
    "Quiz",
    "Flashcards",
    "Weak spots",
    "Tournament",
)

# Shown in this order in Swagger UI
TAGS = [
    {
        "name": UPLOAD,
        "description": "Optional: upload a digital book (PDF) or photos of pages, then study a part of it.",
    },
    {
        "name": LEARN,
        "description": "Explain a topic at the learner's age, and draw the diagrams it needs.",
    },
    {
        "name": QUIZ,
        "description": "Practice quiz built from the lesson. Answers are revealed one question at a time.",
    },
    {
        "name": CARDS,
        "description": "Flashcards built from the lesson, with a review loop kept on the server.",
    },
    {
        "name": WEAK,
        "description": "After a quiz: flashcards of the mistakes, and a new quiz on the same weak ideas.",
    },
    {
        "name": TOURNAMENT,
        "description": "Placeholder for the gamified mode: answer everything, submit once, get a score.",
    },
]

DESCRIPTION = """
Authenticated learn -> quiz -> flashcards API. Data persists in Supabase PostgreSQL; only the
explicit offline demo uses ephemeral storage. Authorize with a Bearer access token before trying data routes.
Model generation is opt-in. The full contract is documented in docs/API_PLATFORM.md.

## Try it in this order
0. *(optional)* **Your book** - `POST /api/materials` with a PDF, or several photos of pages. Copy the material `id`.
   Correct extracted text with `GET/PUT /api/materials/{id}/pages`, then confirm its revision with
   `POST /api/materials/{id}/confirm`. In step 1 add `material_id` and say which part to study (a topic, `chapter 3`, or `pages 40-55`);
   everything after that uses only what your pages say. `GET /api/materials/{material_id}/find` shows which
   pages a request would use, without spending anything.
1. **Learn** - `POST /api/lessons` with a topic and an age. Copy the lesson `id` from the response.
2. *(optional)* `POST /api/lessons/{lesson_id}/visuals` draws the diagrams the lesson planned (once per lesson).
   Send `ask` to get a picture of your own request.
3. **Quiz** - `POST /api/lessons/{lesson_id}/quiz`. Copy the quiz `id`. Questions arrive *without* answers.
4. `POST /api/quiz/{quiz_id}/check` once per question: `index` is the question number (from 0) and `choice`
   the option number (from 0). The answer is revealed after you commit, and **only your first answer to a
   question counts**.
5. **Weak spots** - once you have at least one wrong answer:
   `POST /api/quiz/{quiz_id}/followup` writes *different* questions on the same weak ideas, and
   `POST /api/quiz/{quiz_id}/flashcards` makes cards of your mistakes.
6. **Flashcards** - `POST /api/lessons/{lesson_id}/flashcards` (or the mistakes deck above), then
   `POST /api/flashcards/{deck_id}/review` with `know` or `again` for the `next` card.

## Good to know
- **Slow calls:** anything that writes with the model takes 10-40 seconds. Swagger shows a spinner, then the duration.
- **Pictures:** diagrams come back as complete HTML pages inside JSON (`html`, `visual_html`, `*_visual_html`),
  which Swagger can't render. Fetch **`/view`** endpoints with your Authorization header and render the response in a sandbox. Never put tokens in URLs.
- **Child safety:** the learners are school students (10-18). Everything the model writes is written to rules
  for the learner's age, then checked by a separate safety check before it is kept; what a learner types as a
  topic or picture request is checked first. Anything unsuitable for the age is a friendly `422` that never
  repeats it. A learner older than `AUDIENCE_MAX_AGE` (default 18) still gets the 18-and-under rules, because the
  backend can't verify age. If the check itself can't run, young learners get a `502` and nothing is made.
- **Errors:** `404` unknown or unowned id, `409` nothing to practise yet (no wrong answers),
  `413`/`415` upload too big or not a PDF/picture, `422` invalid input (or a request the book doesn't cover,
  or content held back as unsuitable for the learner's age), `502` the model call or the safety check failed
  (safe to try again; the message never contains the provider's error text).
- **Uploads:** PDFs are read locally for free; pictures (scans, photos) are read by the vision model,
  with provider usage charges. Limits: 25 MB per file, 600 pages, 100 picture pages, 20 photos at once.
- Long generation can use `/api/jobs` with an Idempotency-Key and a separately started worker.
- Scored solo sets require explicit lesson and question review; no multiplayer tournament is claimed.
"""

SWAGGER_UI = {
    "tryItOutEnabled": True,  # forms are editable straight away
    "displayRequestDuration": True,  # calls take a while; show how long
    "defaultModelsExpandDepth": -1,  # hide the long Schemas list
    "docExpansion": "list",
}


class Error(BaseModel):
    detail: str


_ERRORS = {
    404: "Unknown or unowned id. Ephemeral demo records also disappear on restart.",
    409: "Nothing to practise, review required, stale state, or conflicting replay.",
    422: (
        "Invalid input, or a request the book doesn't cover, or content the child-safety check held "
        "back because it isn't suitable for the learner's age. The response never repeats the unsafe text."
    ),
    413: "The upload is too big (25 MB per file, 600 PDF pages, 20 files).",
    415: "That file isn't a PDF or a picture (JPG, PNG or WebP) that can be read.",
    502: "The model call failed, or produced nothing usable. Safe to try again.",
}


def errors(*codes: int) -> dict:
    """`responses=` entries so Swagger lists these error codes and what they mean."""
    return {c: {"model": Error, "description": _ERRORS[c]} for c in codes}


LessonId = Annotated[str, Path(description="The lesson `id` returned by `POST /api/lessons`")]
QuizId = Annotated[
    str,
    Path(description="A quiz `id`, from `POST /api/lessons/{lesson_id}/quiz` or a follow-up quiz"),
]
DeckId = Annotated[str, Path(description="A deck `id`, from a flashcards endpoint")]
MaterialId = Annotated[str, Path(description="The material `id` returned by `POST /api/materials`")]
TournamentId = Annotated[str, Path(description="The `id` returned by `POST /api/tournament`")]
