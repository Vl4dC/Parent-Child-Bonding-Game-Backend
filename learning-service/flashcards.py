"""
Flashcards: a second way to practise a lesson, alongside the quiz.

Deck = concept cards written from the lesson (for the learner's age) + diagram cards
(the lesson's diagram with one box hidden; see visual_recall.py).

Review is a simple in-session Leitner loop, kept on the server so later modes
(e.g. the tournament) can read it:
  "know"  -> the card is retired
  "again" -> the card comes back after REVIEW_GAP other cards
The deck is done when every card has been marked "know".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from lesson import Lesson, reading_level
from quiz_graph import _llm
from safety import guidance
from visual_recall import BLANK, BlankItem, IllustratedQuestion, hidden
from visuals.models import Visual
from visuals.render import document

REVIEW_GAP = 3
CardKind = Literal["concept", "diagram"]
ReviewResult = Literal["know", "again"]


class Flashcard(BaseModel):
    kind: CardKind
    front: str
    back: str
    front_visual: Visual | None = None
    back_visual: Visual | None = None


# --------------------------------------------------------------------------
# Writing cards
# --------------------------------------------------------------------------
class CardDraft(BaseModel):
    front: str = Field(..., description="A question or prompt (not a single bare word), at most ~20 words")
    back: str = Field(..., description="The answer in 1-2 short sentences")


class DeckDraft(BaseModel):
    cards: list[CardDraft]


def cards_prompt(lesson: Lesson, topic: str, age: int, n: int, note: str = "") -> str:
    return (
        f'Write {n} flashcards for a {age}-year-old who just studied "{topic}" with the lesson below.\n'
        f"Write for them: {reading_level(age)}\n"
        "Cover every key point and the important words. Mix card styles: a term and what it means, "
        "a how/why question, cause and effect, an example to recognise, and fill-in-the-blank sentences "
        "(use ____ for the blank). Each card tests one idea; no two cards test the same thing. "
        "Only use facts the lesson teaches.\n\n"
        f"<lesson>\n{lesson.as_text()}\n</lesson>"
        + guidance(age, note)
    )


def make_cards(lesson: Lesson, topic: str, age: int, n: int, note: str = "") -> list[Flashcard]:
    draft = _llm().with_structured_output(DeckDraft).invoke(cards_prompt(lesson, topic, age, n, note))
    cards, fronts = [], set()
    for c in draft.cards:
        front, back = c.front.strip(), c.back.strip()
        if front and back and front.casefold() not in fronts:
            fronts.add(front.casefold())
            cards.append(Flashcard(kind="concept", front=front, back=back))
    return cards[:n]


def card_from_question(q: IllustratedQuestion, picked: str | None = None) -> Flashcard:
    """A quiz question as a recall card: the options are dropped (so it tests memory) and, for
    diagram questions, the hidden-box picture goes on the front and the completed one on the back."""
    if q.visual:  # diagram question: the question text already says what to recall
        front = q.question
        back = q.explanation if q.type == "true_false" else f"{q.answer} - {q.explanation}"
    elif q.type == "true_false":
        front = f"True or false: {q.question}"
        back = f"{q.answer}. {q.explanation}"
    else:
        front = q.question
        back = f"{q.answer} - {q.explanation}"
    if picked:
        back += f" (You chose “{picked}”.)"
    return Flashcard(kind="diagram" if q.visual else "concept", front=front, back=back,
                     front_visual=q.visual, back_visual=q.answer_visual)


def card_from_blank(item: BlankItem) -> Flashcard:
    return Flashcard(
        kind="diagram",
        front=f"What goes in the dashed box marked “{BLANK}”?",
        back=f"{item.answer} - {item.explanation}",
        front_visual=hidden(item),
        back_visual=item.visual,
    )


# --------------------------------------------------------------------------
# Review loop
# --------------------------------------------------------------------------
class DeckProgress(BaseModel):
    queue: list[int]                 # cards still to learn; queue[0] is next
    known: list[int] = []            # retired cards, in the order they were learned
    again: dict[int, int] = {}       # card -> times marked "again"
    reviews: int = 0

    @property
    def next(self) -> int | None:
        return self.queue[0] if self.queue else None


def start(n_cards: int) -> DeckProgress:
    return DeckProgress(queue=list(range(n_cards)))


def review(progress: DeckProgress, card: int, result: ReviewResult) -> DeckProgress:
    """Record one review. Raises ValueError if the card isn't waiting to be reviewed."""
    if card not in progress.queue:
        raise ValueError(f"card {card} is not in the review queue")
    queue = [c for c in progress.queue if c != card]
    known, again = list(progress.known), dict(progress.again)
    if result == "know":
        known.append(card)
    else:
        again[card] = again.get(card, 0) + 1
        queue.insert(min(REVIEW_GAP, len(queue)), card)
    return DeckProgress(queue=queue, known=known, again=again, reviews=progress.reviews + 1)


# --------------------------------------------------------------------------
# API views
# --------------------------------------------------------------------------
class PublicCard(BaseModel):
    index: int
    kind: CardKind
    front: str
    back: str
    front_visual_html: str | None = None
    back_visual_html: str | None = None


class DeckState(BaseModel):
    next: int | None        # card to show next; None = deck finished
    known: int
    total: int
    reviews: int
    hard: list[int]         # cards marked "again" at least once, hardest first


class PublicDeck(BaseModel):
    id: str
    lesson_id: str
    title: str
    cards: list[PublicCard]
    state: DeckState


class StoredDeck(BaseModel):
    id: str
    lesson_id: str
    title: str
    cards: list[Flashcard]
    progress: DeckProgress

    def state(self) -> DeckState:
        p = self.progress
        hard = sorted(p.again, key=lambda c: (-p.again[c], c))
        return DeckState(next=p.next, known=len(p.known), total=len(self.cards), reviews=p.reviews, hard=hard)

    def public(self) -> PublicDeck:
        def html(v: Visual | None, side: str, i: int) -> str | None:
            return document(v, f"{self.id}-{i}-{side}") if v else None

        return PublicDeck(
            id=self.id, lesson_id=self.lesson_id, title=self.title, state=self.state(),
            cards=[PublicCard(index=i, kind=c.kind, front=c.front, back=c.back,
                              front_visual_html=html(c.front_visual, "front", i),
                              back_visual_html=html(c.back_visual, "back", i))
                   for i, c in enumerate(self.cards)],
        )
