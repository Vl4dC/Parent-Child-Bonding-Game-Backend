import base64
import io
import json

import httpx
import pytest

from learning_service.lesson import LessonSection, SectionVisual
from learning_service.visuals import diagram_lint, harness, illustrate, render
from learning_service.visuals.models import (
    DiagramReview,
    Visual,
    VisualDraft,
    VisualPlan,
    VisualPlanItem,
    VisualRequest,
)
from tests.learning.conftest import FAKE_LESSON

GOOD = {
    "diagram": (
        "flowchart TD\n"
        '  sun["Sunlight"]:::start -->|"gives energy to"| leaf["Leaf"]:::key\n'
        '  water["Water"] -->|"travels up to"| leaf\n'
        '  leaf -->|"makes"| sugar["Sugar"]:::result\n'
        '  leaf -->|"releases"| air["Oxygen"]'
    ),
    "chart": json.dumps(
        {
            "data": {"values": [{"x": "A", "y": 1}]},
            "mark": "bar",
            "encoding": {"x": {"field": "x"}, "y": {"field": "y"}},
        }
    ),
    "interactive": '<input id="s" type="range"><svg viewBox="0 0 10 10"></svg><script>s.oninput=()=>{}</script>',
    "illustration": "data:image/webp;base64,UklGRhYAAABXRUJQVlA4",
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
        ("illustration", "https://example.com/a.webp", "data URL"),
        ("illustration", "data:image/svg+xml;base64,PHN2Zz4=", "data URL"),
        ("illustration", 'data:image/webp;base64,AAAA" onload="alert(1)', "data URL"),
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
    assert set(harness.SKILLS) == {"diagram", "chart", "interactive", "illustration"}
    for s in harness.SKILLS.values():
        assert s.description and "#" in s.body


def test_planner_sees_descriptions_not_bodies(monkeypatch):
    off = harness.plan_prompt(request(ask="show me a firewall"))
    assert harness.SKILLS["illustration"].description not in off  # switched off: not offered
    monkeypatch.setenv("ILLUSTRATIONS_ENABLED", "true")
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
    """Draw with a scripted model. The reviewer approves every diagram unless the script says otherwise."""
    llm = ScriptedLLM({DiagramReview: lambda prompt: DiagramReview(ok=True), **script})
    monkeypatch.setattr(harness, "_llm", lambda *args, **kwargs: llm)
    return harness.make_visuals(request(**kw)), llm


def draws(llm):
    """The prompts that asked a model to draw (not to review)."""
    return [p for p in llm.prompts if "You check a diagram" not in p]


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
    assert len(draws(llm)) == 2  # two draws, no planning call
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
    monkeypatch.setattr(
        harness,
        "_llm",
        lambda model=None, temperature=1.0: asked.append((model, temperature)) or "llm",
    )
    harness.drawer_llm()
    monkeypatch.setenv("VISUAL_MODEL", "openrouter:anthropic/claude-haiku-5.5")
    harness.drawer_llm()
    steady = harness.DRAW_TEMPERATURE  # drawing code is steadier than the chat default of 1.0
    assert asked == [(None, steady), ("openrouter:anthropic/claude-haiku-5.5", steady)]
    assert steady < 1.0


# ---------------------------------------------------------------- diagram quality
FRAGMENT = 'flowchart LR\n  a["Sun"] --> b["Leaf"]'
BETTER = GOOD["diagram"].replace("Oxygen", "Oxygen gas")


def reviews(llm):
    return [p for p in llm.prompts if "You check a diagram" in p]


def test_a_whole_mechanism_passes_and_other_diagram_types_are_left_alone():
    assert diagram_lint.problem(GOOD["diagram"]) is None  # the :::roles are not boxes
    assert diagram_lint.problem("mindmap\n  root((Water))\n    Ice") is None
    assert diagram_lint.problem("sequenceDiagram\n  A->>B: hi") is None


@pytest.mark.parametrize(
    "code, words",
    [
        (FRAGMENT, "only 2 boxes"),
        (GOOD["diagram"] + '\n  note["Just a note"]', "no arrows"),
        (GOOD["diagram"] + '\n  x["Cloud"] --> y["Rain"]', "separate pieces"),
        (
            "flowchart TD\n"
            + "\n".join(f'  n{i}["Step {i}"] --> n{i + 1}["Step {i + 1}"]' for i in range(12)),
            "too many",
        ),
        (GOOD["diagram"].replace('"Water"', '"' + "very " * 9 + 'long label"'), "label too long"),
        (GOOD["diagram"].replace('"makes"', '"' + "so " * 9 + 'it makes"'), "arrow label too long"),
    ],
)
def test_a_weak_flowchart_says_what_to_fix(code, words):
    assert words in diagram_lint.problem(code)


def test_a_label_mermaid_cannot_parse_is_a_hard_rejection():
    code = 'flowchart TD\n  a[Sun (star)] --> b["Leaf"]'
    assert "double quotes" in render.validate("diagram", code)
    assert render.validate("diagram", code.replace("[Sun (star)]", '["Sun (star)"]')) is None


def test_a_fragment_is_sent_back_with_the_reason(monkeypatch):
    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft(FRAGMENT), draft(GOOD["diagram"])]},
        lesson=lesson_with(["diagram"]),
    )
    assert [v.code for v in visuals] == [GOOD["diagram"]]
    assert "rejected: only 2 boxes" in draws(llm)[1] and FRAGMENT in draws(llm)[1]
    assert len(reviews(llm)) == 1  # only the structurally sound draft is worth a review


def test_the_reviewer_can_send_a_diagram_back(monkeypatch):
    verdicts = [
        DiagramReview(ok=False, problems=["Add where the water reaches the leaf", " "]),
        DiagramReview(ok=True),
    ]
    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft(GOOD["diagram"]), draft(BETTER)], DiagramReview: verdicts},
        lesson=lesson_with(["diagram"]),
    )
    assert [v.code for v in visuals] == [BETTER]
    assert "rejected: Add where the water reaches the leaf." in draws(llm)[1]
    asked = reviews(llm)[0]
    assert (
        "Section 0" in asked and "Body 0" in asked and GOOD["diagram"] in asked
    )  # judged against the lesson
    assert "whole mechanism" in asked


def test_a_failing_reviewer_never_costs_the_learner_their_diagram(monkeypatch):
    def down(prompt):
        raise RuntimeError("provider down")

    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft(GOOD["diagram"])], DiagramReview: down},
        lesson=lesson_with(["diagram"]),
    )
    assert [v.code for v in visuals] == [GOOD["diagram"]] and len(draws(llm)) == 1


def test_when_the_retries_run_out_the_last_drawable_diagram_is_kept(monkeypatch):
    visuals, llm = run(
        monkeypatch,
        {VisualDraft: [draft(FRAGMENT)] * harness.MAX_ATTEMPTS},
        lesson=lesson_with(["diagram"]),
    )
    assert [v.code for v in visuals] == [FRAGMENT]  # weak, but it draws: better than no diagram
    assert len(draws(llm)) == harness.MAX_ATTEMPTS


def test_no_review_is_paid_for_when_no_redraw_is_left(monkeypatch):
    picky = DiagramReview(ok=False, problems=["Add another step"])
    visuals, llm = run(
        monkeypatch,
        {
            VisualDraft: [draft(GOOD["diagram"])] * harness.MAX_ATTEMPTS,
            DiagramReview: lambda p: picky,
        },
        lesson=lesson_with(["diagram"]),
    )
    assert len(visuals) == 1 and len(reviews(llm)) == harness.MAX_ATTEMPTS - 1


def test_charts_and_interactives_are_not_reviewed(monkeypatch):
    _, llm = run(monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["chart", "interactive"]))
    assert reviews(llm) == []


def test_the_diagram_page_uses_the_app_colours_and_stands_a_wide_chain_up():
    doc = render.document(visual("diagram"), "v")
    assert (
        'theme: "base"' in doc and 'v("--accent")' in doc
    )  # the page's own colours, light and dark
    assert "classDef start" in doc and "classDef key" in doc and "classDef result" in doc
    assert '"$1 TD"' in doc  # a left-to-right chain wider than the page is redrawn top to bottom
    assert "prefers-color-scheme: dark" in doc


# ---------------------------------------------------------------- illustrations
def illustrations_on(monkeypatch, per_lesson=2):
    monkeypatch.setenv("ILLUSTRATIONS_ENABLED", "true")
    monkeypatch.setenv("ILLUSTRATIONS_PER_LESSON", str(per_lesson))


def test_illustration_document_is_an_escaped_img_without_libraries():
    doc = render.document(visual("illustration", alt='a "seed"<b>'), "v")
    assert f'<img class="illustration" src="{GOOD["illustration"]}"' in doc
    assert 'alt="a &quot;seed&quot;&lt;b&gt;"' in doc
    assert "cdn.jsdelivr.net" not in doc and "img-src data:" in doc


def test_illustration_prompt_has_house_style_age_rules_and_no_text():
    young = illustrate.prompt("a seed sprouting", "plants", 9)
    older = illustrate.prompt("a seed sprouting", "plants", 16)
    for p in (young, older):
        assert "Show: a seed sprouting" in p and "no text, letters, numbers" in p
        assert "nothing scary, violent, gory or suggestive" in p
    assert "picture-book" in young and "science-textbook" in older


def test_to_webp_shrinks_wide_images():
    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (2048, 1536), "green").save(raw, "PNG")
    img = Image.open(io.BytesIO(illustrate.to_webp(raw.getvalue())))
    assert img.format == "WEBP" and img.size == (1024, 768)
    with pytest.raises(illustrate.IllustrationFailed):
        illustrate.to_webp(b"not an image")


def fake_image_api(monkeypatch, response=None, error=None):
    """Replace the HTTP call to the image endpoint; returns the request bodies it received."""
    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (64, 48), "blue").save(raw, "JPEG")
    body = response or {
        "data": [
            {"b64_json": base64.b64encode(raw.getvalue()).decode(), "media_type": "image/jpeg"}
        ],
        "usage": {"cost": 0.03},
    }
    sent = []

    def post(url, headers, json, timeout):
        sent.append(json)
        if error:
            raise error
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(illustrate.httpx, "post", post)
    illustrate._generate.cache_clear()
    return sent


def test_illustration_is_drawn_with_one_image_call_and_no_chat_model(monkeypatch):
    illustrations_on(monkeypatch)
    sent = fake_image_api(monkeypatch)
    visuals, llm = run(
        monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["illustration", "chart"])
    )
    assert [v.kind for v in visuals] == ["illustration", "chart"]
    pic = visuals[0]
    assert render.validate("illustration", pic.code) is None
    assert pic.code.startswith("data:image/webp;base64,")
    assert (pic.title, pic.caption, pic.alt) == ("Section 0", "p", "draw 0")
    assert (
        len(sent) == 1 and "Show: draw 0" in sent[0]["prompt"] and sent[0]["aspect_ratio"] == "4:3"
    )
    assert len(llm.prompts) == 1  # only the chart went to the chat model


def test_same_illustration_is_not_requested_twice(monkeypatch):
    illustrations_on(monkeypatch)
    sent = fake_image_api(monkeypatch)
    first = illustrate.illustrate("a seed", "plants", 10)
    assert illustrate.illustrate("a seed", "plants", 10) == first and len(sent) == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"error": httpx.ConnectTimeout("slow")},
        {"response": {"data": []}},
        {"response": {"data": [{"b64_json": base64.b64encode(b"junk").decode()}]}},
    ],
)
def test_failed_illustration_is_dropped_not_fatal(monkeypatch, kw):
    illustrations_on(monkeypatch)
    fake_image_api(monkeypatch, **kw)
    visuals, _ = run(
        monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["illustration", "diagram"])
    )
    assert [v.kind for v in visuals] == ["diagram"]


def test_illustrations_switched_off_are_never_requested(monkeypatch):
    sent = fake_image_api(monkeypatch)
    visuals, _ = run(monkeypatch, {VisualDraft: by_skill}, lesson=lesson_with(["illustration"]))
    assert visuals == [] and sent == []


def test_missing_api_key_fails_cleanly(monkeypatch):
    illustrations_on(monkeypatch)
    fake_image_api(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(illustrate.IllustrationFailed, match="OPENROUTER_API_KEY"):
        illustrate.illustrate("a seed", "plants", 10)
