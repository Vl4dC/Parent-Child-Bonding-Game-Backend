import random

import pytest

import visual_recall as vr
from conftest import FAKE_VISUAL
from test_quiz_graph import RecordingLLM

LEAF = """flowchart TD
  sun["Sunlight"] -->|"gives energy"| leaf["Leaf"]
  water(["Water from roots"]) --> leaf
  air{"Carbon dioxide"} --> leaf
  leaf -->|"makes"| sugar["Glucose"]
  leaf --> oxygen(("Oxygen"))
  subgraph plant["The plant"]
    leaf
  end"""


def visual(code=LEAF):
    return FAKE_VISUAL.model_copy(update={"code": code})


# ---------------------------------------------------------------- parsing
def test_nodes_finds_every_box_shape_but_not_edges_or_subgraphs():
    labels = {n.id: n.label for n in vr.nodes(LEAF)}
    assert labels == {"sun": "Sunlight", "leaf": "Leaf", "water": "Water from roots", "air": "Carbon dioxide",
                      "sugar": "Glucose", "oxygen": "Oxygen"}


def test_only_flowcharts_are_parsed():
    assert vr.nodes('timeline\n  2020 : "Event"') == []
    assert vr.nodes('mindmap\n  root["Idea"]') == []


def test_blank_code_hides_one_box_and_dashes_it():
    out = vr.blank_code(LEAF, "sugar")
    assert 'sugar["?"]' in out and "Glucose" not in out
    assert "style sugar stroke-width:3px,stroke-dasharray:6 4" in out
    assert 'leaf["Leaf"]' in out and '|"makes"|' in out  # everything else untouched


def test_blank_code_can_show_a_candidate_label():
    assert 'sugar["Starch"]' in vr.blank_code(LEAF, "sugar", fill="Starch")


def test_recallable_skips_labels_that_would_leak():
    code = 'flowchart LR\n  a["Leaf"] -->|"the leaf makes food"| b["Food"]\n  c["Sun"] --> a'
    ids = {n.id for n in vr.recallable(code)}
    assert "a" not in ids  # "Leaf" still readable in the edge label
    assert ids == {"c"}  # "Food" leaks too ("makes food")


def test_recallable_skips_duplicate_labels():
    code = 'flowchart LR\n  a["Water"] --> b["Cloud"]\n  c["Water"] --> b'
    assert {n.id for n in vr.recallable(code)} == {"b"}


# ---------------------------------------------------------------- model choices, validated in code
def plan(*choices):
    return vr.BlankPlan(items=[vr.BlankChoice(node_id=i, distractors=d, explanation="because") for i, d in choices])


def test_make_blank_items_validates_model_choices(monkeypatch):
    llm = RecordingLLM(plan(
        ("nope", ["a", "b", "c"]),                                   # unknown id -> dropped
        ("sugar", ["Starch", "Leaf", "Protein", "Salt", "Starch"]),  # "Leaf" is on the diagram -> removed
        ("oxygen", ["Nitrogen", "Oxygen"]),                          # too few usable -> dropped
    ))
    monkeypatch.setattr(vr, "_llm", lambda: llm)
    items = vr.make_blank_items(visual(), "photosynthesis", 10, k=2)
    assert [(i.node_id, i.answer, i.distractors) for i in items] == [("sugar", "Glucose", ["Starch", "Protein", "Salt"])]
    assert "sun: Sunlight" in llm.prompts[0] and "10-year-old" in llm.prompts[0]


def test_non_flowchart_makes_no_items_and_no_model_call(monkeypatch):
    monkeypatch.setattr(vr, "_llm", lambda: pytest.fail("should not call the model"))
    assert vr.make_blank_items(visual("timeline\n  a : b"), "t", 10) == []


# ---------------------------------------------------------------- builders
ITEM = vr.BlankItem(visual=visual(), node_id="sugar", answer="Glucose",
                    distractors=["Starch", "Protein", "Salt"], explanation="Leaves make glucose.")


def test_mcq_hides_the_box_and_offers_the_answer_among_distractors():
    q = vr.to_mcq(ITEM, random.Random(1))
    assert sorted(q.options) == ["Glucose", "Protein", "Salt", "Starch"] and q.answer == "Glucose"
    assert "Glucose" not in q.visual.code and 'sugar["?"]' in q.visual.code
    assert q.answer_visual.code == LEAF
    assert FAKE_VISUAL.title not in q.visual.title  # titles can name the answer, so they're replaced


@pytest.mark.parametrize("seed", range(6))
def test_true_false_shows_right_or_wrong_label_consistently(seed):
    q = vr.to_true_false(ITEM, random.Random(seed))
    shown = q.visual.code.split('sugar["')[1].split('"')[0]
    if q.answer == "True":
        assert shown == "Glucose" and q.explanation.startswith("Yes")
    else:
        assert shown in ITEM.distractors and q.explanation.startswith("No") and "Glucose" in q.explanation
    assert q.options == ["True", "False"]
