"""A fake embedding provider with two meanings (plants, volcanoes), so search by meaning can be tested offline:
"vegetation" lands next to "photosynthesis" without any network."""

import re

PLANTS = {
    "plant",
    "plants",
    "photosynthesis",
    "chlorophyll",
    "leaf",
    "leaves",
    "green",
    "sunlight",
    "sugar",
    "food",
    "vegetation",
    "feed",
}
VOLCANOES = {"volcano", "volcanoes", "magma", "lava", "eruption", "erupt", "erupts", "molten"}


def vector(text: str) -> list[float]:
    words = re.findall(r"[a-z]+", text.lower())
    return [float(sum(w in group for w in words)) for group in (PLANTS, VOLCANOES)]


class FakeEmbedder:
    """Two meanings: plants and volcanoes. Text about neither is a zero vector, similar to nothing."""

    model = "fake:concepts"
    dimensions = 2

    def __init__(self):
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.fail = False

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("provider down")
        self.documents += texts
        return [vector(t) for t in texts]

    def embed_query(self, text):
        if self.fail:
            raise RuntimeError("provider down")
        self.queries.append(text)
        return vector(text)


# ---------------------------------------------------------------- the challenge writers (challenges.py)
from learning_service.challenges import (  # noqa: E402
    DeckDraft,
    EdgeDraft,
    FlowDraft,
    NodeDraft,
    SamePair,
    SameTopics,
    TermDraft,
)
from learning_service.quiz_graph import Question, Quiz  # noqa: E402


def make_questions(n, unresolved=()):
    questions = [
        Question(
            index=i,
            type="multiple_choice",
            question=f"Question number {i}?",
            options=[f"wrong {i}a", f"right {i}", f"wrong {i}b", f"wrong {i}c"],
            answer=f"right {i}",
            explanation=f"Because {i}.",
        )
        for i in range(n)
    ]
    return Quiz(
        title="Quiz",
        topic="t",
        difficulty="medium",
        questions=questions,
        unresolved=list(unresolved),
    )


def draft_deck(extra=()):
    good = [
        TermDraft(
            term=f"Term {chr(65 + i)}",
            forbidden=[f"clue{i}a", f"clue{i}b", f"clue{i}c", f"clue{i}d"],
        )
        for i in range(12)
    ]
    return DeckDraft(terms=[*good, *extra])


def draft_flow(**changes):
    nodes = [
        NodeDraft(id="a", label="Sunlight"),
        NodeDraft(id="b", label="Leaf"),
        NodeDraft(id="c", label="Water"),
        NodeDraft(id="d", label="Carbon dioxide"),
        NodeDraft(id="e", label="Sugar"),
        NodeDraft(id="f", label="Oxygen"),
        NodeDraft(id="g", label="Chlorophyll"),
        NodeDraft(id="h", label="Roots"),
    ]
    edges = [
        EdgeDraft(source="a", target="b", label="gives energy"),
        EdgeDraft(source="c", target="b", label="is used"),
        EdgeDraft(source="d", target="b", label="is used"),
        EdgeDraft(source="b", target="e", label="makes"),
        EdgeDraft(source="b", target="f", label="releases"),
        EdgeDraft(source="h", target="c", label="take in"),
        EdgeDraft(source="g", target="b", label="captures light"),
    ]
    values = dict(
        title="How a leaf makes food",
        caption="Follow the arrows.",
        nodes=nodes,
        edges=edges,
        distractors=[
            "Volcano",
            "Moon",
            "Magnet",
            "Sunlight",
        ],  # the last repeats a box, so it must be dropped
    )
    values.update(changes)
    return FlowDraft(**values)


class Models:
    """Records what the AI was asked, and returns what the test scripts."""

    def __init__(self):
        self.quizzes: list = []
        self.decks: list[str] = []
        self.flows: list[str] = []
        self.quiz = make_questions(12)
        self.deck = draft_deck()
        self.flow = draft_flow()
        self.fail = None
        self.same_topic_prompts: list[str] = []
        self.same_topic_rule = lambda a, b: (
            True
        )  # what the model says about each pair of topic names

    def make_quiz(self, request):
        self.quizzes.append(request)
        if self.fail == "quiz":
            raise RuntimeError("provider key sk-secret-123 rejected")
        return self.quiz

    def ask_deck(self, prompt):
        self.decks.append(prompt)
        if self.fail == "deck":
            raise RuntimeError("provider key sk-secret-123 rejected")
        return self.deck

    def ask_flow(self, prompt):
        self.flows.append(prompt)
        if self.fail == "flow":
            raise RuntimeError("provider key sk-secret-123 rejected")
        return self.flow

    def ask_same_topic(self, prompt):
        self.same_topic_prompts.append(prompt)
        if self.fail == "same_topic":
            raise RuntimeError("provider key sk-secret-123 rejected")
        pairs = re.findall(r'^(\d+)\. "(.*)" \| "(.*)"$', prompt, re.M)
        return SameTopics(
            verdicts=[SamePair(pair=int(n), same=self.same_topic_rule(a, b)) for n, a, b in pairs]
        )

    @property
    def calls(self):
        return len(self.quizzes) + len(self.decks) + len(self.flows)
