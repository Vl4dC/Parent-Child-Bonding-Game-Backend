"""Every prompt that makes the model write something a learner will read carries the age rules."""
import pytest
from conftest import FAKE_LESSON, FAKE_VISUAL

import flashcards
import quiz_graph
import visual_recall
import weakness
from lesson import LessonRequest, lesson_prompt
from safety import content_rules
from visual_recall import IllustratedQuestion
from visuals import harness
from visuals.models import VisualPlanItem, VisualRequest

MISS = weakness.Miss(index=0, picked="False", question=IllustratedQuestion(
    type="true_false", question="NACLs are stateful.", answer="False", explanation="They are stateless."))
SLOT = weakness.Slot(n=0, weakness=0, angle="a new example", qtype="true_false", truth=True)
WEAK = weakness.Weakness(concept="Firewalls", misses=[0])
ITEM = VisualPlanItem(kind="diagram", purpose="See the firewall decide", brief="boxes and arrows", section_index=0)


def remedial(age, note=""):
    return weakness.RemedialRequest(topic="networks", age=age, lesson_text="Lesson.", misses=[MISS], note=note)


def visual_request(age, note="", ask="show me a firewall"):
    return VisualRequest(topic="networks", age=age, lesson=FAKE_LESSON, ask=ask, note=note)


# (name, build(age, note) -> the prompt). Where the model writes text a learner will read.
WRITERS = {
    "lesson": lambda age, note: lesson_prompt(LessonRequest(topic="volcanoes", age=age), note=note),
    "lesson from a book": lambda age, note: lesson_prompt(LessonRequest(topic="volcanoes", age=age), "Page text.", note),
    "quiz questions": lambda age, note: quiz_graph._context(quiz_graph.QuizRequest(prompt="p", age=age, note=note)),
    "flashcards": lambda age, note: flashcards.cards_prompt(FAKE_LESSON, "volcanoes", age, 5, note),
    "weak-spot diagnosis": lambda age, note: weakness.diagnose_prompt(remedial(age, note)),
    "weak-spot question": lambda age, note: weakness.variant_prompt(remedial(age, note), SLOT, WEAK, [MISS], ""),
    "visual planner": lambda age, note: harness.plan_prompt(visual_request(age, note)),
    "visual drawer": lambda age, note: harness.draw_prompt(visual_request(age, note, ask=None), ITEM),
}
# The diagram-box writer is called once per box and is re-run with the whole quiz, so it has no note of its own.
RULES_ONLY = {"diagram boxes": lambda age: visual_recall.blank_prompt(FAKE_VISUAL, [], "networks", age, 1)}


@pytest.mark.parametrize("name", WRITERS)
def test_the_prompt_carries_the_rules_for_the_learners_age(name):
    assert content_rules(8) in WRITERS[name](8, "")
    assert content_rules(15) in WRITERS[name](15, "")
    assert content_rules(8) not in WRITERS[name](15, "")


@pytest.mark.parametrize("name", WRITERS)
def test_the_prompt_leaves_adults_alone(name):
    assert "Content rules" not in WRITERS[name](30, "")


@pytest.mark.parametrize("name", WRITERS)
def test_the_rewrite_note_reaches_the_writer(name):
    assert "fix-this-please" in WRITERS[name](8, "fix-this-please")
    assert "fix-this-please" not in WRITERS[name](8, "")


@pytest.mark.parametrize("name", RULES_ONLY)
def test_the_diagram_box_prompt_carries_the_rules_too(name):
    assert content_rules(8) in RULES_ONLY[name](8)
    assert "Content rules" not in RULES_ONLY[name](30)


def test_the_audience_cap_reaches_the_prompts(monkeypatch):
    monkeypatch.setenv("AUDIENCE_MAX_AGE", "9")
    assert content_rules(9) in lesson_prompt(LessonRequest(topic="volcanoes", age=40))
