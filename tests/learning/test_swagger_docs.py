"""Swagger UI (/docs) is how the backend is driven by hand: keep it complete, and keep /view pages safe."""

import pytest

import learning_service.routes as app_module
from learning_service.api_docs import TAGS
from tests.learning.test_followup_api import answer, make_quiz, quiz_deck
from tests.learning.test_learning_modes_api import with_diagram

MODEL_CALLS = {
    ("post", "/api/lessons"),
    ("post", "/api/materials"),
    ("post", "/api/lessons/{lesson_id}/visuals"),
    ("post", "/api/lessons/{lesson_id}/quiz"),
    ("post", "/api/quiz/{quiz_id}/followup"),
    ("post", "/api/lessons/{lesson_id}/flashcards"),
    ("post", "/api/tournament"),
    ("post", "/api/chat"),
    ("post", "/api/chat/image"),
    ("post", "/api/challenges"),
    ("post", "/api/weekly/next/prepare"),
}


@pytest.fixture(scope="module")
def spec():
    from app.config import Settings
    from app.main import create_app

    return create_app(Settings(app_env="test", demo_mode=True, _env_file=None)).openapi()


def operations(spec):
    return [
        (method, path, op)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if path
        in {
            getattr(r, "path", "")
            for r in [*app_module.app.routes, *app_module.tournament.router.routes]
        }
    ]


# ---------------------------------------------------------------- the docs themselves
def test_the_page_has_a_walkthrough_and_ordered_tags(spec):
    assert "Try it in this order" in spec["info"]["description"]
    assert (
        [t["name"] for t in spec["tags"]]
        == [t["name"] for t in TAGS]
        == [
            "Your book",
            "Chat",
            "Memory",
            "Challenges",
            "Matches",
            "Weekly",
            "Learn",
            "Quiz",
            "Flashcards",
            "Weak spots",
            "Tournament",
        ]
    )
    assert all(t["description"] for t in spec["tags"])


def test_every_operation_is_tagged_summarised_and_described(spec):
    declared = {t["name"] for t in TAGS}
    for method, path, op in operations(spec):
        where = f"{method.upper()} {path}"
        assert op.get("tags") and set(op["tags"]) <= declared, where
        assert op.get("summary"), where
        assert len(op.get("description", "")) >= 30, where


def test_every_request_body_has_examples_to_try(spec):
    for method, path, op in operations(spec):
        body = op.get("requestBody")
        if body and "multipart/form-data" in body["content"]:
            assert "PDF" in op["description"], (
                f"{method.upper()} {path}: say what file to upload"
            )  # Swagger shows a file picker
        elif body:
            examples = body["content"]["application/json"].get("examples")
            assert examples, f"{method.upper()} {path} has no example to try"


def test_upload_fields_get_a_file_picker_in_swagger(spec):
    """Swagger UI draws a file picker only for `format: binary`; the default schema made it a text box."""
    schema = spec["components"]["schemas"]["Body_upload_material_api_materials_post"]
    assert schema["properties"]["files"]["items"] == {"type": "string", "format": "binary"}
    assert "contentMediaType" not in str(spec)


def test_error_codes_are_documented(spec):
    for method, path, op in operations(spec):
        codes = set(op["responses"])
        if "{" in path:
            assert "404" in codes, f"{method.upper()} {path}: unknown ids give 404"
        if (method, path) in MODEL_CALLS:
            assert "502" in codes, f"{method.upper()} {path}: model failures give 502"
    followup = spec["paths"]["/api/quiz/{quiz_id}/followup"]["post"]["responses"]
    assert {"404", "409", "502"} <= set(followup)
    assert "409" in spec["paths"]["/api/quiz/{quiz_id}/flashcards"]["post"]["responses"]


def test_swagger_ui_is_served_with_try_it_out_on(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text
    assert '"tryItOutEnabled": true' in r.text and '"displayRequestDuration": true' in r.text


# ---------------------------------------------------------------- /view pages
def assert_sandboxed_page(r):
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert (
        r.headers["content-security-policy"] == "sandbox allow-scripts"
    )  # model-written code gets an opaque origin
    assert r.text.startswith("<!doctype html>")


def test_lesson_visual_view(client, lesson_id):
    with_diagram(client, lesson_id)
    visual = client.get(f"/api/lessons/{lesson_id}").json()["visuals"][0]
    r = client.get(f"/api/lessons/{lesson_id}/visuals/{visual['id']}/view")
    assert_sandboxed_page(r)
    assert "Firewall" in r.text and r.text == visual["html"]
    assert client.get(f"/api/lessons/{lesson_id}/visuals/nope/view").status_code == 404
    assert client.get("/api/lessons/nope/visuals/x/view").status_code == 404


def picture_question(client, lesson_id):
    with_diagram(client, lesson_id)
    quiz = make_quiz(client, lesson_id, n=4)
    return quiz, next(q for q in quiz["questions"] if q["visual_html"])


def test_question_view_hides_the_box_until_the_question_is_answered(client, lesson_id):
    quiz, q = picture_question(client, lesson_id)
    url = f"/api/quiz/{quiz['id']}/questions/{q['index']}/view"
    hidden = client.get(url)
    assert_sandboxed_page(hidden)
    assert "Firewall" not in hidden.text  # the label isn't leaked
    early = client.get(url + "?show=answer")
    assert early.status_code == 409 and "Answer the question first" in early.json()["detail"]
    answer(client, quiz["id"], q["index"], 0)
    done = client.get(url + "?show=answer")
    assert_sandboxed_page(done)
    assert "Firewall" in done.text  # completed diagram, once answered


def test_question_view_errors(client, lesson_id):
    quiz, q = picture_question(client, lesson_id)
    text_question = next(x for x in quiz["questions"] if not x["visual_html"])
    assert (
        client.get(f"/api/quiz/{quiz['id']}/questions/{text_question['index']}/view").status_code
        == 404
    )
    assert client.get(f"/api/quiz/{quiz['id']}/questions/99/view").status_code == 422
    assert (
        client.get(f"/api/quiz/{quiz['id']}/questions/{q['index']}/view?show=both").status_code
        == 422
    )
    assert client.get("/api/quiz/nope/questions/0/view").status_code == 404


def test_card_view_front_and_back(client, lesson_id):
    quiz, q = picture_question(client, lesson_id)
    wrong = next(i for i, o in enumerate(q["options"]) if o != "Firewall")
    answer(client, quiz["id"], q["index"], wrong)
    deck = quiz_deck(client, quiz["id"]).json()
    url = f"/api/flashcards/{deck['id']}/cards/0/view"
    front, back = client.get(url), client.get(url + "?side=back")
    assert_sandboxed_page(front)
    assert_sandboxed_page(back)
    assert "Firewall" not in front.text and "Firewall" in back.text
    assert client.get(f"/api/flashcards/{deck['id']}/cards/9/view").status_code == 404
    assert client.get(url + "?side=middle").status_code == 422
    assert client.get("/api/flashcards/nope/cards/0/view").status_code == 404


def test_card_without_a_diagram_has_nothing_to_view(client, lesson_id):
    deck = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 5}).json()
    r = client.get(f"/api/flashcards/{deck['id']}/cards/0/view")
    assert r.status_code == 404 and "no diagram" in r.json()["detail"]
