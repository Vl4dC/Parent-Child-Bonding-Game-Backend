import itertools
import random

import pytest

import weakness as wk
from quiz_graph import Question
from test_visual_recall import ITEM
from test_visuals import ScriptedLLM
from visual_recall import BlankItem, IllustratedQuestion, to_mcq


def old_q(index=0, question="What happens to a puddle when the Sun warms it?", answer="It evaporates",
          options=("It evaporates", "It freezes", "It disappears", "It falls")):
    return IllustratedQuestion(index=index, type="multiple_choice", question=question, options=list(options),
                               answer=answer, explanation="Warm water turns into vapor.")


def miss(index=0, q=None, picked="It freezes"):
    return wk.Miss(index=index, question=q or old_q(index), picked=picked)


def request(misses=None, **kw):
    return wk.RemedialRequest(topic="the water cycle", age=10, lesson_text="LESSON TEXT",
                              misses=misses or [miss()], **kw)


def diagram_miss(index=1):
    return wk.Miss(index=index, question=to_mcq(ITEM, random.Random(1)).model_copy(update={"index": index}),
                   picked="Starch")


# ---------------------------------------------------------------- pure helpers
def test_similar_ignores_filler_words():
    assert wk.similar("What happens to the puddle?", "what HAPPENS to a puddle") == 1.0
    assert wk.similar("How do clouds form?", "Why does a puddle shrink?") < 0.2
    assert wk.similar("", "") == 0.0


def test_normalize_keeps_only_sensible_groupings():
    misses = [miss(0), miss(2), miss(5)]
    raw = [wk.Weakness(concept="  Evaporation ", misconception="thinks it freezes", misses=[0, 0, 9]),   # 9 unknown
           wk.Weakness(concept="Clouds", misses=[0, 2]),                                                  # 0 already used
           wk.Weakness(concept="   ", misses=[5])]                                                        # no name
    out = wk.normalize(raw, misses)
    # the model's own order is kept for equally-missed ideas; miss 5, which it skipped, gets a fallback
    # weakness named after the question's explanation
    assert [(w.concept, w.misses) for w in out] == [("Evaporation", [0]), ("Clouds", [2]),
                                                    ("Warm water turns into vapor.", [5])]


def test_normalize_puts_most_missed_first_and_caps():
    misses = [miss(i) for i in range(8)]
    raw = [wk.Weakness(concept=f"idea {i}", misses=[i]) for i in range(1, 7)] + [wk.Weakness(concept="big", misses=[0, 7])]
    out = wk.normalize(raw, misses)
    assert out[0].concept == "big" and len(out) == wk.MAX_WEAKNESSES


def test_plan_sizes_the_quiz_from_the_weaknesses():
    rng = random.Random(0)
    one = [wk.Weakness(concept="a", misses=[0])]
    assert len(wk.plan_slots(request(), one, rng)) == 3                       # never fewer than 3
    many = [wk.Weakness(concept=str(i), misses=[0]) for i in range(5)]
    assert len(wk.plan_slots(request(), many, rng)) == 10                     # 2 per weakness, capped
    assert len(wk.plan_slots(request(num_questions=4), many, rng)) == 4       # explicit size wins


def test_plan_alternates_ideas_and_varies_angles_within_one_idea():
    ws = [wk.Weakness(concept="a", misses=[0, 1]), wk.Weakness(concept="b", misses=[2])]
    slots = wk.plan_slots(request(misses=[miss(0), miss(1), miss(2)], num_questions=6), ws, random.Random(3))
    assert [s.weakness for s in slots] == [0, 1, 0, 1, 0, 1]
    for w in (0, 1):
        angles = [s.angle for s in slots if s.weakness == w]
        assert len(set(angles)) == 3  # three new questions on one idea, three different angles


def test_plan_is_reproducible_with_a_seed():
    ws = [wk.Weakness(concept="a", misses=[0])]
    a = wk.plan_slots(request(), ws, random.Random(7))
    b = wk.plan_slots(request(), ws, random.Random(7))
    assert a == b


def test_plan_varies_missed_diagram_questions_but_not_more_than_the_cap():
    misses = [diagram_miss(1), diagram_miss(2), diagram_miss(3), miss(0)]
    ws = [wk.Weakness(concept=f"w{i}", misses=[m.index]) for i, m in enumerate(misses)]
    slots = wk.plan_slots(request(misses=misses), ws, random.Random(0))
    converted = [s for s in slots if s.diagram_miss is not None]
    assert len(converted) == wk.MAX_DIAGRAM_VARIANTS
    assert len({s.weakness for s in converted}) == len(converted)  # one per idea


# ---------------------------------------------------------------- "not the same question" rules
def slot(qtype="multiple_choice", truth=True):
    return wk.Slot(n=0, weakness=0, angle="x", qtype=qtype, truth=truth)


def new_q(question="A pot of soup left uncovered slowly gets lower. Which process explains this?",
          answer="Liquid turns into vapor", options=("Liquid turns into vapor", "The soup freezes", "The soup melts", "The pot shrinks")):
    return Question(type="multiple_choice", question=question, options=list(options), answer=answer, explanation="e")


def test_a_genuinely_new_question_is_accepted():
    assert wk.problem_with(new_q(), slot(), [miss()]) is None


@pytest.mark.parametrize("make, reason", [
    (lambda: new_q(question="What happens to a puddle when the sun warms it up?"), "too similar"),
    (lambda: new_q(answer="It evaporates", options=("It evaporates", "A", "B", "C")), "repeats the old one"),
    (lambda: new_q(options=("Liquid turns into vapor", "It freezes", "It disappears", "X"), answer="Liquid turns into vapor"), "reuses options"),
])
def test_copies_of_the_old_question_are_rejected(make, reason):
    assert reason in wk.problem_with(make(), slot(), [miss()])


def test_wrong_type_and_wrong_truth_value_are_rejected():
    assert "must be a true_false" in wk.problem_with(new_q(), slot("true_false"), [miss()])
    tf = Question(type="true_false", question="Soup left out loses water to the air.", answer="True", explanation="e")
    assert wk.problem_with(tf, slot("true_false", truth=True), [miss()]) is None
    assert "must be false" in wk.problem_with(tf, slot("true_false", truth=False), [miss()])


def test_diagram_questions_are_not_compared_by_their_generic_text():
    assert wk.problem_with(new_q(), slot(), [diagram_miss()]) is None


# ---------------------------------------------------------------- writing one question
def flaky_writer(*responses):
    """Writer that answers with each response in turn (an Exception is raised instead)."""
    queue = list(responses)

    def write(_prompt):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return write


def run_text(monkeypatch, writer, rng=None):
    llm = ScriptedLLM({Question: writer})
    monkeypatch.setattr(wk, "_llm", lambda: llm)
    weakness = wk.Weakness(concept="Evaporation", misconception="thinks warm water freezes", misses=[0])
    q = wk.text_variant(request(), slot(), weakness, [miss()], "", rng or random.Random(0))
    return q, llm


def test_rejected_question_is_rewritten_with_the_reason(monkeypatch):
    copy = new_q(answer="It evaporates", options=("It evaporates", "A", "B", "C"))
    q, llm = run_text(monkeypatch, flaky_writer(copy, new_q()))
    assert q.answer == "Liquid turns into vapor" and len(llm.prompts) == 2
    assert "rejected" in llm.prompts[1] and "repeats the old one" in llm.prompts[1]


def test_prompt_names_the_idea_the_belief_and_the_old_question(monkeypatch):
    _, llm = run_text(monkeypatch, flaky_writer(new_q()))
    p = llm.prompts[0]
    assert "Evaporation" in p and "thinks warm water freezes" in p and "do NOT reuse" in p
    assert "What happens to a puddle when the Sun warms it?" in p and "they chose: It freezes" in p
    assert "LESSON TEXT" in p and "10-year-old" in p


def test_model_errors_count_as_attempts_and_it_gives_up_eventually(monkeypatch):
    q, llm = run_text(monkeypatch, flaky_writer(RuntimeError("timeout"), RuntimeError("timeout"), RuntimeError("timeout")))
    assert q is None and len(llm.prompts) == wk.MAX_ATTEMPTS


def test_answer_position_is_shuffled(monkeypatch):
    positions = set()
    for seed in range(40):
        q, _ = run_text(monkeypatch, flaky_writer(new_q()), rng=random.Random(seed))
        assert sorted(q.options) == sorted(new_q().options)
        positions.add(q.options.index(q.answer))
    assert positions == {0, 1, 2, 3}


# ---------------------------------------------------------------- diagram variants
def test_missed_diagram_question_gets_a_different_box_of_the_same_diagram(monkeypatch):
    calls = []

    def fake_blank(visual, topic, age, k, exclude=frozenset()):
        calls.append((visual.code, exclude))
        return [BlankItem(visual=visual, node_id="oxygen", answer="Oxygen",
                             distractors=["Nitrogen", "Helium", "Argon"], explanation="Leaves release oxygen.")]

    monkeypatch.setattr(wk, "make_blank_items", fake_blank)
    monkeypatch.setattr(wk, "_llm", lambda: pytest.fail("a diagram variant needs no writer"))
    dm = diagram_miss(1)
    ws = wk.Weakness(concept="photosynthesis", misses=[1])
    s = wk.Slot(n=0, weakness=0, angle="x", qtype="multiple_choice", truth=True, diagram_miss=1)
    out = wk.make_variant({"request": request(misses=[dm]), "slot": s, "weakness": ws})["questions"][0]
    assert calls[0][1] == frozenset({"sugar"}) and calls[0][0] == dm.question.answer_visual.code  # new box, same diagram
    assert out.node_id == "oxygen" and out.visual is not None and "Oxygen" not in out.visual.code


def test_diagram_variant_falls_back_to_a_text_question(monkeypatch):
    monkeypatch.setattr(wk, "make_blank_items", lambda *a, **k: [])
    llm = ScriptedLLM({Question: flaky_writer(new_q())})
    monkeypatch.setattr(wk, "_llm", lambda: llm)
    s = wk.Slot(n=0, weakness=0, angle="x", qtype="multiple_choice", truth=True, diagram_miss=1)
    out = wk.make_variant({"request": request(misses=[diagram_miss(1)]), "slot": s,
                           "weakness": wk.Weakness(concept="c", misses=[1])})["questions"][0]
    assert out.visual is None and out.question.startswith("A pot of soup")


# ---------------------------------------------------------------- the whole graph
STEMS = itertools.cycle([
    "A pot of soup left uncovered slowly gets lower. Which process explains this?",
    "Why does a wet towel on a sunny line feel dry by evening?",
    "A cold glass in a warm room gets drops on the outside. Where did they come from?",
    "Steam from a kettle hits a cold window. What do you see form?",
    "Rain keeps falling on a hill. Where does most of the water end up?",
    "Which sentence about hot sidewalks after a rainstorm is correct?",
])


def graph_writer(counter=itertools.count(1)):
    def write(prompt):
        k = next(counter)
        stem = next(STEMS)
        if "true_false question" in prompt:
            truth = "statement that is TRUE" in prompt
            return Question(type="true_false", question=f"{stem} (statement {k})", answer="True" if truth else "False",
                            explanation="e")
        return Question(type="multiple_choice", question=stem + f" #{k}", options=[f"Right {k}", f"Wrong {k}a", f"Wrong {k}b", f"Wrong {k}c"],
                        answer=f"Right {k}", explanation="e")
    return write


def run_graph(monkeypatch, reviews, diagnosis=None, misses=None):
    misses = misses or [miss(0), miss(1, old_q(1, "How do clouds form?", "Vapor cools", ("Vapor cools", "Rain rises", "Sun melts", "Wind stops"))),
                        miss(2, old_q(2, "Where does rain collect?", "Rivers and lakes", ("Rivers and lakes", "Clouds only", "The Sun", "Nowhere")))]
    diagnosis = diagnosis or wk.Diagnosis(weaknesses=[
        wk.Weakness(concept="Evaporation", misconception="thinks warm water freezes", misses=[0]),
        wk.Weakness(concept="Clouds and rain", misconception="", misses=[1, 2])])
    llm = ScriptedLLM({wk.Diagnosis: [diagnosis], Question: graph_writer(), wk.VariantReview: reviews})
    monkeypatch.setattr(wk, "_llm", lambda: llm)
    return wk.make_remedial(request(misses=misses, seed=5)), llm


def test_full_run_writes_new_questions_on_the_weak_ideas(monkeypatch):
    result, llm = run_graph(monkeypatch, [wk.VariantReview()])
    assert len(result.questions) == 4 and [q.index for q in result.questions] == [0, 1, 2, 3]
    assert result.focus == ["Clouds and rain", "Evaporation"] or result.focus == ["Evaporation", "Clouds and rain"]
    old = {"What happens to a puddle when the Sun warms it?", "How do clouds form?", "Where does rain collect?"}
    assert not old & {q.question for q in result.questions}
    assert len(llm.prompts) == 1 + 4 + 1  # diagnose, four writers, one review
    assert llm.prompts[0].startswith("A 10-year-old studied")


def test_flagged_question_is_rewritten_once(monkeypatch):
    bad = wk.VariantReview(bad=[wk.BadVariant(index=0, issue="two options are correct")])
    result, llm = run_graph(monkeypatch, [bad, wk.VariantReview()])
    assert len(result.questions) == 4
    rewrite = [p for p in llm.prompts if "A reviewer flagged" in p]
    assert len(rewrite) == 1 and "two options are correct" in rewrite[0]


def test_question_still_flagged_after_the_rewrite_is_dropped(monkeypatch):
    bad = wk.VariantReview(bad=[wk.BadVariant(index=0, issue="wrong answer key")])
    result, _ = run_graph(monkeypatch, [bad, bad])
    assert len(result.questions) == 3


def test_diagnosis_failure_still_produces_a_quiz(monkeypatch):
    def boom(_p):
        raise RuntimeError("model hiccup")

    llm = ScriptedLLM({wk.Diagnosis: boom, Question: graph_writer(), wk.VariantReview: [wk.VariantReview()]})
    monkeypatch.setattr(wk, "_llm", lambda: llm)
    result = wk.make_remedial(request(misses=[miss(0)], seed=1))
    assert len(result.questions) == 3 and result.focus == ["Warm water turns into vapor."]


def test_assemble_drops_near_duplicates_and_still_flagged_questions_and_numbers_the_rest():
    def tf(text):
        return IllustratedQuestion(type="true_false", question=text, answer="True", explanation="e",
                                   options=["True", "False"])

    def at(n, weakness):
        return wk.Slot(n=n, weakness=weakness, angle="x", qtype="true_false", truth=True)

    state = {
        "weaknesses": [wk.Weakness(concept="a", misses=[0]), wk.Weakness(concept="b", misses=[1])],
        "slots": [at(0, 0), at(1, 0), at(2, 1), at(3, 1)],
        "questions": {0: tf("Soup left out loses water to the air."),
                      1: tf("Soup left out loses water to the air!"),         # same question again
                      2: tf("Steam turns into drops on a cold window."),
                      3: tf("Rain collects in rivers.")},                      # flagged by the reviewer
        "bad": [wk.BadVariant(index=3, issue="wrong key")],
    }
    out = wk.assemble(state)["result"]
    assert [(q.index, q.question) for q in out.questions] == [
        (0, "Soup left out loses water to the air."), (1, "Steam turns into drops on a cold window.")]
    assert out.focus == ["a", "b"]


@pytest.mark.parametrize("statement", [
    "A child says water on a garden path is precipitation. This is a mistake: precipitation falls from clouds.",
    "Clouds are made of tiny drops. True or false?",
    "Rain forms when vapor warms up. That is wrong.",
    "The answer is that vapor cools first.",
])
def test_true_false_statements_that_give_away_their_answer_are_rejected(statement):
    q = Question(type="true_false", question=statement, answer="True", explanation="e")
    assert "gives away its own answer" in wk.problem_with(q, slot("true_false", truth=True), [miss()])


def test_plain_true_false_claims_pass_and_the_prompt_forbids_verdicts():
    q = Question(type="true_false", question="Steam turns into tiny drops when it touches a cold window.",
                 answer="True", explanation="e")
    assert wk.problem_with(q, slot("true_false", truth=True), [miss()]) is None
    prompt = wk.variant_prompt(request(), slot("true_false"), wk.Weakness(concept="c", misses=[0]), [miss()], "")
    assert "never 'true or false'" in prompt
