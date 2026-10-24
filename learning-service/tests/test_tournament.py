def create(client, lesson_id, n=3):
    r = client.post("/api/tournament", json={"lesson_id": lesson_id, "num_questions": n})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_and_get(client, lesson_id):
    body = create(client, lesson_id)
    assert body["lesson_id"] == lesson_id and len(body["questions"]) == 2
    assert "explanation" not in str(body)
    assert client.get(f"/api/tournament/{body['id']}").json() == body


def test_submit_scores(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    r = client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 0]})
    assert r.status_code == 200
    body = r.json()
    assert body["score"] == 1 and body["total"] == 2
    assert body["results"][0] == {"correct": True, "answer_index": 1, "explanation": "AWS reserves 5.", "answer_visual_html": None,
                                  "index": 0, "choice": 1}
    assert body["results"][1]["correct"] is False


def test_submit_bad_input(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [1]}).status_code == 422
    assert client.post(f"/api/tournament/{tid}/submit", json={"answers": [1, 9]}).status_code == 422


def test_unknown_ids_are_404(client):
    assert client.post("/api/tournament", json={"lesson_id": "nope"}).status_code == 404
    assert client.get("/api/tournament/nope").status_code == 404


def test_tournament_and_practice_ids_do_not_mix(client, lesson_id):
    tid = create(client, lesson_id)["id"]
    qid = client.post(f"/api/lessons/{lesson_id}/quiz", json={}).json()["id"]
    assert client.post(f"/api/quiz/{tid}/check", json={"index": 0, "choice": 1}).status_code == 404
    assert client.post(f"/api/tournament/{qid}/submit", json={"answers": [1, 0]}).status_code == 404
