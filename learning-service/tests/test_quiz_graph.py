import quiz_graph
from quiz_graph import QuizRequest, QuizSpec


class FakeLLM:
    """Stands in for the chat model: structured output always returns `spec`."""

    def __init__(self, spec):
        self.spec = spec

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _prompt):
        return self.spec


def run_parse(monkeypatch, types, allowed):
    spec = QuizSpec(topic="Python", question_types=types)
    monkeypatch.setattr(quiz_graph, "_llm", lambda: FakeLLM(spec))
    req = QuizRequest(prompt="quiz on python", allowed_types=allowed)
    return quiz_graph.parse_prompt({"request": req})["spec"]


def test_allowed_types_filters_out_short_answer(monkeypatch):
    spec = run_parse(monkeypatch, ["multiple_choice", "short_answer"], ["multiple_choice", "true_false"])
    assert spec.question_types == ["multiple_choice"]


def test_allowed_types_falls_back_to_multiple_choice(monkeypatch):
    spec = run_parse(monkeypatch, ["short_answer"], ["multiple_choice", "true_false"])
    assert spec.question_types == ["multiple_choice"]


def test_no_allowed_types_keeps_everything(monkeypatch):
    spec = run_parse(monkeypatch, ["true_false", "short_answer"], None)
    assert spec.question_types == ["true_false", "short_answer"]


class RecordingLLM(FakeLLM):
    """Like FakeLLM but remembers every prompt it was sent."""

    def __init__(self, result):
        super().__init__(result)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.spec


def test_context_empty_without_age_or_lesson():
    assert quiz_graph._context(None) == ""
    assert quiz_graph._context(QuizRequest(prompt="x")) == ""


def test_generate_question_prompt_carries_age_and_lesson(monkeypatch):
    q = quiz_graph.Question(type="true_false", question="Plants need light.", answer="True", explanation="Yes.")
    llm = RecordingLLM(q)
    monkeypatch.setattr(quiz_graph, "_llm", lambda: llm)
    req = QuizRequest(prompt="x", age=9, source="LESSON: plants turn light into food")
    slot = quiz_graph.Slot(index=3, subtopic="light", qtype="true_false", difficulty="easy")
    out = quiz_graph.generate_question({"slot": slot, "spec": QuizSpec(topic="plants"), "request": req})
    assert out["questions"][3].index == 3
    assert "9-year-old" in llm.prompts[0]
    assert "LESSON: plants turn light into food" in llm.prompts[0]


def test_review_prompt_checks_lesson_coverage(monkeypatch):
    llm = RecordingLLM(quiz_graph.Review())
    monkeypatch.setattr(quiz_graph, "_llm", lambda: llm)
    q = quiz_graph.Question(type="true_false", question="Q", answer="True", explanation="E")
    state = {"request": QuizRequest(prompt="x", age=9, source="LESSON TEXT"), "questions": {0: q}}
    quiz_graph.review(state)
    assert "lesson doesn't cover it" in llm.prompts[0] and "LESSON TEXT" in llm.prompts[0]


def test_mcq_options_are_shuffled_so_the_answer_is_not_always_first(monkeypatch):
    options = ["Right", "Wrong 1", "Wrong 2", "Wrong 3"]
    slot = quiz_graph.Slot(index=0, subtopic="x", qtype="multiple_choice", difficulty="easy")
    positions = set()
    for _ in range(60):
        q = quiz_graph.Question(type="multiple_choice", question="Q?", options=list(options),
                                answer="Right", explanation="E")
        monkeypatch.setattr(quiz_graph, "_llm", lambda q=q: RecordingLLM(q))
        out = quiz_graph.generate_question({"slot": slot, "spec": QuizSpec(topic="t")})["questions"][0]
        assert sorted(out.options) == sorted(options) and out.answer == "Right"
        positions.add(out.options.index("Right"))
    assert positions == {0, 1, 2, 3}
