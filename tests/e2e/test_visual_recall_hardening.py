"""Diagram recall (hide a box, ask what belongs there): leak-proofing, hostile labels, model-output hygiene."""

import random
import time

import pytest

import learning_service.visual_recall as vr
from tests.learning.conftest import FAKE_VISUAL
from tests.learning.test_quiz_graph import RecordingLLM

WORDS = [
    "Sun",
    "Leaf",
    "Root",
    "Water",
    "Sugar",
    "Oxygen",
    "Cell",
    "Energy",
    "Light",
    "Plant",
    "Heat",
    "Cloud",
    "Rain",
    "River",
    "Ocean",
    "Ice",
]
SHAPES = [("[", "]"), ("(", ")"), ("{", "}"), ("([", "])"), ("((", "))"), ("[[", "]]")]


def visual(code):
    return FAKE_VISUAL.model_copy(update={"code": code})


def random_flowchart(rng):
    count = rng.randint(3, 9)
    labels = [
        rng.choice(WORDS) + (f" {rng.choice(WORDS).lower()}" if rng.random() < 0.5 else "")
        for _ in range(count)
    ]
    lines = ["flowchart TD"]
    for n, label in enumerate(labels):
        left, right = rng.choice(SHAPES)
        lines.append(f'  n{n}{left}"{label}"{right}')
    for _ in range(rng.randint(2, count + 2)):
        a, b = rng.sample(range(count), 2)
        edge = f'|"{rng.choice(WORDS).lower()} it"|' if rng.random() < 0.4 else ""
        lines.append(f"  n{a} -->{edge} n{b}")
    if rng.random() < 0.3:
        lines += ['  subgraph grp["The whole picture"]', "    n0", "  end"]
    return "\n".join(lines)


# ---------------------------------------------------------------- the answer can never be read off the picture
@pytest.mark.parametrize("seed", range(150))
def test_a_hidden_box_never_leaks_its_label_anywhere_in_the_picture(seed):
    code = random_flowchart(random.Random(seed))
    for node in vr.recallable(code):
        hidden = vr.blank_code(code, node.id)
        assert node.label.casefold() not in hidden.casefold(), (node, hidden)
        assert vr.BLANK in hidden and f"style {node.id} " in hidden
        # Every other box is still there, untouched.
        for other in vr.nodes(code):
            if other.id != node.id:
                assert f'"{other.label}"' in hidden


@pytest.mark.parametrize("seed", range(40))
def test_questions_built_from_any_diagram_hide_the_answer_until_it_is_revealed(seed):
    rng = random.Random(1000 + seed)
    code = random_flowchart(rng)
    candidates = vr.recallable(code)
    if not candidates:
        pytest.skip("this random diagram has no box that can be hidden fairly")
    node = rng.choice(candidates)
    item = vr.BlankItem(
        visual=visual(code),
        node_id=node.id,
        answer=node.label,
        distractors=["Decoy one", "Decoy two", "Decoy three"],
        explanation="because",
    )
    mcq = vr.to_mcq(item, rng)
    tf = vr.to_true_false(item, rng)
    assert node.label.casefold() not in mcq.visual.code.casefold()
    assert node.label.casefold() in mcq.answer_visual.code.casefold()
    assert sorted(mcq.options) == sorted([node.label, "Decoy one", "Decoy two", "Decoy three"])
    if tf.answer == "False":  # a wrong label is shown, never the real one
        assert node.label.casefold() not in tf.visual.code.casefold()
        assert "Decoy" in tf.visual.code
    assert mcq.visual.title == tf.visual.title == "Lesson diagram" and mcq.visual.caption == ""
    assert (
        node.label.casefold() not in (mcq.visual.alt + mcq.visual.title + mcq.question).casefold()
    )


@pytest.mark.parametrize(
    "label",
    [
        "a+b",
        "(x)",
        r"\d+",
        "$1",
        "a.b",
        "x|y",
        "{curly}",
        "Ünïcödé",
        "日本語",
        "A  B   C",
        "100%",
        "C++",
        "a&b",
        "x<y",
        "it's",
    ],
)
def test_labels_full_of_regex_and_markup_characters_are_hidden_exactly(label):
    code = f'flowchart LR\n  s["Start here"] --> t["{label}"] --> e["Finish line"]'
    assert {n.id: n.label for n in vr.nodes(code)}["t"] == label
    assert "t" in {n.id for n in vr.recallable(code)}
    hidden = vr.blank_code(code, "t")
    assert label not in hidden and 't["?"]' in hidden and 's["Start here"]' in hidden


def test_a_quote_in_the_replacement_label_cannot_close_the_label_early():
    out = vr.blank_code('flowchart LR\n  a["Real"] --> b["Other"]', "a", fill='say "hi"')
    assert "a[\"say 'hi'\"]" in out


def test_boxes_whose_label_reappears_anywhere_are_not_offered():
    code = 'flowchart LR\n  a["Water"] -->|"water flows"| b["River"]\n  c["Rain"] --> b\n  d["Rain"] --> b'
    # "Water" is readable in an edge label and "Rain" is duplicated; only "River" is safe to hide.
    assert {n.id for n in vr.recallable(code)} == {"b"}
    # A label that is only a fragment of a longer word elsewhere is also withheld (conservative).
    assert "c" not in {n.id for n in vr.recallable('flowchart LR\n  c["Sun"] --> d["Sunlight"]')}


def test_a_box_whose_id_spells_its_label_is_not_offered():
    code = 'flowchart LR\n  Firewall["Firewall"] --> n2["Inside"]'
    assert {n.id for n in vr.recallable(code)} == {
        "n2"
    }  # hiding "Firewall" would leave the id readable


# ---------------------------------------------------------------- hostile or huge diagram code cannot stall the parser
HOSTILE_CODE = {
    "unclosed-quote": "flowchart LR\n" + 'a["' + "x" * 4900,
    "many-open-brackets": "flowchart LR\n" + "[" * 4900,
    "long-identifier": "flowchart LR\n" + "a" * 4900,
    "spaces": "flowchart LR\n" + "a " * 2400,
    "quote-storm": "flowchart LR\n" + '"' * 4900,
    "nested-shapes": "flowchart LR\n" + 'a(["' * 1200,
    "many-nodes": "flowchart LR\n" + "\n".join(f'n{i}["Label number {i}"]' for i in range(150)),
    "many-edges-same-label": "flowchart LR\n"
    + "\n".join(f'a{i}["Same"] --> b{i}["Same"]' for i in range(150)),
}


@pytest.mark.parametrize("name", HOSTILE_CODE)
def test_pathological_diagram_code_is_parsed_in_bounded_time(name):
    code = HOSTILE_CODE[name]
    started = time.perf_counter()
    nodes = vr.nodes(code)
    fair = vr.recallable(code)
    for node in fair[:5]:
        vr.blank_code(code, node.id)
    assert time.perf_counter() - started < 2.0, name
    assert len(fair) <= len(nodes)


# ---------------------------------------------------------------- what the model may and may not contribute
LEAF = 'flowchart TD\n  sun["Sunlight"] --> l2["Leaf"]\n  l2 --> sugar["Glucose"]'


def choose(*choices):
    return vr.BlankPlan(
        items=[vr.BlankChoice(node_id=i, distractors=d, explanation="because") for i, d in choices]
    )


def run(monkeypatch, plan, k=1, exclude=frozenset(), code=LEAF):
    monkeypatch.setattr(vr, "_llm", lambda: RecordingLLM(plan))
    return vr.make_blank_items(visual(code), "photosynthesis", 12, k=k, exclude=exclude)


def test_the_model_cannot_reuse_the_answer_or_diagram_words_as_wrong_options(monkeypatch):
    items = run(
        monkeypatch,
        choose(
            ("sugar", ["glucose", "GLUCOSE", "Sunlight", "Leaf", "Starch", "Fructose", "Maltose"])
        ),
    )
    assert items[0].distractors == ["Starch", "Fructose", "Maltose"]


def test_too_few_usable_options_means_no_question_rather_than_a_weak_one(monkeypatch):
    assert run(monkeypatch, choose(("sugar", ["Starch", "Starch", "starch", ""]))) == []
    assert (
        run(monkeypatch, choose(("sugar", ["Starch", "x" * 61, "Fructose"]))) == []
    )  # one is over the length limit
    assert run(monkeypatch, choose(("sugar", []))) == []


def test_requests_are_capped_deduplicated_and_can_skip_boxes(monkeypatch):
    plan = choose(
        ("sugar", ["A1", "A2", "A3"]),
        ("sugar", ["B1", "B2", "B3"]),
        ("l2", ["C1", "C2", "C3"]),
        ("sun", ["D1", "D2", "D3"]),
    )
    assert [i.node_id for i in run(monkeypatch, plan, k=2)] == ["sugar", "l2"]
    assert [i.node_id for i in run(monkeypatch, plan, k=3, exclude=frozenset({"sugar"}))] == [
        "l2",
        "sun",
    ]
    assert run(monkeypatch, plan, k=0) == []
    assert run(monkeypatch, choose()) == []


def test_the_model_is_only_offered_boxes_that_can_be_hidden_fairly(monkeypatch):
    llm = RecordingLLM(choose(("sugar", ["A1", "A2", "A3"])))
    monkeypatch.setattr(vr, "_llm", lambda: llm)
    vr.make_blank_items(visual('flowchart LR\n  a["Water"] -->|"water"| b["Cloud"]'), "water", 12)
    assert "a: Water" not in llm.prompts[0] and "b: Cloud" in llm.prompts[0]


def test_distractors_are_trimmed_and_unquoted(monkeypatch):
    items = run(monkeypatch, choose(("sugar", ['  "Starch"  ', "'Protein'", "Salt "])))
    assert items[0].distractors[0] == "Starch" and items[0].distractors[2] == "Salt"


@pytest.mark.xfail(
    strict=True,
    reason="BUG-13: a model-written wrong option containing a newline injects Mermaid statements into the learner's diagram",
)
def test_a_wrong_option_cannot_smuggle_extra_diagram_statements(monkeypatch):
    sneaky = 'Modem"]\n  click sun call alert(1)\n  x["y'
    items = run(monkeypatch, choose(("sugar", ["Router", sneaky, "Cable"])))
    original_lines = len(LEAF.splitlines())
    for item in items:
        assert all("\n" not in d for d in item.distractors)
        for seed in range(8):
            shown = vr.to_true_false(item, random.Random(seed)).visual.code
            assert (
                len(shown.splitlines()) == original_lines + 1
            )  # the original diagram plus the dashed-border style line
            assert "click" not in shown
