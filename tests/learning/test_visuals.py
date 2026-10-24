import json

import pytest

from learning_service.lesson import LessonSection, SectionVisual
from learning_service.visuals import harness, render
from learning_service.visuals.models import (
    Visual,
    VisualDraft,
    VisualPlan,
    VisualPlanItem,
    VisualRequest,
)
from tests.learning.conftest import FAKE_LESSON

GOOD = {
    "diagram": 'flowchart LR\n  a["Sun"] -->|"energy"| b["Leaf"]',
    "chart": json.dumps(
        {
            "data": {"values": [{"x": "A", "y": 1}]},
            "mark": "bar",
            "encoding": {"x": {"field": "x"}, "y": {"field": "y"}},
        }
    ),
    "interactive": '<input id="s" type="range"><svg viewBox="0 0 10 10"></svg><script>s.oninput=()=>{}</script>',
}


# ---------------------------------------------------------------- validator
@pytest.mark.parametrize("kind", GOOD)
def test_good_code_passes(kind):
    assert render.validate(kind, GOOD[kind]) is None


@pytest.mark.parametrize(
    "kind, code, reason",
    [
        ("diagram", "hello\n a --> b", "diagram type"),
        ("diagram", "%%{init: {}}%%\nflowchart LR\n a-->b", "init"),
        ("diagram", "flowchart LR\n a-->b\n click a call x()", "click"),
        ("chart", "{not json", "valid JSON"),
        ("chart", json.dumps({"data": {"url": "https://evil/x.csv"}, "mark": "bar"}), "url"),
        ("chart", json.dumps({"data": {"values": []}}), "mark"),
        (
            "chart",
            json.dumps(
                {
                    "data": {"values": []},
                    "layer": [
                        {"mark": "bar"},
                        {"mark": "text", "encoding": {"y": {"field": "a", "axis": None}}},
                    ],
                }
            ),
            "share axes",
        ),
        ("interactive", '<script src="https://cdn.example/x.js"></script>', "external scripts"),
        ("interactive", '<img src="https://example.com/a.png">', "external images"),
        ("interactive", "<script>fetch('/x')</script>", "network"),
        ("interactive", "<script>localStorage.x=1</script>", "storage"),
        ("interactive", "<script>alert(1)</script>", "alert"),
        ("interactive", "<html><body><svg></svg></body></html>", "fragment"),
        ("interactive", "", "empty"),
    ],
)
def test_bad_code_is_rejected_with_reason(kind, code, reason):
    problem = render.validate(kind, code)
    assert problem and reason in problem


def test_oversized_code_is_rejected():
    assert "under 40000" in render.validate("interactive", "<div>" + "x" * 40_001 + "</div>")


def test_clean_strips_fences():
    assert render.clean("```mermaid\nflowchart LR\n a-->b\n```") == "flowchart LR\n a-->b"
    assert render.clean("  plain  ") == "plain"


# ---------------------------------------------------------------- renderer
def visual(kind, code=None, alt="A leaf diagram"):
    return Visual(
        kind=kind, section_index=0, title="T</title>", caption="c", alt=alt, code=code or GOOD[kind]
    )


def test_document_has_sandbox_csp_and_pinned_libs():
    doc = render.document(visual("diagram"), "v1")
    assert "default-src 'none'" in doc and "connect-src" not in doc  # no network beyond scripts
    assert "mermaid@11.4.1" in doc and "<title>T&lt;/title&gt;</title>" in doc
    assert '"visual-height"' in doc or "visual-height" in doc

    chart = render.document(visual("chart"), "v3")
    assert 'v("--fg")' in chart and "theme: " not in chart  # themed from the page CSS variables

    inter = render.document(visual("interactive"), "v2")
    assert "cdn.jsdelivr.net" not in inter and "unsafe-eval" not in inter


def test_diagram_source_is_escaped():
    doc = render.document(visual("diagram", 'flowchart LR\n a["<b>x</b>"] --> b'), "v")
    assert "&lt;b&gt;x&lt;/b&gt;" in doc


def test_chart_and_alt_cannot_break_out_of_script():
    spec = json.dumps({"data": {"values": [{"x": "</script><script>alert(1)//"}]}, "mark": "bar"})
    doc = render.document(visual("chart", spec, alt="</script>oops"), "v")
    assert "</script><script>alert" not in doc and "</script>oops" not in doc


# ---------------------------------------------------------------- skills
def test_skills_loaded_with_when_and_how():
    assert set(harness.SKILLS) == {"diagram", "chart", "interactive"}
    for s in harness.SKILLS.values():
        assert s.description and "#" in s.body


def test_planner_sees_descriptions_not_bodies():
    p = harness.plan_prompt(request(ask="show me a firewall"))
    for s in harness.SKILLS.values():
        assert s.description in p
        assert s.body not in p


def test_drawer_loads_only_the_chosen_skill():
    item = VisualPlanItem(section_index=1, kind="chart", purpose="p", brief="b")
    p = harness.draw_prompt(request(), item)
    assert harness.SKILLS["chart"].body in p and harness.SKILLS["diagram"].body not in p
    assert "Rules" in p and FAKE_LESSON.sections[1].heading in p and "12 years old" in p


# ---------------------------------------------------------------- graph
class ScriptedLLM:
    """Fake chat model: per output schema, a queue of responses or a function of the prompt
    (draws run in parallel, so order-independent answers need the function form)."""

    def __init__(self, script):
        self.script = {k: v if callable(v) else list(v) for k, v in script.items()}
        self.prompts = []

    def with_structured_output(self, schema):
        llm = self

        class Bound:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                entry = llm.script[schema]
                return entry(prompt) if callable(entry) else entry.pop(0)

        return Bound()


def lesson_with(kinds):
    """A lesson with one section per entry; each entry is the planned visual kind, or None."""
    sections = [
        LessonSection(
            heading=f"Section {i}",
            body=f"Body {i}",
            visual=SectionVisual(kind=k, purpose="p", brief=f"draw {i}") if k else None,
        )
        for i, k in enumerate(kinds)
    ]
    return FAKE_LESSON.model_copy(update={"sections": sections})


def request(lesson=FAKE_LESSON, **kw):
    return VisualRequest(topic="networks", age=12, lesson=lesson, **kw)


def draft(code):
    return VisualDraft(title="t", caption="c", alt="a", code=code)


def run(monkeypatch, script, **kw):
    llm = ScriptedLLM(script)
    monkeypatch.setattr(harness, "_llm", lambda: llm)
    return harness.make_visuals(request(**kw)), llm


def by_skill(prompt):
    """Answer each draw with valid code for whichever skill the prompt loaded."""
    if "Vega-Lite" in prompt:
        return draft(GOOD["chart"])
    return draft(GOOD["interactive"] if "HTML fragment" in prompt else GOOD["diagram"])


def test_lesson_planned_visuals_are_drawn_without_a_planner_call(monkeypatch):
    visuals, llm = run(
        monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["diagram", None, "chart"])
    )
    assert [(v.kind, v.section_index) for v in visuals] == [("diagram", 0), ("chart", 2)]
    assert len(llm.prompts) == 2  # two draws, no planning call
    assert any("draw 2" in p for p in llm.prompts)  # the lesson's own brief reaches the drawer


def test_lesson_without_planned_visuals_draws_nothing(monkeypatch):
    visuals, llm = run(monkeypatch, {}, lesson=lesson_with([None, None]))
    assert visuals == [] and llm.prompts == []


def test_ask_uses_the_planner_and_makes_exactly_one(monkeypatch):
    items = [
        VisualPlanItem(section_index=i, kind="diagram", purpose="p", brief="b") for i in range(2)
    ]
    visuals, llm = run(
        monkeypatch,
        {VisualPlan: [VisualPlan(items=items)], VisualDraft: by_skill},
        ask="show me how firewalls work",
    )
    assert len(visuals) == 1
    assert 'asked to see: "show me how firewalls work"' in llm.prompts[0]


def test_ask_section_is_clamped(monkeypatch):
    plan = VisualPlan(items=[VisualPlanItem(section_index=9, kind="chart", purpose="p", brief="b")])
    visuals, _ = run(monkeypatch, {VisualPlan: [plan], VisualDraft: by_skill}, ask="compare")
    assert visuals[0].section_index == len(FAKE_LESSON.sections) - 1


def test_ask_planner_may_return_nothing(monkeypatch):
    visuals, _ = run(monkeypatch, {VisualPlan: [VisualPlan()]}, ask="something odd")
    assert visuals == []


def test_rejected_draft_is_retried_with_the_reason(monkeypatch):
    bad = "<script>localStorage.x=1</script><svg></svg>"
    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft(bad), draft("```html\n" + GOOD["interactive"] + "\n```")]},
        lesson=lesson_with(["interactive"]),
    )
    assert len(visuals) == 1 and visuals[0].code == GOOD["interactive"]
    assert "rejected: no storage" in llm.prompts[1] and bad in llm.prompts[1]


def test_visual_dropped_after_max_attempts(monkeypatch):
    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft("nope")] * harness.MAX_ATTEMPTS},
        lesson=lesson_with(["diagram"]),
    )
    assert visuals == [] and len(llm.prompts) == harness.MAX_ATTEMPTS


def test_max_visuals_cap(monkeypatch):
    visuals, _ = run(
        monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["diagram"] * 4), max_visuals=2
    )
    assert [v.section_index for v in visuals] == [0, 1]


def test_drawer_uses_the_separate_visual_model_only_when_one_is_set(monkeypatch):
    asked = []
    monkeypatch.setattr(harness, "_llm", lambda model=None: asked.append(model) or "llm")
    harness.drawer_llm()
    monkeypatch.setenv("VISUAL_MODEL", "openrouter:anthropic/claude-haiku-5.5")
    harness.drawer_llm()
    assert asked == [None, "openrouter:anthropic/claude-haiku-5.5"]
