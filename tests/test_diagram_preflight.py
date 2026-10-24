import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagram_preflight import DiagramPreflight, browser_probe, dependencies, schema_ready
from app.main import create_app
from app.repository import MemoryRepository


def settings(**changes):
    return Settings(
        app_env="test", demo_mode=True, diagram_assembly_enabled=True, _env_file=None, **changes
    )


def healthy(_):
    return {
        "ready": True,
        "reason": None,
        "versions": {
            "node": "v22.23.0",
            "mermaid": "12.2.0",
            "playwright": "1.64.1",
            "browser": "155.0.0.0",
        },
        "sandbox": True,
    }


def available(_):
    return {
        "ready": True,
        "reason": None,
        "versions": {"mermaid": "12.2.0", "playwright-core": "1.64.1"},
    }


def test_feature_disabled_never_checks_renderer_or_schema():
    def fail(_):
        pytest.fail("Disabled feature touched the renderer")

    config = settings()
    config.diagram_assembly_enabled = False
    result = DiagramPreflight(runtime_probe=fail, dependency_probe=fail).check(None, config)
    assert result["ready"] and result["state"] == "disabled" and result["schema_ready"] is None
    with TestClient(create_app(config)) as client:
        response = client.get("/ready")
        assert response.status_code == 200 and response.json()["durable"] is False
        assert response.headers["cache-control"] == "no-store"


def test_missing_node_makes_ready_fail_without_rendering():
    config = settings(diagram_node_executable="/nonexistent/private-node-path")
    with TestClient(create_app(config)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        result = response.json()["features"]["diagram_assembly"]
        assert result["reason"] == "node_unavailable" and not result["renderer_ready"]
        assert "/nonexistent" not in response.text


def test_healthy_readiness_is_cached_without_data_changes():
    calls = []
    clock = [100.0]
    repo = MemoryRepository()
    config = settings()

    def probe(c):
        calls.append(1)
        return healthy(c)

    cache = DiagramPreflight(
        runtime_probe=probe, dependency_probe=available, clock=lambda: clock[0]
    )
    first = cache.check(repo, config)
    assert first["ready"] and first["schema_ready"] and first["renderer_ready"]
    assert cache.check(repo, config) == first and len(calls) == 1
    first["versions"]["node"] = "client mutation"
    assert cache.check(repo, config)["versions"]["node"] == "v22.23.0"
    clock[0] += 61
    assert cache.check(repo, config)["ready"] and len(calls) == 2
    assert repo.data == {} and not hasattr(repo, "diagram_state")


def test_failures_are_sanitized_and_retry_after_short_ttl():
    calls = []
    clock = [10.0]

    def probe(_):
        calls.append(1)
        raise RuntimeError("password private-node-path and student content")

    cache = DiagramPreflight(
        runtime_probe=probe, dependency_probe=available, clock=lambda: clock[0]
    )
    first = cache.check(MemoryRepository(), settings())
    assert first["reason"] == "renderer_unavailable" and not first["ready"]
    assert "password" not in json.dumps(first) and "private-node" not in json.dumps(first)
    cache.check(MemoryRepository(), settings())
    assert len(calls) == 1
    clock[0] += 11
    cache.check(MemoryRepository(), settings())
    assert len(calls) == 2


def test_configuration_change_invalidates_cache_and_dependency_loss_is_immediate():
    calls = []
    config = settings()
    broken = [False]

    def deps(_):
        return {"ready": False, "reason": "packages_unavailable"} if broken[0] else available(_)

    def probe(c):
        calls.append(c.diagram_node_executable)
        return healthy(c)

    cache = DiagramPreflight(runtime_probe=probe, dependency_probe=deps)
    assert cache.check(MemoryRepository(), config)["ready"]
    config.diagram_node_executable = "different-configured-node"
    assert cache.check(MemoryRepository(), config)["ready"] and len(calls) == 2
    broken[0] = True
    assert cache.check(MemoryRepository(), config)["reason"] == "packages_unavailable"


def test_concurrent_probe_is_single_flight_and_busy_renderer_is_deferred():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def probe(c):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return healthy(c)

    cache = DiagramPreflight(runtime_probe=probe, dependency_probe=available)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(cache.check, MemoryRepository(), settings())
        assert entered.wait(2)
        try:
            other = cache.check(MemoryRepository(), settings())
            assert other["reason"] == "preflight_in_progress"
        finally:
            release.set()
        assert running.result()["ready"]
    assert calls == [1]
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    cache = DiagramPreflight(runtime_probe=healthy, dependency_probe=available, render_slots=slots)
    assert cache.check(MemoryRepository(), settings())["reason"] == "renderer_busy"
    slots.release()
    assert cache.check(MemoryRepository(), settings())["ready"]


def test_preflight_checks_schema_objects_and_disabled_trigger(repository):
    config = settings()
    cache = DiagramPreflight(runtime_probe=healthy, dependency_probe=available)
    assert schema_ready(repository, config) and cache.check(repository, config)["ready"]
    if not repository.durable:
        pytest.skip("Migration failure/trigger checks require PostgreSQL")
    try:
        with repository.control() as conn:
            conn.execute("alter table api_private.records disable trigger diagram_source_changed")
        result = cache.check(repository, config)
        assert not result["ready"] and result["reason"] == "migration_required"
        with repository.control() as conn:
            conn.execute(
                "alter table api_private.records enable replica trigger diagram_source_changed"
            )
        assert not cache.check(repository, config)["ready"]
    finally:
        with repository.control() as conn:
            conn.execute("alter table api_private.records enable trigger diagram_source_changed")
    assert cache.check(repository, config)["ready"]
    try:
        with repository.control() as conn:
            conn.execute(
                "alter index api_private.diagram_races_template rename to temporary_missing_index"
            )
        assert not cache.check(repository, config)["ready"]
    finally:
        with repository.control() as conn:
            conn.execute(
                "alter index api_private.temporary_missing_index rename to diagram_races_template"
            )


@pytest.mark.parametrize(
    "failure", ["node", "helper", "packages", "versions", "browser", "sandbox"]
)
def test_dependency_status_codes_do_not_expose_paths(monkeypatch, tmp_path, failure):
    import app.diagram_preflight as module

    root = tmp_path / "private-runtime"
    root.mkdir()
    (root / "render.mjs").write_text("// backend helper")
    for name, version in module.EXPECTED.items():
        directory = root / "node_modules" / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"version": version}))
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.shutil, "which", lambda value: "/private-runtime/node")
    config = settings()
    expected = {
        "node": "node_unavailable",
        "helper": "helper_unavailable",
        "packages": "packages_unavailable",
        "versions": "package_version_mismatch",
        "browser": "browser_unavailable",
        "sandbox": "sandbox_required",
    }
    if failure == "node":
        monkeypatch.setattr(module.shutil, "which", lambda value: None)
    if failure == "helper":
        (root / "render.mjs").unlink()
    if failure == "packages":
        (root / "node_modules" / "mermaid" / "package.json").unlink()
    if failure == "versions":
        (root / "node_modules" / "mermaid" / "package.json").write_text('{"version":"0.0.0"}')
    if failure == "browser":
        config.diagram_browser_executable = "/private-browser/missing"
        monkeypatch.setattr(
            module.shutil,
            "which",
            lambda value: None if value == config.diagram_browser_executable else "/node",
        )
    if failure == "sandbox":
        config.app_env = "production"
        config.diagram_browser_sandbox = False
    result = dependencies(config)
    assert not result["ready"] and result["reason"] == expected[failure]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"node": "v20.1.0"}, "node_version_unsupported"),
        ({"mermaid": "0.0.0"}, "renderer_version_mismatch"),
        ({"sandbox": False}, "sandbox_unverified"),
        ({"browser": "/private/browser/path"}, "renderer_metadata_invalid"),
    ],
)
def test_browser_probe_validates_provenance_without_echo(monkeypatch, change, expected):
    import app.diagram_preflight as module

    metadata = {
        "node": "v22.23.0",
        "mermaid": "12.2.0",
        "playwright": "1.64.1",
        "browser": "155.0.0.0",
        "sandbox": True,
    } | change
    monkeypatch.setattr(module, "render_snapshot", lambda code, config: {"renderer": metadata})
    result = browser_probe(settings())
    assert not result["ready"] and result["reason"] == expected
    assert "private" not in json.dumps(result)


def test_renderer_crash_produces_safe_unavailable_status(monkeypatch):
    import app.diagram_preflight as module

    def fail(*_):
        raise HTTPException(503, "secret source and filesystem path")

    monkeypatch.setattr(module, "render_snapshot", fail)
    assert browser_probe(settings()) == {"ready": False, "reason": "renderer_unavailable"}


def test_main_readiness_combines_storage_and_feature_health(monkeypatch):
    repo = MemoryRepository()
    app = create_app(settings(), repository=repo)
    app.state.diagram_preflight = DiagramPreflight(
        runtime_probe=healthy, dependency_probe=available
    )
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200 and ready.json()["ready"]
        assert ready.json()["features"]["diagram_assembly"]["renderer_ready"]
        monkeypatch.setattr(repo, "ready", lambda: False)
        result = client.get("/ready")
        assert (
            result.status_code == 503
            and result.json()["features"]["diagram_assembly"]["reason"] == "storage_unavailable"
        )


def test_recent_success_has_bounded_grace_while_game_renderer_is_busy():
    clock = [100.0]
    slots = threading.BoundedSemaphore(1)
    cache = DiagramPreflight(
        runtime_probe=healthy,
        dependency_probe=available,
        clock=lambda: clock[0],
        render_slots=slots,
    )
    repo = MemoryRepository()
    config = settings()
    assert cache.check(repo, config)["ready"]
    slots.acquire()
    try:
        clock[0] += 61
        result = cache.check(repo, config)
        assert result["ready"] and result["probe_deferred"]
        clock[0] += 60
        result = cache.check(repo, config)
        assert not result["ready"] and result["reason"] == "renderer_busy"
    finally:
        slots.release()
    assert cache.check(repo, config)["ready"]
