import learning_service.store as store
from tests.learning.conftest import FAKE_QUIZ


# ---------------------------------------------------------------- learning
def test_create_lesson(client):
    r = client.post("/api/lessons", json={"topic": "  computer networks ", "age": 12})
    assert r.status_code == 201
    body = r.json()
    assert body["topic"] == "computer networks" and body["age"] == 12
    assert body["title"] == "How Networks Talk"
    assert [s["heading"] for s in body["sections"]] == ["Addresses", "Rules"]
    assert len(body["key_points"]) == 3
    assert client.seen["lesson_request"].age == 12


def test_get_lesson(client, lesson_id):
    r = client.get(f"/api/lessons/{lesson_id}")
    assert r.status_code == 200 and r.json()["id"] == lesson_id
    assert client.get("/api/lessons/nope").status_code == 404


def test_lesson_bad_input_is_422(client):
    assert client.post("/api/lessons", json={"topic": "   ", "age": 12}).status_code == 422
    assert client.post("/api/lessons", json={"topic": "x", "age": 4}).status_code == 422
    assert client.post("/api/lessons", json={"topic": "x", "age": 100}).status_code == 422
    assert client.post("/api/lessons", json={"topic": "x"}).status_code == 422


def test_lesson_failure_is_502(client, monkeypatch):
    import learning_service.routes as app_module

    def boom(_request):
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(app_module, "make_lesson", boom)
    r = client.post("/api/lessons", json={"topic": "x", "age": 10})
    assert r.status_code == 502 and "invalid api key" not in r.json()["detail"]


# ---------------------------------------------------------------- quiz
def create_quiz(client, lesson_id, n=3):
    r = client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": n})
    assert r.status_code == 201, r.text
    return r.json()


def test_quiz_is_built_from_lesson_and_age(client, lesson_id):
    create_quiz(client, lesson_id, n=4)
    req = client.seen["quiz_request"]
    assert req.age == 12
    assert req.num_questions == 4
    assert req.allowed_types == ["multiple_choice", "true_false"]
    assert "AWS reserves 5 IPs per subnet" in req.source  # lesson text handed to the agent
    assert "computer networks" in req.prompt


def test_quiz_hides_answers_and_drops_unresolved(client, lesson_id):
    body = create_quiz(client, lesson_id)
    assert body["lesson_id"] == lesson_id
    assert body["dropped"] == 1
    assert [q["index"] for q in body["questions"]] == [0, 1]
    assert body["questions"][1]["options"] == ["True", "False"]
    text = str(body)
    assert "answer" not in text and "explanation" not in text and "AWS reserves" not in text
    assert client.get(f"/api/quiz/{body['id']}").json() == body


def test_quiz_for_unknown_lesson_is_404(client):
    assert client.post("/api/lessons/nope/quiz", json={}).status_code == 404


def test_check_correct_and_incorrect(client, lesson_id):
    qid = create_quiz(client, lesson_id)["id"]
    right = client.post(f"/api/quiz/{qid}/check", json={"index": 0, "choice": 1}).json()
    assert right == {
        "correct": True,
        "answer_index": 1,
        "explanation": "AWS reserves 5.",
        "answer_visual_html": None,
    }
    wrong = client.post(f"/api/quiz/{qid}/check", json={"index": 1, "choice": 0}).json()
    assert wrong == {
        "correct": False,
        "answer_index": 1,
        "explanation": "NACLs are stateless.",
        "answer_visual_html": None,
    }


def test_check_bad_input(client, lesson_id):
    qid = create_quiz(client, lesson_id)["id"]
    assert client.post(f"/api/quiz/{qid}/check", json={"index": 5, "choice": 0}).status_code == 422
    assert client.post(f"/api/quiz/{qid}/check", json={"index": 1, "choice": 2}).status_code == 422
    assert client.post("/api/quiz/nope/check", json={"index": 0, "choice": 0}).status_code == 404


def test_quiz_count_out_of_range_is_422(client, lesson_id):
    assert (
        client.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 16}).status_code == 422
    )


def test_quiz_agent_failure_is_502(client, lesson_id, monkeypatch):
    def boom(_request):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(store, "make_quiz", boom)
    r = client.post(f"/api/lessons/{lesson_id}/quiz", json={})
    assert r.status_code == 502 and "rate limited" not in r.json()["detail"]


def test_all_unresolved_is_502(client, lesson_id, monkeypatch):
    bad = FAKE_QUIZ.model_copy(deep=True, update={"unresolved": [0, 1, 2]})
    monkeypatch.setattr(store, "make_quiz", lambda _r: bad)
    r = client.post(f"/api/lessons/{lesson_id}/quiz", json={})
    assert r.status_code == 502 and "reliable" in r.json()["detail"]


def test_api_documentation_served(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200 and "/api/lessons" in r.json()["paths"]


# ---------------------------------------------------------------- visuals
def test_auto_visuals_are_stored_and_rendered(client, lesson_id):
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    assert r.status_code == 201
    [v] = r.json()["visuals"]
    assert (
        v["kind"] == "diagram"
        and v["section_index"] == 1
        and v["title"] == "How a firewall decides"
    )
    assert v["html"].startswith("<!doctype html>") and "Content-Security-Policy" in v["html"]
    req = client.seen["visual_request"]
    assert req.ask is None and req.age == 12 and req.lesson.title == "How Networks Talk"
    assert client.get(f"/api/lessons/{lesson_id}").json()["visuals"] == [v]


def test_illustration_is_stored_in_the_lesson_and_served_as_an_img(client, lesson_id, monkeypatch):
    import base64
    import io

    from PIL import Image

    import learning_service.routes as app_module
    from learning_service.visuals.illustrate import to_webp
    from learning_service.visuals.models import Visual

    raw = io.BytesIO()
    Image.new("RGB", (1200, 900), "orange").save(raw, "PNG")
    code = "data:image/webp;base64," + base64.b64encode(to_webp(raw.getvalue())).decode()
    pic = Visual(
        kind="illustration",
        section_index=0,
        title="Addresses",
        caption="Every device has an address.",
        alt="a row of houses, each with a mailbox",
        code=code,
    )
    monkeypatch.setattr(app_module, "make_visuals", lambda _request: [pic])
    [v] = client.post(f"/api/lessons/{lesson_id}/visuals", json={}).json()["visuals"]
    assert v["kind"] == "illustration" and f'src="{code}"' in v["html"]
    assert client.get(f"/api/lessons/{lesson_id}").json()["visuals"] == [
        v
    ]  # stored with the lesson
    page = client.get(f"/api/lessons/{lesson_id}/visuals/{v['id']}/view")
    assert page.status_code == 200 and '<img class="illustration"' in page.text


def test_ask_is_passed_and_visuals_accumulate(client, lesson_id):
    client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "  show me a firewall  "})
    assert client.seen["visual_request"].ask == "show me a firewall"
    visuals = client.get(f"/api/lessons/{lesson_id}").json()["visuals"]
    assert len(visuals) == 2 and visuals[0]["id"] != visuals[1]["id"]


def test_blank_ask_means_auto(client, lesson_id):
    client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "   "})
    assert client.seen["visual_request"].ask is None


def test_visuals_errors(client, lesson_id, monkeypatch):
    import learning_service.routes as app_module

    assert client.post("/api/lessons/nope/visuals", json={}).status_code == 404
    assert (
        client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "x" * 301}).status_code == 422
    )

    def boom(_request):
        raise RuntimeError("model down")

    monkeypatch.setattr(app_module, "make_visuals", boom)
    r = client.post(f"/api/lessons/{lesson_id}/visuals", json={})
    assert r.status_code == 502 and "model down" not in r.json()["detail"]


def test_auto_visuals_draw_only_once_per_lesson(client, lesson_id):
    first = client.post(f"/api/lessons/{lesson_id}/visuals", json={}).json()["visuals"]
    second = client.post(f"/api/lessons/{lesson_id}/visuals", json={}).json()["visuals"]
    assert len(first) == 1 and second == []
    asked = client.post(f"/api/lessons/{lesson_id}/visuals", json={"ask": "more please"}).json()[
        "visuals"
    ]
    assert len(asked) == 1  # learner requests always draw


def test_failed_auto_visuals_can_be_retried(client, lesson_id, monkeypatch):
    import learning_service.routes as app_module

    real = app_module.make_visuals

    def boom(_request):
        raise RuntimeError("down")

    monkeypatch.setattr(app_module, "make_visuals", boom)
    assert client.post(f"/api/lessons/{lesson_id}/visuals", json={}).status_code == 502
    monkeypatch.setattr(app_module, "make_visuals", real)
    assert len(client.post(f"/api/lessons/{lesson_id}/visuals", json={}).json()["visuals"]) == 1


def test_prefers_visuals_reaches_the_lesson_writer(client):
    client.post("/api/lessons", json={"topic": "volcanoes", "age": 11, "prefers_visuals": True})
    assert client.seen["lesson_request"].prefers_visuals is True


def test_lesson_exposes_planned_visuals(client, monkeypatch):
    import learning_service.routes as app_module
    from learning_service.lesson import SectionVisual
    from tests.learning.conftest import FAKE_LESSON

    planned = FAKE_LESSON.model_copy(deep=True)
    planned.sections[1].visual = SectionVisual(
        kind="diagram", purpose="See the firewall decide", brief="b"
    )
    monkeypatch.setattr(app_module, "make_lesson", lambda _r: planned)
    body = client.post("/api/lessons", json={"topic": "networks", "age": 12}).json()
    assert body["sections"][0]["visual"] is None
    assert body["sections"][1]["visual"]["purpose"] == "See the firewall decide"
