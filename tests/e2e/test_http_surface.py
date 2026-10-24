"""The HTTP surface as a hostile reviewer sees it: credentials, headers, bodies, limits, CORS, errors."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import CANARY, PARENT, api_routes, bug_on_postgres, child_headers


def anonymous(client):
    return TestClient(client.app)


# ---------------------------------------------------------------- credentials
BAD_AUTHORIZATION = [
    None,
    "Basic dXNlcjpwYXNz",
    "Bearer",
    "Bearer ",
    "Token abc",
    "Bearer not-a-real-token",
    "Bearer demo_access_forged",
    "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTQwMDAtODAwMC0wMDAwMDAwMDAwMDEifQ.",
]


# Opt-in features merged from main that answer bad credentials differently (BUG-18, BUG-19, both off
# by default). They get their own checks below; remove a prefix once its bugs are fixed.
FLAGGED_FEATURES = ("/api/diagram-assembly/", "/api/guardian/")


def route_requests(client, *, flagged):
    for method, path in api_routes(client.app):
        if path.startswith(FLAGGED_FEATURES) == flagged:
            url = re.sub(r"\{[^}]+\}", "x", path)
            yield method, url, {"json": {}} if method in ("POST", "PUT", "PATCH") else {}


def test_every_data_route_rejects_missing_or_bad_credentials(client):
    routes = list(route_requests(client, flagged=False))
    # The walk must not silently cover nothing: docs/API_PLATFORM.md lists 43 operations.
    assert len(routes) >= 40
    assert ("POST", "/api/rooms/join", {"json": {}}) in routes
    assert ("GET", "/api/me/progress", {}) in routes
    anon = anonymous(client)
    for method, url, body in routes:
        for header in BAD_AUTHORIZATION:
            headers = {"Authorization": header} if header else {}
            r = anon.request(method, url, headers=headers, **body)
            assert r.status_code == 401, (method, url, header, r.status_code)
            assert r.headers["cache-control"] == "no-store"
            if header is None or not header.lower().startswith("bearer ") or len(header) <= 7:
                assert r.headers["www-authenticate"] == "Bearer"  # no usable credential at all


def test_flagged_feature_routes_never_accept_bad_credentials(client):
    routes = list(route_requests(client, flagged=True))
    assert len(routes) >= 10  # diagram assembly and guardian approval are both mounted
    anon = anonymous(client)
    for method, url, body in routes:
        for header in BAD_AUTHORIZATION:
            headers = {"Authorization": header} if header else {}
            r = anon.request(method, url, headers=headers, **body)
            assert r.status_code in (401, 503), (method, url, header, r.status_code)
            assert r.headers["cache-control"] == "no-store"


@pytest.mark.xfail(strict=True, reason="BUG-18: these 401s omit WWW-Authenticate")
@pytest.mark.parametrize(
    "url", ["/api/diagram-assembly/races/x", "/api/guardian/registration-requests"]
)
def test_flagged_features_challenge_a_missing_credential(client, url):
    r = anonymous(client).get(url)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.xfail(strict=True, reason="BUG-19: the feature flag is checked before the credential")
@pytest.mark.parametrize(
    ("url", "header"),
    [
        ("/api/diagram-assembly/races/x", "Bearer not-a-real-token"),
        ("/api/guardian/registration-requests", None),
    ],
)
def test_flagged_features_ask_for_sign_in_before_revealing_the_flag(client, url, header):
    r = anonymous(client).get(url, headers={"Authorization": header} if header else {})
    assert r.status_code == 401


@pytest.mark.xfail(strict=True, reason="BUG-16: a 401 for an invalid token omits WWW-Authenticate")
def test_an_invalid_token_is_challenged_like_a_missing_one(client):
    r = anonymous(client).get("/api/me/profile", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


def test_credentials_only_count_in_the_authorization_header(client):
    token = client.headers["Authorization"].split()[1]
    anon = anonymous(client)
    assert (
        anon.get("/api/me/profile", headers={"Authorization": "bearer " + token}).status_code == 200
    )
    assert anon.get("/api/me/profile", params={"access_token": token}).status_code == 401
    cookie = f"access_token={token}; sb-access-token={token}"
    assert anon.get("/api/me/profile", headers={"Cookie": cookie}).status_code == 401
    spoof = {"X-Access-Token": token, "X-Forwarded-User": PARENT, "X-User-Id": PARENT}
    assert anon.get("/api/me/profile", headers=spoof).status_code == 401


def test_a_forged_identity_header_cannot_switch_accounts(api):
    api.put("/api/me/profile", json={"display_name": "Parent"})
    r = api.get("/api/me/profile", headers={**child_headers(api), "X-User-Id": PARENT})
    assert r.json()["display_name"] == "Learner"


# ---------------------------------------------------------------- headers on every outcome
@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/api/does-not-exist", 404),
        ("GET", "/auth/does-not-exist", 404),
        ("DELETE", "/api/me/profile", 405),
        ("POST", "/auth/me", 405),
        ("GET", "/api/lessons/missing", 404),
        ("GET", "/api/resources/nonsense", 422),
    ],
)
def test_errors_stay_non_cacheable_and_traceable(client, method, path, status):
    r = client.request(method, path)
    assert r.status_code == status
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["pragma"] == "no-cache"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert re.fullmatch(r"[0-9a-f]{24}", r.headers["x-request-id"])


def test_request_ids_are_server_generated_and_unique(client):
    ids = {
        client.get("/health", headers={"X-Request-ID": "attacker-chosen"}).headers["x-request-id"]
        for _ in range(5)
    }
    assert len(ids) == 5 and "attacker-chosen" not in ids


# ---------------------------------------------------------------- validation never echoes input
@pytest.mark.parametrize(
    "method,url,body",
    [
        ("post", "/api/lessons", {"topic": CANARY, "age": CANARY}),
        (
            "put",
            "/api/me/profile",
            {"display_name": 5, "daily_goal": CANARY, "preferences": {"theme": CANARY}},
        ),
        ("post", "/api/rooms/join", {"code": CANARY, "display_name": CANARY}),
        ("post", "/api/rooms", {"display_name": CANARY, "capacity": CANARY}),
        ("post", "/api/jobs", {"kind": CANARY, "parameters": CANARY}),
        ("put", "/api/materials/x/pages", {"expected_revision": CANARY, "pages": CANARY}),
        ("post", "/api/tournament", {"lesson_id": 5, "num_questions": CANARY}),
        ("post", "/api/flashcards/x/review", {"card": CANARY, "result": CANARY}),
        ("post", "/api/quiz/x/check", {"index": CANARY, "choice": CANARY}),
        ("post", "/auth/login", {"email": CANARY, "password": CANARY * 20}),
        ("post", "/auth/refresh", {"refresh_token": CANARY * 400}),
        ("put", "/auth/password", {"password": CANARY[:5]}),
    ],
)
def test_validation_errors_never_echo_what_was_sent(client, method, url, body):
    r = getattr(client, method)(url, json=body, headers={"Idempotency-Key": "echo-test-0001"})
    assert r.status_code == 422
    assert CANARY not in r.text and '"input"' not in r.text and '"ctx"' not in r.text


@pytest.mark.parametrize(
    "content,content_type",
    [
        (b'{"topic": "' + CANARY.encode() + b'", ', "application/json"),
        (b"[]", "application/json"),
        (b"null", "application/json"),
        (b'"' + CANARY.encode() + b'"', "application/json"),
        (b'{"topic":"x","age":NaN}', "application/json"),
        (b'{"topic":"x","age":Infinity}', "application/json"),
        (b'\xff\xfe{"topic":"' + CANARY.encode() + b'"', "application/json"),
        (b'{"topic":"x","age":12}', "text/plain"),
        (b'{"topic":"x","age":12}', "application/x-www-form-urlencoded"),
        (b"topic=" + CANARY.encode() + b"&age=12", "application/x-www-form-urlencoded"),
        (b"", "application/json"),
    ],
)
def test_malformed_bodies_are_rejected_without_echo(client, content, content_type):
    r = client.post("/api/lessons", content=content, headers={"Content-Type": content_type})
    assert r.status_code == 422, (r.status_code, r.text)
    assert CANARY not in r.text


def test_absurdly_nested_json_is_a_client_error_not_a_crash(client):
    r = client.post(
        "/api/lessons", content=b"[" * 200_000, headers={"Content-Type": "application/json"}
    )
    assert r.status_code in (400, 422)


def test_repeated_keys_use_the_last_value_but_cannot_bypass_validation(client):
    body = b'{"topic":"x","age":12,"age":4}'
    r = client.post("/api/lessons", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422  # the last duplicate wins, so the invalid age is what is checked


# ---------------------------------------------------------------- body size
@pytest.mark.parametrize(
    "method,url",
    [
        ("post", "/api/lessons"),
        ("put", "/api/me/profile"),
        ("post", "/api/rooms"),
        ("post", "/api/jobs"),
    ],
)
def test_streamed_bodies_over_the_limit_are_cut_off(client, method, url):
    def chunks():
        for _ in range(5):
            yield b"x" * 2**20

    r = getattr(client, method)(
        url,
        content=chunks(),
        headers={"Content-Type": "application/json", "Idempotency-Key": "chunked-body-0001"},
    )
    assert r.status_code == 413, r.text
    assert r.headers["cache-control"] == "no-store"


def test_the_size_limiter_counts_received_bytes_whatever_the_headers_claim():
    """Deterministic, transport-free check of app.http_limits.BodyLimit."""
    import asyncio

    from fastapi import HTTPException

    from app.http_limits import BodyLimit

    async def deliver(sizes, path="/api/lessons"):
        messages = [
            {"type": "http.request", "body": b"x" * n, "more_body": i < len(sizes) - 1}
            for i, n in enumerate(sizes)
        ]

        async def app(scope, receive, send):
            while (await receive()).get("more_body"):
                pass

        async def receive():
            return messages.pop(0)

        async def send(message):
            pass

        await BodyLimit(app)({"type": "http", "path": path, "headers": []}, receive, send)

    mib = 2**20
    asyncio.run(deliver([mib] * 4))  # exactly four MiB is allowed
    for sizes, path in [
        ([mib] * 4 + [1], "/api/lessons"),
        ([mib * 5], "/api/rooms"),
        ([mib] * 61 + [1], "/api/materials"),
    ]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(deliver(sizes, path))
        assert error.value.status_code == 413
    asyncio.run(
        deliver([mib] * 61, "/api/materials")
    )  # uploads get their larger, still bounded, allowance
    asyncio.run(
        BodyLimit(lambda *a: asyncio.sleep(0))({"type": "websocket"}, None, None)
    )  # non-HTTP scopes pass through


def test_json_body_limit_boundary(client):
    limit = 4 * 2**20

    def body(total):
        head, tail = b'{"display_name":"', b'"}'
        return head + b"a" * (total - len(head) - len(tail)) + tail

    headers = {"Content-Type": "application/json"}
    at_limit = client.put("/api/me/profile", content=body(limit), headers=headers)
    assert at_limit.status_code == 422  # accepted by the size gate, refused by field validation
    over = client.put("/api/me/profile", content=body(limit + 1), headers=headers)
    assert over.status_code == 413


# ---------------------------------------------------------------- identifiers in paths
HOSTILE_IDS = [
    "..%2F..%2Fetc%2Fpasswd",
    "%2e%2e",
    "%27%20OR%201%3D1--",
    "%3Cscript%3Ealert(1)%3C%2Fscript%3E",
    "a%20b",
    "%25",
    "%5C%5C",
    "%F0%9F%A6%8A",
    "0",
    "-1",
    "null",
    "undefined",
    "a" * 5000,
]
READ_ROUTES = [
    "/api/lessons/{}",
    "/api/quiz/{}",
    "/api/flashcards/{}",
    "/api/tournament/{}",
    "/api/tournament/{}/review",
    "/api/materials/{}",
    "/api/materials/{}/pages",
    "/api/materials/{}/revisions/1",
    "/api/materials/{}/find?topic=x",
    "/api/jobs/{}",
    "/api/rooms/{}",
    "/api/lessons/{}/visuals/x/view",
    "/api/quiz/{}/questions/0/view",
    "/api/flashcards/{}/cards/0/view",
]
WRITE_ROUTES = [
    ("post", "/api/lessons/{}/approve", {"expected_revision": 1, "confirmed": True}),
    (
        "post",
        "/api/lessons/{}/revisions",
        {"lesson": {"title": "t", "summary": "s", "sections": [], "key_points": []}},
    ),
    ("post", "/api/lessons/{}/complete", None),
    ("post", "/api/lessons/{}/quiz", {}),
    ("post", "/api/lessons/{}/flashcards", {}),
    ("post", "/api/lessons/{}/visuals", {}),
    ("post", "/api/quiz/{}/check", {"index": 0, "choice": 0}),
    ("post", "/api/quiz/{}/attempts", None),
    ("post", "/api/quiz/{}/followup", {}),
    ("post", "/api/quiz/{}/flashcards", {}),
    ("post", "/api/flashcards/{}/review", {"card": 0, "result": "know"}),
    ("post", "/api/tournament/{}/approve", {"expected_revision": 1, "confirmed": True}),
    ("post", "/api/tournament/{}/submit", {"answers": [0]}),
    ("post", "/api/materials/{}/confirm", {"expected_revision": 1, "confirmed": True}),
    (
        "put",
        "/api/materials/{}/pages",
        {"expected_revision": 1, "pages": [{"number": 1, "text": "x", "via": "text"}]},
    ),
    ("delete", "/api/materials/{}", None),
    ("delete", "/api/jobs/{}", None),
    ("post", "/api/rooms/{}/commands", {"expected_version": 1, "action": "leave"}),
]


@pytest.mark.parametrize("identifier", HOSTILE_IDS, ids=lambda s: s[:24])
def test_hostile_identifiers_are_plain_404s(api, identifier):
    for route in READ_ROUTES:
        r = api.get(route.format(identifier))
        assert r.status_code == 404, (route, r.status_code, r.text[:120])
    for method, route, body in WRITE_ROUTES:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(api, method)(route.format(identifier), **kwargs)
        assert r.status_code == 404, (method, route, r.status_code, r.text[:120])


def test_nul_in_a_path_identifier_is_a_clean_404(api, repository, request):
    bug_on_postgres(
        request, repository, "BUG-01: a NUL byte in user text/ids becomes a 503 on PostgreSQL"
    )
    for route in READ_ROUTES:
        r = api.get(route.format("a%00b"))
        assert r.status_code == 404, (route, r.status_code)


NUL_TEXT_CASES = [
    ("post", "/api/lessons", {"topic": "a\u0000b", "age": 12}),
    ("post", "/api/rooms", {"display_name": "a\u0000b"}),
    ("put", "/api/me/profile", {"display_name": "a\u0000b"}),
]


@pytest.mark.xfail(
    strict=True,
    reason="BUG-01: a NUL byte in user text is stored (memory) or becomes a 503 (PostgreSQL)",
)
@pytest.mark.parametrize("method,url,body", NUL_TEXT_CASES, ids=[c[1] for c in NUL_TEXT_CASES])
def test_nul_in_user_text_is_refused_or_stripped(api, method, url, body):
    r = getattr(api, method)(url, json=body)
    assert r.status_code == 422 or (r.status_code < 300 and "\\u0000" not in r.text), r.status_code


def test_nul_in_a_cursor_does_not_become_a_server_error(api, repository, request):
    bug_on_postgres(
        request, repository, "BUG-01: a NUL byte in user text/ids becomes a 503 on PostgreSQL"
    )
    assert api.get("/api/resources/lessons", params={"after": "a\u0000b"}).status_code in (200, 422)


# ---------------------------------------------------------------- CORS
def preflight(client, origin, method="GET", headers="Authorization"):
    return client.options(
        "/api/me/profile",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_cors_allows_only_the_exact_configured_origin(client):
    ok = preflight(client, "http://localhost:3000", "PUT", "Authorization,Content-Type")
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in ok.headers
    for origin in [
        "https://evil.example",
        "null",
        "http://localhost:3000.evil.example",
        "http://localhost:3001",
        "https://localhost:3000",
        "http://LOCALHOST:3000",
        "*",
    ]:
        r = preflight(client, origin)
        assert "access-control-allow-origin" not in r.headers, origin
        assert r.status_code == 400, origin


def test_cors_refuses_unlisted_methods_and_headers(client):
    assert preflight(client, "http://localhost:3000", "TRACE").status_code == 400
    assert preflight(client, "http://localhost:3000", "GET", "X-Evil").status_code == 400
    methods = preflight(client, "http://localhost:3000").headers["access-control-allow-methods"]
    assert set(methods.split(", ")) == {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_a_foreign_page_is_not_given_permission_to_read_responses(client):
    r = client.get("/api/me/profile", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


# ---------------------------------------------------------------- docs, health, readiness
def test_schema_and_docs_do_not_leak_internals(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    for needle in [
        "api_private",
        "service_role",
        "DemoParent123",
        "DemoChild123",
        "sb_publishable",
        "DATABASE_URL",
    ]:
        assert needle not in spec.text, needle
    assert client.get("/docs").status_code == 200 and client.get("/redoc").status_code == 200


def test_health_and_readiness_shapes(client, repository):
    assert client.get("/health").json() == {"status": "ok", "auth_mode": "demo"}
    assert client.get("/ready").json() == {"ready": True, "durable": repository.durable}
    assert client.post("/health").status_code == 405
    assert client.get("/health", params={"debug": "1"}).json() == {
        "status": "ok",
        "auth_mode": "demo",
    }


def test_readiness_failure_is_a_503_without_exception_text(client, monkeypatch):
    def broken():
        raise RuntimeError(CANARY)

    monkeypatch.setattr(client.app.state.repository, "ready", broken)
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["ready"] is False and CANARY not in r.text


@pytest.mark.xfail(
    strict=True, reason="BUG-10: HEAD /health and /ready answer 405, which uptime monitors use"
)
@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_head_requests_work_for_monitors(client, path):
    assert client.head(path).status_code == 200


# ---------------------------------------------------------------- per-account request limit
def test_request_limit_counts_every_authenticated_request_then_blocks(client):
    client.app.state.settings.api_requests_per_minute = 5
    codes = [client.get("/api/lessons/missing").status_code for _ in range(7)]
    assert codes == [404] * 5 + [429] * 2


def test_request_limit_resets_in_the_next_minute(client, monkeypatch):
    client.app.state.settings.api_requests_per_minute = 2
    offset = [0]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + offset[0])
    assert [client.get("/api/me/profile").status_code for _ in range(3)] == [200, 200, 429]
    offset[0] = 61
    assert client.get("/api/me/profile").status_code == 200


def test_unauthenticated_floods_do_not_spend_an_accounts_budget(client):
    client.app.state.settings.api_requests_per_minute = 2
    anon = anonymous(client)
    for _ in range(25):
        assert (
            anon.get(
                "/api/me/profile", headers={"Authorization": "Bearer demo_access_x"}
            ).status_code
            == 401
        )
    assert client.get("/api/me/profile").status_code == 200


def test_request_limit_is_not_shared_between_accounts(client):
    client.app.state.settings.api_requests_per_minute = 1
    other = child_headers(client)
    assert client.get("/api/me/profile").status_code == 200
    assert client.get("/api/me/profile").status_code == 429
    assert client.get("/api/me/profile", headers=other).status_code == 200


@pytest.mark.xfail(strict=True, reason="BUG-04: 429 responses carry no Retry-After header")
def test_rate_limited_responses_say_when_to_retry(client):
    client.app.state.settings.api_requests_per_minute = 1
    client.get("/api/me/profile")
    r = client.get("/api/me/profile")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


# ---------------------------------------------------------------- unexpected failures
def test_unexpected_exceptions_do_not_leak_details(client, lesson_id, monkeypatch):
    def boom():
        raise RuntimeError(CANARY)

    monkeypatch.setattr("app.platform.now", boom)
    crashing = TestClient(client.app, raise_server_exceptions=False)
    crashing.headers.update(client.headers)
    r = crashing.post(f"/api/lessons/{lesson_id}/complete")
    assert r.status_code == 500 and CANARY not in r.text and "Traceback" not in r.text


@pytest.mark.xfail(
    strict=True, reason="BUG-03: 500 responses lack the no-store and X-Request-ID headers"
)
def test_unexpected_exceptions_keep_the_documented_headers(client, lesson_id, monkeypatch):
    def boom():
        raise RuntimeError("internal")

    monkeypatch.setattr("app.platform.now", boom)
    crashing = TestClient(client.app, raise_server_exceptions=False)
    crashing.headers.update(client.headers)
    r = crashing.post(f"/api/lessons/{lesson_id}/complete")
    assert r.status_code == 500
    assert r.headers.get("cache-control") == "no-store" and "x-request-id" in r.headers


# ---------------------------------------------------------------- generation gates
def test_generation_routes_are_closed_while_generation_is_disabled(api, lesson_id):
    quiz_id = api.post(f"/api/lessons/{lesson_id}/quiz", json={"num_questions": 3}).json()["id"]
    api.app.state.settings.generation_enabled = False
    gated = [
        ("/api/lessons", {"topic": "x", "age": 12}),
        (f"/api/lessons/{lesson_id}/visuals", {}),
        (f"/api/lessons/{lesson_id}/quiz", {}),
        (f"/api/lessons/{lesson_id}/flashcards", {}),
        ("/api/tournament", {"lesson_id": lesson_id}),
        (f"/api/quiz/{quiz_id}/followup", {}),
    ]
    for url, body in gated:
        r = api.post(url, json=body)
        assert r.status_code == 503, url
        assert "disabled" in r.json()["detail"]
    upload = api.post(
        "/api/materials", files=[("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))]
    )
    assert upload.status_code == 503
    # Nothing generated is hidden or lost: reads and non-generating writes keep working.
    assert api.get(f"/api/lessons/{lesson_id}").status_code == 200
    assert api.get(f"/api/quiz/{quiz_id}").status_code == 200
    assert api.post(f"/api/quiz/{quiz_id}/check", json={"index": 0, "choice": 0}).status_code == 200
    assert api.post(f"/api/lessons/{lesson_id}/complete").status_code == 200


def test_busy_generation_is_a_429_that_does_not_spend_the_daily_budget(api):
    api.app.state.settings.generation_daily_limit = 1
    slots = api.app.state.generation_slots
    held = 0
    while slots.acquire(blocking=False):
        held += 1
    try:
        r = api.post("/api/lessons", json={"topic": "x", "age": 12})
        assert r.status_code == 429 and "busy" in r.json()["detail"]
    finally:
        for _ in range(held):
            slots.release()
    assert api.post("/api/lessons", json={"topic": "x", "age": 12}).status_code == 201
    assert api.post("/api/lessons", json={"topic": "x", "age": 12}).status_code == 429


def test_daily_generation_budget_is_per_account(api):
    api.app.state.settings.generation_daily_limit = 2
    other = child_headers(api)
    assert [
        api.post("/api/lessons", json={"topic": "x", "age": 12}).status_code for _ in range(3)
    ] == [201, 201, 429]
    assert (
        api.post("/api/lessons", json={"topic": "x", "age": 12}, headers=other).status_code == 201
    )
