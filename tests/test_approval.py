import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.approval_mail import deliver_one
from app.approval_service import ApprovalService
from app.config import Settings
from app.main import create_app
from app.repository import PostgresRepository

SECRET = "synthetic-test-only-approval-secret-" * 2
PAYLOAD = {
    "learnerDisplayName": "Sam",
    "parentEmail": "guardian@example.com",
    "learnerBirthYear": 2014,
}
CANONICAL = {
    "learner_display_name": "Sam",
    "guardian_email": "guardian@example.com",
    "learner_birth_year": 2014,
}


@pytest.fixture
def approval(repository):
    # A separate namespace table is intentionally not reset by older learning fixtures.
    if repository.durable:
        with repository.control() as connection:
            connection.execute("truncate api_private.approval_records")
    now = [1_800_000_000]
    service = ApprovalService(repository, SECRET, clock=lambda: now[0])
    user_id = str(uuid4())
    identities = {
        "guardian": {
            "id": user_id,
            "email": "guardian@example.com",
            "email_confirmed_at": "2026-10-24T12:00:00Z",
            "user_metadata": {"account_type": "learner"},
        },
        "other": {
            "id": str(uuid4()),
            "email": "other@example.com",
            "email_confirmed_at": "2026-10-24T12:00:00Z",
            "user_metadata": {"account_type": "parent"},
        },
        "unverified": {
            "id": str(uuid4()),
            "email": "guardian@example.com",
            "email_confirmed_at": None,
            "user_metadata": {"account_type": "parent"},
        },
    }

    def upstream(request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in identities:
            return httpx.Response(401, json={"error_code": "bad_jwt"})
        return httpx.Response(200, json=identities[token])

    settings = Settings(
        app_env="test",
        supabase_url="https://supabase.example.com",
        supabase_publishable_key="sb_publishable_synthetic_test",
        learner_approval_enabled=True,
        learner_approval_secret=SECRET,
        _env_file=None,
    )
    application = create_app(
        settings, transport=httpx.MockTransport(upstream), repository=repository
    )
    # Freeze the route factory's service too, while leaving actual verified identity calls intact.
    import app.approval_api as routes

    original = routes.service
    routes.service = lambda request: service
    try:
        with TestClient(application) as client:
            yield client, service, now, identities
    finally:
        routes.service = original


def create(client, key="registration-key-1", payload=None):
    response = client.post(
        "/auth/registration/learners",
        json=payload or PAYLOAD,
        headers={"Idempotency-Key": key.ljust(32, "_")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def record(service, request_id):
    with service.store.transaction() as tx:
        return tx.get("requests", request_id)


def decision(
    client,
    request_id,
    proof,
    *,
    version=1,
    action="approve",
    key="decision-key-1",
    token="guardian",
):
    return client.post(
        f"/api/guardian/registration-requests/{request_id}/decision",
        json={"decision": action, "expected_version": version, "proof": proof},
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
    )


def authorize(service, request_id, identities):
    service.grant_authority(request_id, identities["guardian"]["id"], "synthetic-case-evidence")


def test_request_retry_receipt_and_private_storage(approval):
    client, service, _, _ = approval
    created = create(client)
    assert create(client) == created
    assert created["status"] == "pending" and created["activated"] is False
    rid = created["request_id"]
    response = client.get(
        f"/auth/registration/requests/{rid}",
        headers={"X-Registration-Receipt": created["receipt"]},
    )
    assert response.status_code == 200
    assert set(response.json()) == {"request_id", "status", "version", "expires_at", "activated"}
    assert "guardian@example.com" not in response.text
    assert client.get(f"/auth/registration/requests/{rid}").status_code == 422
    assert (
        client.get(
            f"/auth/registration/requests/{rid}",
            headers={"X-Registration-Receipt": "approval_status_" + "x" * 64},
        ).status_code
        == 404
    )
    proof = service.proof(record(service, rid))
    with service.store.transaction() as tx:
        dumped = json.dumps(
            [v for ns in ("requests", "idempotency", "outbox") for _, v in tx.list(ns)]
        )
        assert created["receipt"] not in dumped
        assert proof not in dumped
        assert len(tx.list("outbox")) == 1
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_conflicting_creation_and_input_validation(approval):
    client, _, _, _ = approval
    create(client)
    changed = PAYLOAD | {"learnerDisplayName": "Different"}
    assert (
        client.post(
            "/auth/registration/learners",
            json=changed,
            headers={"Idempotency-Key": "registration-key-1".ljust(32, "_")},
        ).status_code
        == 409
    )
    for patch in (
        {"learnerDisplayName": "   "},
        {"learnerDisplayName": "Bad\nName"},
        {"learnerBirthYear": 3000},
        {"learnerBirthYear": True},
        {"learnerBirthYear": "2014"},
        {"parentEmail": "not-an-email"},
        {"password": "NeverStoreThis123!"},
    ):
        result = client.post(
            "/auth/registration/learners",
            json=PAYLOAD | patch,
            headers={"Idempotency-Key": "another-create-key".ljust(32, "_")},
        )
        assert result.status_code == 422, result.text
        assert "NeverStoreThis123!" not in result.text


def test_guardian_mailbox_is_not_authority_and_roles_are_ignored(approval):
    client, service, _, identities = approval
    created = create(client)
    rid = created["request_id"]
    assert client.get("/api/guardian/registration-requests").status_code == 401
    assert (
        client.get(
            "/api/guardian/registration-requests", headers={"Authorization": "Bearer unverified"}
        ).status_code
        == 403
    )
    own = client.get(
        "/api/guardian/registration-requests", headers={"Authorization": "Bearer guardian"}
    ).json()
    assert len(own["items"]) == 1 and own["items"][0]["can_decide"] is False
    assert (
        client.get(
            "/api/guardian/registration-requests", headers={"Authorization": "Bearer other"}
        ).json()["items"]
        == []
    )
    proof = service.proof(record(service, rid))
    assert decision(client, rid, proof).status_code == 403
    assert decision(client, rid, proof, token="other").status_code == 404
    authorize(service, rid, identities)
    # A forged/untrusted account_type is irrelevant; the verified request-specific grant matters.
    assert decision(client, rid, proof).status_code == 200


@pytest.mark.parametrize("action,expected", [("approve", "approved"), ("decline", "declined")])
def test_decision_is_idempotent_and_does_not_activate(approval, action, expected):
    client, service, _, identities = approval
    created = create(client)
    rid = created["request_id"]
    authorize(service, rid, identities)
    proof = service.proof(record(service, rid))
    accepted = decision(client, rid, proof, action=action)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == expected
    assert accepted.json()["version"] == 2 and accepted.json()["activated"] is False
    assert decision(client, rid, proof, action=action).json() == accepted.json()
    assert (
        decision(
            client, rid, proof, action="decline" if action == "approve" else "approve"
        ).status_code
        == 409
    )
    assert decision(client, rid, proof, action=action, key="new-decision-key").status_code == 409
    assert client.post(f"/auth/registration/requests/{rid}/activate").status_code == 404
    with service.store.transaction() as tx:
        assert len(tx.list("decisions")) == 1


def test_resend_rotates_proof_has_cooldown_and_replay_protection(approval):
    client, service, now, identities = approval
    created = create(client)
    rid = created["request_id"]
    authorize(service, rid, identities)
    old_proof = service.proof(record(service, rid))
    headers = {"X-Registration-Receipt": created["receipt"], "Idempotency-Key": "resend-key-1"}
    url = f"/auth/registration/requests/{rid}/resend"
    assert client.post(url, headers=headers).status_code == 429
    now[0] += 61
    assert client.post(url, headers=headers).status_code == 202
    assert client.post(url, headers=headers).status_code == 202
    current = record(service, rid)
    assert current["version"] == 2 and current["generation"] == 2
    assert decision(client, rid, old_proof, version=2).status_code == 403
    assert decision(client, rid, service.proof(current), version=1).status_code == 409
    assert decision(client, rid, service.proof(current), version=2).status_code == 200
    with service.store.transaction() as tx:
        assert len(tx.list("outbox")) == 2


def test_expired_requests_and_authority(approval):
    client, service, now, identities = approval
    created = create(client)
    rid = created["request_id"]
    service.grant_authority(rid, identities["guardian"]["id"], "evidence", lifetime=1)
    proof = service.proof(record(service, rid))
    now[0] += 2
    assert decision(client, rid, proof).status_code == 403
    now[0] += 7 * 86400
    status = client.get(
        f"/auth/registration/requests/{rid}", headers={"X-Registration-Receipt": created["receipt"]}
    )
    assert status.status_code == 410
    assert (
        client.post(
            f"/auth/registration/requests/{rid}/resend",
            headers={
                "X-Registration-Receipt": created["receipt"],
                "Idempotency-Key": "resend-expired",
            },
        ).status_code
        == 410
    )
    assert (
        client.post(
            "/auth/registration/learners",
            json=PAYLOAD,
            headers={"Idempotency-Key": "registration-key-1".ljust(32, "_")},
        ).status_code
        == 410
    )
    assert service.claim_mail() is None


def test_recipient_and_bad_receipt_rate_limits(approval):
    client, _, _, _ = approval
    created = create(client)
    create(client, "second-create-key")
    create(client, "third-create-key")
    response = client.post(
        "/auth/registration/learners",
        json=PAYLOAD,
        headers={"Idempotency-Key": "fourth-create-key".ljust(32, "_")},
    )
    assert response.status_code == 429 and "retry-after" in response.headers
    url = f"/auth/registration/requests/{created['request_id']}"
    statuses = [
        client.get(url, headers={"X-Registration-Receipt": "x" * 32}).status_code
        for _ in range(120)
    ]
    assert 404 in statuses and statuses[-1] == 429


def test_cross_origin_headers_and_receipt_is_not_session(approval):
    client, _, _, _ = approval
    created = create(client)
    response = client.options(
        "/auth/registration/learners",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,x-registration-receipt,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in response.headers
    assert (
        client.get(
            "/api/me/profile", headers={"Authorization": "Bearer " + created["receipt"]}
        ).status_code
        == 401
    )


def test_worker_delivery_retry_bounds_and_no_plaintext_proofs(approval):
    client, service, now, _ = approval
    created = create(client)
    calls = []

    def failing_sender(message):
        calls.append(message)
        raise RuntimeError("provider failure with sensitive details")

    assert deliver_one(service, failing_sender)
    assert not deliver_one(service, failing_sender)
    now[0] += 61
    assert deliver_one(service, failing_sender)
    now[0] += 121
    assert deliver_one(service, failing_sender)
    now[0] += 10000
    assert not deliver_one(service, failing_sender)
    assert len(calls) == 3
    assert len({c["mail_id"] for c in calls}) == 1
    with service.store.transaction() as tx:
        mail = tx.list("outbox")[0][1]
        assert mail["status"] == "failed"
        assert calls[0]["proof"] not in json.dumps(mail)
    response = client.get(
        f"/auth/registration/requests/{created['request_id']}",
        headers={"X-Registration-Receipt": created["receipt"]},
    )
    assert "provider failure" not in response.text and "proof" not in response.text


def test_worker_success_stale_lease_and_superseded_mail(approval):
    client, service, now, _ = approval
    created = create(client)
    first_mail, _, first_lease = service.claim_mail()
    now[0] += 61
    replacement, _, second_lease = service.claim_mail()
    assert first_mail["id"] == replacement["id"]
    assert not service.finish_mail(first_mail["id"], first_lease, True)
    assert service.finish_mail(replacement["id"], second_lease, True)
    now[0] += 61
    service.resend(created["request_id"], created["receipt"], "another-resend-key")
    sent = []
    assert deliver_one(service, sent.append)
    assert sent[0]["proof"] == service.proof(record(service, created["request_id"]))
    assert not deliver_one(service, sent.append)


def test_concurrent_decisions_have_one_winner(approval):
    client, service, _, identities = approval
    created = create(client)
    rid = created["request_id"]
    authorize(service, rid, identities)
    proof = service.proof(record(service, rid))

    def act(action):
        try:
            service.decide(
                rid,
                "guardian@example.com",
                identities["guardian"]["id"],
                {"decision": action, "expected_version": 1, "proof": proof},
                "key-" + action,
            )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(act, ["approve", "decline"])) == [200, 409]


def test_postgres_private_permissions_and_restart(approval):
    client, service, _, _ = approval
    created = create(client)
    if not service.store.repository.durable:
        pytest.skip("PostgreSQL privilege and restart check")
    repository = service.store.repository
    with repository.control() as connection:
        assert (
            connection.execute(
                "select has_table_privilege('authenticated','api_private.approval_records','SELECT')"
            ).fetchone()[0]
            is False
        )
        assert (
            connection.execute(
                "select has_table_privilege('anon','api_private.approval_records','SELECT')"
            ).fetchone()[0]
            is False
        )
    restarted = PostgresRepository(repository.url)
    try:
        other = ApprovalService(restarted, SECRET, clock=service.clock)
        assert other.status(created["request_id"], created["receipt"])["status"] == "pending"
    finally:
        restarted.close()


def test_concurrent_create_retries_enqueue_once(approval):
    _, service, _, _ = approval
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.create(CANONICAL, "same-creation-key", "test-client"), range(2)
            )
        )
    assert results[0] == results[1]
    with service.store.transaction() as tx:
        assert len(tx.list("requests")) == 1
        assert len(tx.list("outbox")) == 1


def test_receipt_cannot_read_another_request_and_guardian_pagination(approval):
    client, _, _, _ = approval
    first = create(client)
    second = create(client, "second-request-key")
    assert (
        client.get(
            f"/auth/registration/requests/{second['request_id']}",
            headers={"X-Registration-Receipt": first["receipt"]},
        ).status_code
        == 404
    )
    headers = {"Authorization": "Bearer guardian"}
    page = client.get("/api/guardian/registration-requests?limit=1", headers=headers).json()
    other = client.get(
        "/api/guardian/registration-requests",
        headers=headers,
        params={"limit": 1, "after": page["next_cursor"]},
    ).json()
    assert len(page["items"]) == len(other["items"]) == 1
    assert page["items"][0]["request_id"] != other["items"][0]["request_id"]
    assert other["next_cursor"] is None


def test_bad_unicode_proof_is_rejected_without_server_error(approval):
    client, service, _, identities = approval
    created = create(client)
    authorize(service, created["request_id"], identities)
    assert decision(client, created["request_id"], "ü" * 64).status_code == 403


def test_superseded_mail_is_not_sent_and_sender_holds_no_database_lock(approval):
    client, service, now, _ = approval
    created = create(client)
    now[0] += 61
    service.resend(created["request_id"], created["receipt"], "fresh-resend-key")
    sent = []

    def sender(message):
        # A nested status transaction would time out if the SMTP call held the control lock.
        assert service.status(created["request_id"], created["receipt"])["status"] == "pending"
        sent.append(message)

    assert deliver_one(service, sender)
    assert len(sent) == 1
    assert sent[0]["proof"] == service.proof(record(service, created["request_id"]))
    assert not deliver_one(service, sender)
    with service.store.transaction() as tx:
        assert sorted(mail["status"] for _, mail in tx.list("outbox")) == ["cancelled", "sent"]


def test_feature_defaults_off_and_requires_secret():
    settings = Settings(app_env="test", demo_mode=True, _env_file=None)
    with TestClient(create_app(settings)) as client:
        response = client.post("/auth/registration/learners", json=PAYLOAD)
        assert response.status_code == 503
    with pytest.raises(ValidationError):
        Settings(app_env="test", demo_mode=True, learner_approval_enabled=True, _env_file=None)
