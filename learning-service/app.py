"""
Web app for the learning tool. Interactive API docs (Swagger UI) at /docs; the page at /.

  Your book   POST /api/materials (PDF or photos)           -> the pages, read (materials.py)
  Learn       POST /api/lessons {topic, age, material_id?}  -> lesson explained for that age; with a
                                                               material_id, built only from that book's pages
              POST /api/lessons/{id}/visuals {ask?}         -> visualization harness (visuals/) draws the
                                                               visuals the lesson planned, or answers `ask`
  Quiz        POST /api/lessons/{id}/quiz                   -> practice quiz built from the lesson
              POST /api/quiz/{id}/check {index, choice}     -> instant feedback, one question at a time
  Flashcards  POST /api/lessons/{id}/flashcards             -> deck built from the lesson
              POST /api/flashcards/{id}/review {card, result} -> know / again; returns the next card
  Weak spots  POST /api/quiz/{id}/followup                  -> new questions on what was missed (weakness.py)
              POST /api/quiz/{id}/flashcards                -> flashcards of the mistakes
  Tournament  /api/tournament/...                           -> separate gamified mode (tournament.py)

Quizzes and decks mix in diagram items (a box hidden in a lesson diagram) when the lesson has diagrams.
Answers stay on the server (store.py); the browser only gets them after committing to a choice.
The /view endpoints return a diagram as a standalone web page, so it can be looked at from Swagger UI.

Run:  python -m uv run serve.py          -> open to everyone on the network; prints the address to share
      python -m uv run serve.py --local  -> this PC only          (docs: /docs)
      (no --reload: on this Windows/OneDrive setup it leaves stale workers running old code)
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

import flashcards
import materials
import tournament
from api_docs import (CARDS, DESCRIPTION, LEARN, QUIZ, SWAGGER_UI, TAGS, UPLOAD, WEAK, DeckId, LessonId,
                      MaterialId, QuizId, errors)
from lesson import (Lesson, LessonRequest, LessonSource, NotCovered, make_lesson, make_lesson_from_pages)
from materials import MaterialInfo, NotInMaterial, select_pages
from store import (
    DECKS,
    LESSONS,
    MATERIALS,
    QUIZZES,
    CheckOut,
    PublicQuiz,
    PublicVisual,
    StoredLesson,
    StoredVisual,
    answer_question,
    deck_from_lesson,
    deck_from_quiz,
    followup_quiz,
    get_or_404,
    log,
    new_id,
    question_at,
    quiz_from_lesson,
    remember_material,
)
from visuals.harness import make_visuals
from visuals.models import VisualRequest
from visuals.render import document

STATIC = Path(__file__).parent / "static"

# Model-written diagram code runs in a page of its own: `sandbox` gives it the same locked-down,
# opaque origin the <iframe sandbox="allow-scripts"> gives it inside the app.
SANDBOXED = {"Content-Security-Policy": "sandbox allow-scripts"}


class PublicLesson(Lesson):
    id: str
    topic: str
    age: int
    source: LessonSource | None = Field(None, description="Set when the lesson was built from an upload: which pages")
    visuals: list[PublicVisual] = []


class FoundPages(BaseModel):
    pages: list[int]
    how: Literal["pages", "chapter", "search"]
    label: str
    characters: int = Field(..., description="Size of the text that would go into the lesson")
    truncated: bool
    preview: str = Field(..., description="The start of that text")


class VisualsIn(BaseModel):
    ask: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = Field(
        None, description="A picture you want, in your own words. Leave out to draw what the lesson planned.")


class VisualsOut(BaseModel):
    visuals: list[PublicVisual]  # only the ones made by this call; may be empty


class CreateQuiz(BaseModel):
    num_questions: int = Field(5, ge=1, le=15)


class CheckIn(BaseModel):
    index: int = Field(..., description="Question number, from 0")
    choice: int = Field(..., description="Option number, from 0 (True = 0, False = 1 on true/false questions)")


class CreateDeck(BaseModel):
    num_cards: int = Field(8, ge=3, le=20)


class FollowupIn(BaseModel):
    num_questions: int | None = Field(None, ge=1, le=10, description="Leave out to decide from the weak spots")


class QuizDeckIn(BaseModel):
    which: Literal["missed", "all"] = Field("missed", description="`missed`: only wrong answers. `all`: the whole quiz.")


class ReviewIn(BaseModel):
    card: int = Field(..., description="Card number: the `next` value of the deck state")
    result: flashcards.ReviewResult


# Pre-filled request bodies for Swagger's "Try it out"
LessonBody = Annotated[LessonRequest, Body(openapi_examples={
    "young": {"summary": "Young learner", "value": {"topic": "the water cycle", "age": 8}},
    "pictures": {"summary": "Asks for pictures in the topic",
                 "value": {"topic": "how the internet works, explain it visually", "age": 14}},
    "visual_learner": {"summary": "Returning learner who likes visuals",
                       "value": {"topic": "photosynthesis", "age": 12, "prefers_visuals": True}},
    "from_book": {"summary": "From my uploaded book (paste the material id)",
                  "value": {"topic": "photosynthesis", "age": 14, "material_id": "<id from POST /api/materials>"}},
})]
VisualsBody = Annotated[VisualsIn, Body(openapi_examples={
    "planned": {"summary": "Draw what the lesson planned", "value": {}},
    "ask": {"summary": "Ask for a picture", "value": {"ask": "show me what happens inside the leaf"}},
})]
QuizBody = Annotated[CreateQuiz, Body(openapi_examples={"five": {"summary": "Five questions", "value": {"num_questions": 5}}})]
CheckBody = Annotated[CheckIn, Body(openapi_examples={
    "first_option": {"summary": "Question 0, first option", "value": {"index": 0, "choice": 0}},
    "second_question": {"summary": "Question 1, second option", "value": {"index": 1, "choice": 1}},
})]
FollowupBody = Annotated[FollowupIn, Body(openapi_examples={
    "auto": {"summary": "Let it decide how many", "value": {}},
    "five": {"summary": "Five questions", "value": {"num_questions": 5}},
})]
QuizDeckBody = Annotated[QuizDeckIn, Body(openapi_examples={
    "missed": {"summary": "Only my mistakes", "value": {"which": "missed"}},
    "all": {"summary": "The whole quiz", "value": {"which": "all"}},
})]
DeckBody = Annotated[CreateDeck, Body(openapi_examples={"eight": {"summary": "Eight cards", "value": {"num_cards": 8}}})]
ReviewBody = Annotated[ReviewIn, Body(openapi_examples={
    "know": {"summary": "Got it", "value": {"card": 0, "result": "know"}},
    "again": {"summary": "Still learning", "value": {"card": 0, "result": "again"}},
})]


def _public_lesson(stored: StoredLesson) -> PublicLesson:
    return PublicLesson(id=stored.id, topic=stored.request.topic, age=stored.request.age, source=stored.source,
                        visuals=[PublicVisual.of(v) for v in stored.visuals], **stored.lesson.model_dump())


app = FastAPI(title="Learning Quiz Agent", description=DESCRIPTION, version="0.1.0", openapi_tags=TAGS,
              swagger_ui_parameters=SWAGGER_UI)
app.include_router(tournament.router)


# --------------------------------------------------------------------------
# Your book (upload)
# --------------------------------------------------------------------------
def limit_upload_size(content_length: Annotated[int | None, Header()] = None) -> None:
    """Turn away an obviously oversized upload before reading it. (Not hardened against a client that lies.)"""
    if content_length is not None and content_length > materials.MAX_TOTAL_BYTES + 2**20:
        raise HTTPException(413, f"That upload is too big: the limit is {materials.MAX_TOTAL_BYTES // 2**20} MB in total")


@app.post("/api/materials", status_code=201, tags=[UPLOAD], summary="Upload a book or photos of pages",
          responses=errors(413, 415, 502), dependencies=[Depends(limit_upload_size)])
def upload_material(files: Annotated[list[UploadFile], File(
        description="One PDF (a digital book), or one or more photos of pages (JPG, PNG, WebP)")]) -> MaterialInfo:
    """Read an upload so lessons can be built from it.

    **Text PDFs** are read locally and cost nothing. **Pages that are pictures** (scanned PDFs, photos) are read by
    the vision model: a fraction of a cent per page, in parallel, so a scanned book takes up to a minute.
    Limits: 25 MB per file (60 MB together), 600 PDF pages, 100 picture pages read per upload, 20 photos.

    The response says how many pages were read each way, lists the chapter headings found (ask for one with
    `chapter 3`) and warns about pages that couldn't be read. Only the text is kept, in memory: nothing is written
    to disk and everything is gone when the server restarts. Anyone who can reach this server can upload."""
    named = [(f.filename or "upload", f.file.read(materials.MAX_FILE_BYTES + 1)) for f in files]
    try:
        material = materials.read_files(named, materials.model_vision_reader())
    except materials.BadUpload as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return remember_material(material).info()


@app.get("/api/materials/{material_id}", tags=[UPLOAD], summary="Get an upload's details", responses=errors(404))
def get_material(material_id: MaterialId) -> MaterialInfo:
    """Page counts, how each page was read, the chapter headings found, and any warnings."""
    return get_or_404(MATERIALS, material_id, "Material").info()


@app.get("/api/materials/{material_id}/find", tags=[UPLOAD], summary="Which pages would a request use?",
         responses=errors(404))
def find_pages(material_id: MaterialId,
               topic: Annotated[str, Query(min_length=1, max_length=200, description=(
                   "What you would type as the lesson topic: a topic, `chapter 3` or `pages 40-55`"))]) -> FoundPages:
    """Preview which pages a lesson request would be built from. Searches locally: no model call, no cost.
    422 if the book doesn't contain it."""
    material = get_or_404(MATERIALS, material_id, "Material")
    try:
        picked = select_pages(material, topic)
    except NotInMaterial as exc:
        raise HTTPException(422, str(exc)) from exc
    return FoundPages(pages=picked.pages, how=picked.how, label=picked.label, characters=len(picked.text),
                      truncated=picked.truncated, preview=picked.text[:600])


@app.delete("/api/materials/{material_id}", status_code=204, tags=[UPLOAD], summary="Remove an upload",
            responses=errors(404))
def delete_material(material_id: MaterialId) -> None:
    """Forget the uploaded pages now. Lessons already made from it keep working (they hold their own text)."""
    get_or_404(MATERIALS, material_id, "Material")
    del MATERIALS[material_id]


# --------------------------------------------------------------------------
# Learn
# --------------------------------------------------------------------------
@app.post("/api/lessons", status_code=201, tags=[LEARN], summary="Explain a topic for an age",
          responses=errors(502))
def create_lesson(body: LessonBody) -> PublicLesson:
    """Write a lesson on `topic` at the reading level of a learner `age` years old: summary, sections that
    build on each other, key points.

    Visuals are planned *with* the lesson: each section that needs a picture carries a `visual` plan.
    Words like "visually" or "show me" in `topic`, or `prefers_visuals`, ask for a picture in nearly every
    section; younger learners get more pictures. The pictures themselves are drawn by
    `POST /api/lessons/{lesson_id}/visuals`.

    **From your own book:** with `material_id` (from `POST /api/materials`), `topic` says which part to study:
    a topic (`photosynthesis`), a chapter (`chapter 3`) or pages (`pages 40-55`). The lesson, and everything
    built from it (visuals, quiz, flashcards, follow-ups), then uses **only what those pages say**. If the
    pages don't cover the request you get a 422 saying so, instead of an invented lesson; the response's
    `source` lists the pages used. Takes about 20-30 s."""
    material = get_or_404(MATERIALS, body.material_id, "Material") if body.material_id else None
    source = None
    # sync def: FastAPI runs it in a worker thread, so the slow model call doesn't block the server
    try:
        if material is None:
            lesson = make_lesson(body)
        else:
            picked = select_pages(material, body.topic)
            lesson = make_lesson_from_pages(body, picked.text)
            source = LessonSource(material_id=material.id, title=material.name, pages=picked.pages,
                                  label=picked.label, truncated=picked.truncated)
    except (NotInMaterial, NotCovered) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("lesson generation failed")
        raise HTTPException(502, f"Lesson generation failed: {str(exc)[:300]}") from exc
    stored = StoredLesson(id=new_id(), request=body, lesson=lesson, source=source)
    LESSONS[stored.id] = stored
    return _public_lesson(stored)


@app.get("/api/lessons/{lesson_id}", tags=[LEARN], summary="Get a lesson", responses=errors(404))
def get_lesson(lesson_id: LessonId) -> PublicLesson:
    """The lesson with every visual drawn for it so far."""
    return _public_lesson(get_or_404(LESSONS, lesson_id, "Lesson"))


@app.post("/api/lessons/{lesson_id}/visuals", status_code=201, tags=[LEARN], summary="Draw the lesson's visuals",
          responses=errors(404, 502))
def create_visuals(lesson_id: LessonId, body: VisualsBody) -> VisualsOut:
    """Draw diagrams, charts and interactive widgets for the lesson.

    Without `ask`: draws the visuals the lesson planned, **once per lesson** (repeat calls return an empty
    list). With `ask`: draws one visual answering that request, every time.

    Each visual's `html` is a complete web page; see it rendered with `GET /api/lessons/{lesson_id}/visuals/{visual_id}/view`.
    Takes about 15-30 s."""
    stored = get_or_404(LESSONS, lesson_id, "Lesson")
    ask = body.ask or None
    if ask is None:
        if stored.auto_drawn:
            return VisualsOut(visuals=[])
        stored.auto_drawn = True  # set before drawing so a double call can't draw twice
    req = VisualRequest(topic=stored.request.topic, age=stored.request.age, lesson=stored.lesson, ask=ask)
    try:
        visuals = make_visuals(req)
    except Exception as exc:
        if ask is None:
            stored.auto_drawn = False
        log.exception("visual generation failed")
        raise HTTPException(502, f"Visual generation failed: {str(exc)[:300]}") from exc
    new = [StoredVisual(id=new_id(), **v.model_dump()) for v in visuals]
    stored.visuals.extend(new)
    return VisualsOut(visuals=[PublicVisual.of(v) for v in new])


@app.get("/api/lessons/{lesson_id}/visuals/{visual_id}/view", response_class=HTMLResponse, tags=[LEARN],
         summary="View a lesson visual as a web page", responses=errors(404))
def view_visual(lesson_id: LessonId, visual_id: str) -> HTMLResponse:
    """The visual as a standalone page. In Swagger, execute this and open the *Request URL* in a new tab."""
    stored = get_or_404(LESSONS, lesson_id, "Lesson")
    visual = next((v for v in stored.visuals if v.id == visual_id), None)
    if visual is None:
        raise HTTPException(404, "Visual not found in this lesson (ids are in the lesson's `visuals[].id`)")
    return HTMLResponse(document(visual, visual.id), headers=SANDBOXED)


# --------------------------------------------------------------------------
# Quiz (practice: instant feedback per question)
# --------------------------------------------------------------------------
@app.post("/api/lessons/{lesson_id}/quiz", status_code=201, tags=[QUIZ], summary="Create a quiz from a lesson",
          responses=errors(404, 502))
def create_quiz(lesson_id: LessonId, body: QuizBody) -> PublicQuiz:
    """Build a practice quiz from the lesson text, pitched at the learner's age.

    When the lesson has diagrams, some questions hide one box in a diagram and ask what belongs there
    (`visual_html`; see it with `GET /api/quiz/{quiz_id}/questions/{index}/view`).

    Questions come **without answers**; send each one to `POST /api/quiz/{quiz_id}/check`. `dropped` counts
    questions removed because they failed the review step. Takes about 20-40 s."""
    stored = quiz_from_lesson(get_or_404(LESSONS, lesson_id, "Lesson"), body.num_questions)
    QUIZZES[stored.id] = stored
    return stored.public()


@app.get("/api/quiz/{quiz_id}", tags=[QUIZ], summary="Get a quiz", responses=errors(404))
def get_quiz(quiz_id: QuizId) -> PublicQuiz:
    """The quiz as the learner sees it: questions and options, never answers."""
    return get_or_404(QUIZZES, quiz_id, "Quiz").public()


@app.post("/api/quiz/{quiz_id}/check", tags=[QUIZ], summary="Answer a question", responses=errors(404))
def check_answer(quiz_id: QuizId, body: CheckBody) -> CheckOut:
    """Grade one answer and reveal the correct option and the explanation.

    `choice` is the option number from 0 (true/false questions have two options: True = 0, False = 1).
    Only the learner's **first** answer to each question is recorded; that is what the follow-up quiz and the
    mistakes flashcards are built from. Later calls for the same question still return the grade.
    Diagram questions also return `answer_visual_html`, the completed diagram.

    422 if `index` or `choice` is out of range."""
    return answer_question(get_or_404(QUIZZES, quiz_id, "Quiz"), body.index, body.choice)


@app.get("/api/quiz/{quiz_id}/questions/{index}/view", response_class=HTMLResponse, tags=[QUIZ],
         summary="View a diagram question as a web page", responses=errors(404, 409))
def view_question(quiz_id: QuizId, index: int,
                  show: Annotated[Literal["question", "answer"], Query(
                      description="`question`: the diagram with its hidden box. `answer`: the completed diagram, "
                                  "only once that question has been answered.")] = "question") -> HTMLResponse:
    """The diagram of a diagram question as a standalone page (404 if the question has none)."""
    quiz = get_or_404(QUIZZES, quiz_id, "Quiz")
    question = question_at(quiz, index)
    if question.visual is None:
        raise HTTPException(404, "This question has no diagram")
    if show == "answer":
        if index not in quiz.attempts:
            raise HTTPException(409, "Answer the question first; the completed diagram would give it away")
        return HTMLResponse(document(question.answer_visual, f"{quiz.id}-a{index}"), headers=SANDBOXED)
    return HTMLResponse(document(question.visual, f"{quiz.id}-q{index}"), headers=SANDBOXED)


# --------------------------------------------------------------------------
# Weak spots (built from the answers recorded by /check)
# --------------------------------------------------------------------------
@app.post("/api/quiz/{quiz_id}/followup", status_code=201, tags=[WEAK], summary="New quiz on my weak spots",
          responses=errors(404, 409, 502))
def create_followup(quiz_id: QuizId, body: FollowupBody) -> PublicQuiz:
    """Write a new quiz on what this learner got wrong: **different** questions on the same weak ideas, so it
    can't be passed by remembering the old answers (new situations, other wording, other options; a missed
    diagram question gets another box of the same diagram hidden).

    Needs at least one wrong answer so far (409 otherwise). `focus` lists the ideas being practised and
    `parent_id` is the quiz it came from. It is a normal quiz: answer it with `/check`; it records answers too,
    so it can have its own follow-up. Without `num_questions`: 2 per weak idea, between 3 and 10.
    Takes about 20-40 s."""
    quiz = get_or_404(QUIZZES, quiz_id, "Quiz")
    stored = followup_quiz(quiz, get_or_404(LESSONS, quiz.lesson_id, "Lesson"), body.num_questions)
    QUIZZES[stored.id] = stored  # a follow-up is a quiz like any other: it records answers and can have its own follow-up
    return stored.public()


@app.post("/api/quiz/{quiz_id}/flashcards", status_code=201, tags=[WEAK], summary="Flashcards of my mistakes",
          responses=errors(404, 409))
def create_quiz_deck(quiz_id: QuizId, body: QuizDeckBody) -> flashcards.PublicDeck:
    """Flashcards of the questions answered wrongly (`which: missed`) or of the whole quiz (`all`).
    No model call: instant. Review them with `POST /api/flashcards/{deck_id}/review`.

    Cards ask the question without options; the back has the answer, the explanation and what the learner
    chose. Diagram questions keep their picture. 409 if nothing was answered wrongly."""
    stored = deck_from_quiz(get_or_404(QUIZZES, quiz_id, "Quiz"), body.which)
    DECKS[stored.id] = stored
    return stored.public()


# --------------------------------------------------------------------------
# Flashcards
# --------------------------------------------------------------------------
@app.post("/api/lessons/{lesson_id}/flashcards", status_code=201, tags=[CARDS], summary="Create flashcards from a lesson",
          responses=errors(404, 502))
def create_deck(lesson_id: LessonId, body: DeckBody) -> flashcards.PublicDeck:
    """Write flashcards from the lesson at the learner's age: terms, how/why, cause and effect, fill-in-the-blank,
    plus up to 2 diagram cards (a hidden box on the front, the completed diagram on the back) when the lesson
    has diagrams. Exactly `num_cards` cards. Takes about 15-25 s."""
    stored = deck_from_lesson(get_or_404(LESSONS, lesson_id, "Lesson"), body.num_cards)
    DECKS[stored.id] = stored
    return stored.public()


@app.get("/api/flashcards/{deck_id}", tags=[CARDS], summary="Get a deck and its progress", responses=errors(404))
def get_deck(deck_id: DeckId) -> flashcards.PublicDeck:
    """All cards plus `state`: which card is `next`, how many are `known`, and which were `hard`."""
    return get_or_404(DECKS, deck_id, "Deck").public()


@app.post("/api/flashcards/{deck_id}/review", tags=[CARDS], summary="Mark a card known or still learning",
          responses=errors(404))
def review_card(deck_id: DeckId, body: ReviewBody) -> flashcards.DeckState:
    """`know` retires the card; `again` brings it back after 3 other cards. The deck is finished when `next` is
    null. 422 if that card isn't waiting to be reviewed (for example it is already known)."""
    deck = get_or_404(DECKS, deck_id, "Deck")
    try:
        deck.progress = flashcards.review(deck.progress, body.card, body.result)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return deck.state()


@app.get("/api/flashcards/{deck_id}/cards/{index}/view", response_class=HTMLResponse, tags=[CARDS],
         summary="View a diagram card as a web page", responses=errors(404))
def view_card(deck_id: DeckId, index: int,
              side: Annotated[Literal["front", "back"], Query(description="Which side of the card")] = "front"
              ) -> HTMLResponse:
    """A diagram card's picture as a standalone page (404 if that side has none)."""
    deck = get_or_404(DECKS, deck_id, "Deck")
    if not 0 <= index < len(deck.cards):
        raise HTTPException(404, f"Card {index} not found; this deck has {len(deck.cards)} cards (0-{len(deck.cards) - 1})")
    card = deck.cards[index]
    visual = card.front_visual if side == "front" else card.back_visual
    if visual is None:
        raise HTTPException(404, f"This card has no diagram on its {side}")
    return HTMLResponse(document(visual, f"{deck.id}-{index}-{side}"), headers=SANDBOXED)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
