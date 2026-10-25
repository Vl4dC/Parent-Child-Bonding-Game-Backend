def test_restart_preserves_the_old_pass_and_replays_once(client, lesson_id):
    response = client.post(f"/api/lessons/{lesson_id}/flashcards", json={"num_cards": 8})
    assert response.status_code == 201, response.text
    old = response.json()
    reviewed = client.post(
        f"/api/flashcards/{old['id']}/review", json={"card": old["state"]["next"], "result": "know"}
    )
    assert reviewed.status_code == 200
    headers = {"Idempotency-Key": "restart-same-pass"}
    result = client.post(
        f"/api/flashcards/{old['id']}/restart", json={"expected_reviews": 1}, headers=headers
    )
    assert result.status_code == 200, result.text
    fresh = result.json()
    assert fresh["id"] != old["id"] and fresh["state"]["reviews"] == 0
    assert fresh["cards"] == old["cards"]
    assert (
        client.post(
            f"/api/flashcards/{old['id']}/restart", json={"expected_reviews": 1}, headers=headers
        ).json()["id"]
        == fresh["id"]
    )
    assert (
        client.post(
            f"/api/flashcards/{old['id']}/restart", json={"expected_reviews": 0}, headers=headers
        ).status_code
        == 409
    )
    assert client.get(f"/api/flashcards/{old['id']}").json()["state"]["reviews"] == 1
    assert (
        client.post(
            f"/api/flashcards/{old['id']}/restart",
            json={"expected_reviews": 1},
            headers=headers | {"X-Player-ID": "00000000-0000-4000-8000-000000000002"},
        ).status_code
        == 404
    )
