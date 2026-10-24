import pytest

import learning_service.flashcards as fc
from tests.learning.conftest import FAKE_LESSON
from tests.learning.test_quiz_graph import RecordingLLM
from tests.learning.test_visual_recall import ITEM


# ---------------------------------------------------------------- review loop
def test_know_retires_a_card():
    p = fc.review(fc.start(3), 0, "know")
    assert p.queue == [1, 2] and p.known == [0] and p.reviews == 1 and p.next == 1


def test_again_brings_the_card_back_after_a_gap():
    p = fc.review(fc.start(6), 0, "again")
    assert p.queue == [1, 2, 3, 0, 4, 5] and p.again == {0: 1}


def test_again_near_the_end_goes_last():
    p = fc.start(2)
    p = fc.review(p, 0, "again")
    assert p.queue == [1, 0]
    p = fc.review(p, 1, "know")
    p = fc.review(p, 0, "again")
    assert p.queue == [0] and p.again == {0: 2}


def test_deck_finishes_when_all_known():
    p = fc.start(2)
    for card in (0, 1):
        p = fc.review(p, card, "know")
    assert p.next is None and p.known == [0, 1]


def test_reviewing_a_card_not_in_the_queue_is_an_error():
    p = fc.review(fc.start(2), 0, "know")
    with pytest.raises(ValueError):
        fc.review(p, 0, "know")


def test_state_lists_hard_cards_first():
    deck = fc.StoredDeck(
        id="d",
        lesson_id="l",
        title="t",
        cards=[fc.Flashcard(kind="concept", front=f"f{i}", back="b") for i in range(3)],
        progress=fc.DeckProgress(queue=[0, 1, 2], again={2: 3, 0: 1}, reviews=4),
    )
    s = deck.state()
    assert s.hard == [2, 0] and s.total == 3 and s.known == 0 and s.next == 0


# ---------------------------------------------------------------- writing cards
def test_make_cards_dedupes_and_caps(monkeypatch):
    draft = fc.DeckDraft(
        cards=[
            fc.CardDraft(front="What is a router?", back="It directs traffic."),
            fc.CardDraft(front="what is a router?", back="dup"),
            fc.CardDraft(front="  ", back="empty front"),
            fc.CardDraft(front="A ____ checks messages.", back="Firewall"),
            fc.CardDraft(front="Why packets?", back="Small pieces travel well."),
        ]
    )
    llm = RecordingLLM(draft)
    monkeypatch.setattr(fc, "_llm", lambda: llm)
    cards = fc.make_cards(FAKE_LESSON, "networks", 9, n=2)
    assert [c.front for c in cards] == ["What is a router?", "A ____ checks messages."]
    assert all(c.kind == "concept" for c in cards)
    p = llm.prompts[0]
    assert "9-year-old" in p and "fill-in-the-blank" in p and "AWS reserves 5 IPs per subnet" in p


def test_diagram_card_hides_box_on_front_and_completes_it_on_back():
    card = fc.card_from_blank(ITEM)
    assert card.kind == "diagram" and "Glucose" in card.back
    assert "Glucose" not in card.front_visual.code and "Glucose" in card.back_visual.code
    deck = fc.StoredDeck(
        id="d", lesson_id="l", title="t", cards=[card], progress=fc.start(1)
    ).public()
    assert deck.cards[0].front_visual_html.startswith("<!doctype html>")
    assert "Glucose" not in deck.cards[0].front_visual_html
