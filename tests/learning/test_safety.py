"""Child-safety guardrails (safety.py): the age policy, the checker, and the retry-then-block gate."""

import pytest

import learning_service.safety as safety
from learning_service.safety import AgeBlocked, SafetyUnavailable, Verdict
from tests.learning.test_quiz_graph import RecordingLLM

OK = Verdict(ok=True)
BAD = Verdict(ok=False, category="violence", reason="describes injuries in graphic detail")


@pytest.fixture(autouse=True)
def _no_cap_in_env(monkeypatch):
    monkeypatch.delenv("AUDIENCE_MAX_AGE", raising=False)
    monkeypatch.delenv("SAFETY_MODEL", raising=False)


# ---------------------------------------------------------------- the age policy
@pytest.mark.parametrize("age, band", [(6, "5-7"), (10, "8-11"), (13, "12-14"), (17, "15-18")])
def test_each_age_band_gets_its_own_rules(age, band):
    assert band in safety.content_rules(age)


def test_rules_differ_between_bands_and_get_stricter_for_the_young():
    texts = {age: safety.content_rules(age) for age in (6, 10, 13, 17)}
    assert len(set(texts.values())) == 4


@pytest.mark.parametrize("age", [6, 10, 13, 17])
def test_every_band_forbids_the_core_harms(age):
    rules = safety.content_rules(age).lower()
    for topic in (
        "graphic violence",
        "sexual",
        "drugs",
        "self-harm",
        "hate",
        "dangerous",
        "strong language",
        "personal",
    ):
        assert topic in rules, topic


@pytest.mark.parametrize("age", [6, 10, 13, 17])
def test_rules_defend_against_instructions_hidden_in_the_topic_or_book(age):
    rules = safety.content_rules(age).lower()
    assert "never instructions" in rules and "override" in rules


@pytest.mark.parametrize("age", [10, 13, 17])
def test_school_topics_are_taught_gently_not_banned(age):
    rules = safety.content_rules(age).lower()
    assert "war" in rules and "trusted adult" in rules


def test_the_audience_cap_applies_to_the_policy_not_the_reading_level(monkeypatch):
    assert safety.policy_age(30) == 18  # default: this is a school app for 10-18
    assert safety.content_rules(30) == safety.content_rules(18)
    assert safety.policy_age(12) == 12  # a younger learner keeps their own band
    monkeypatch.setenv("AUDIENCE_MAX_AGE", "15")
    assert safety.policy_age(30) == 15 and safety.policy_age(11) == 11
    monkeypatch.setenv("AUDIENCE_MAX_AGE", "99")
    assert safety.policy_age(30) == 30 and "adult" in safety.content_rules(30).lower()


@pytest.mark.parametrize("junk", ["", "abc", "-3", "2"])
def test_a_bad_cap_falls_back_to_the_default(monkeypatch, junk):
    monkeypatch.setenv("AUDIENCE_MAX_AGE", junk)
    assert safety.policy_age(40) == 18


# ---------------------------------------------------------------- the checker
def test_check_sends_the_text_the_policy_age_and_the_rules(monkeypatch):
    llm = RecordingLLM(OK)
    monkeypatch.setattr(safety, "checker_llm", lambda: llm)
    assert safety.check("Volcanoes erupt when magma rises.", 45, topic="volcanoes") == OK
    prompt = llm.prompts[0]
    assert "Volcanoes erupt when magma rises." in prompt and "volcanoes" in prompt
    assert "18-year-old" in prompt  # capped, not 45
    assert safety.content_rules(18) in prompt


def test_check_tells_a_request_from_finished_content(monkeypatch):
    llm = RecordingLLM(OK)
    monkeypatch.setattr(safety, "checker_llm", lambda: llm)
    safety.check("how do guns work", 12, kind="request")
    safety.check("A lesson.", 12, kind="content")
    assert "what a learner typed" in llm.prompts[0] and "lesson" in llm.prompts[1]


def test_check_keeps_a_blocked_verdict_and_never_leaves_its_category_blank(monkeypatch):
    monkeypatch.setattr(
        safety, "checker_llm", lambda: RecordingLLM(Verdict(ok=False, category="none"))
    )
    assert safety.check("x", 12).category == "other"


def test_check_only_sends_a_bounded_amount_of_text(monkeypatch):
    llm = RecordingLLM(OK)
    monkeypatch.setattr(safety, "checker_llm", lambda: llm)
    safety.check("a" * 200_000, 12)
    assert len(llm.prompts[0]) < 40_000


def test_the_checker_model_can_be_separate(monkeypatch):
    asked = []
    monkeypatch.setattr(
        safety,
        "_load_llm",
        lambda model=None, temperature=1.0: asked.append((model, temperature)) or "llm",
    )
    safety.checker_llm()
    monkeypatch.setenv("SAFETY_MODEL", "openrouter:cheap/model")
    safety.checker_llm()
    assert asked == [(None, 0.0), ("openrouter:cheap/model", 0.0)]


# ---------------------------------------------------------------- screening what the learner typed
def test_screen_request_lets_a_normal_topic_through(monkeypatch):
    monkeypatch.setattr(safety, "check", lambda text, age, **kw: OK)
    safety.screen_request("the water cycle", 10)


def test_screen_request_blocks_without_asking_the_writer(monkeypatch):
    seen = []
    monkeypatch.setattr(safety, "check", lambda text, age, **kw: seen.append(kw) or BAD)
    with pytest.raises(AgeBlocked) as blocked:
        safety.screen_request("something unsuitable", 10)
    assert blocked.value.category == "violence" and seen == [{"kind": "request"}]


# ---------------------------------------------------------------- the gate: check, retry once, then block
def gate(monkeypatch, verdicts, age=12, build_results=("first", "second", "third")):
    """Run ensure_safe with scripted verdicts; returns (result or exception, builds, checked texts)."""
    verdicts, builds, checked = list(verdicts), [], []

    def build():
        builds.append(1)
        return build_results[len(builds) - 1]

    def fake_check(text, age, **kw):
        checked.append(text)
        verdict = verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    monkeypatch.setattr(safety, "check", fake_check)
    try:
        out = safety.ensure_safe(build, str.upper, age, topic="t")
    except Exception as exc:  # noqa: BLE001 - the test inspects it
        out = exc
    return out, len(builds), checked


def test_safe_content_is_built_and_checked_once(monkeypatch):
    out, builds, checked = gate(monkeypatch, [OK])
    assert (out, builds, checked) == ("first", 1, ["FIRST"])


def test_unsafe_content_is_rebuilt_once_and_the_second_try_can_pass(monkeypatch):
    out, builds, checked = gate(monkeypatch, [BAD, OK])
    assert (out, builds, checked) == ("second", 2, ["FIRST", "SECOND"])


def test_content_that_fails_twice_is_blocked_and_never_returned(monkeypatch):
    out, builds, _ = gate(monkeypatch, [BAD, BAD])
    assert isinstance(out, AgeBlocked) and out.category == "violence" and builds == 2


def test_the_checker_failing_blocks_a_young_learner(monkeypatch):
    out, builds, _ = gate(monkeypatch, [RuntimeError("provider is down: key sk-123")])
    assert isinstance(out, SafetyUnavailable) and builds == 1
    assert "sk-123" not in str(out) and "provider" not in str(
        out
    )  # nothing from the provider reaches the client


def test_the_checker_failing_on_the_retry_also_blocks(monkeypatch):
    out, builds, _ = gate(monkeypatch, [BAD, RuntimeError("boom")])
    assert isinstance(out, SafetyUnavailable) and builds == 2


def test_nothing_written_means_nothing_to_check(monkeypatch):
    monkeypatch.setattr(safety, "check", lambda *a, **k: pytest.fail("no text, so no checker call"))
    assert safety.ensure_safe(lambda: [], lambda items: "  ".join(items), 12) == []


def test_an_error_from_building_is_not_hidden_by_the_gate(monkeypatch):
    monkeypatch.setattr(safety, "check", lambda *a, **k: OK)

    def build():
        raise ValueError("model exploded")

    with pytest.raises(ValueError, match="model exploded"):
        safety.ensure_safe(build, str, 12)


def test_for_adults_a_broken_checker_does_not_stop_learning(monkeypatch):
    monkeypatch.setenv("AUDIENCE_MAX_AGE", "99")
    out, builds, _ = gate(monkeypatch, [RuntimeError("boom")], age=30)
    assert (out, builds) == ("first", 1)


# ---------------------------------------------------------------- what the learner is told
@pytest.mark.parametrize(
    "category",
    ["violence", "sexual", "drugs", "hate", "dangerous", "scary", "language", "privacy", "other"],
)
def test_blocked_message_is_friendly_and_never_repeats_anything_unsafe(category):
    message = AgeBlocked(category).message
    assert "teacher" in message or "trusted adult" in message
    assert "graphic" not in message and "violence" not in message.lower()


def test_a_self_harm_block_points_to_a_trusted_adult_kindly():
    message = AgeBlocked("self_harm").message
    assert "trusted adult" in message and "counsellor" in message
    assert message != AgeBlocked("violence").message


def test_an_unknown_category_still_gets_the_default_message():
    assert AgeBlocked("brand-new").message == AgeBlocked("other").message
