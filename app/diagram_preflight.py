"""Read-only dependency checks and cached fixed-diagram browser preflight. No AI calls."""

import argparse
import copy
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from app.diagram_render import render_snapshot
from app.diagram_store import DiagramStore

EXPECTED = {"mermaid": "12.2.0", "playwright-core": "1.64.1"}
PROBE_CODE = 'flowchart LR\n A["Input"] --> B["Output"]'
ROOT = Path(__file__).resolve().parents[1] / "tools" / "diagram-renderer"


def dependencies(settings):
    if os.name != "posix":
        return {"ready": False, "reason": "platform_unsupported"}
    if not shutil.which(settings.diagram_node_executable):
        return {"ready": False, "reason": "node_unavailable"}
    if not (ROOT / "render.mjs").is_file():
        return {"ready": False, "reason": "helper_unavailable"}
    try:
        versions = {
            name: json.loads((ROOT / "node_modules" / name / "package.json").read_text())["version"]
            for name in EXPECTED
        }
    except (OSError, ValueError, KeyError):
        return {"ready": False, "reason": "packages_unavailable"}
    if versions != EXPECTED:
        return {"ready": False, "reason": "package_version_mismatch"}
    if settings.diagram_browser_executable and not shutil.which(
        settings.diagram_browser_executable
    ):
        return {"ready": False, "reason": "browser_unavailable"}
    if settings.app_env == "production" and not settings.diagram_browser_sandbox:
        return {"ready": False, "reason": "sandbox_required"}
    return {"ready": True, "reason": None, "versions": versions}


def browser_probe(settings):
    try:
        snapshot = render_snapshot(PROBE_CODE, settings)
    except Exception:
        return {"ready": False, "reason": "renderer_unavailable"}
    metadata = snapshot.get("renderer", {})
    node = metadata.get("node", "")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", node) or int(node[1:].split(".")[0]) < 22:
        return {"ready": False, "reason": "node_version_unsupported"}
    if (
        metadata.get("mermaid") != EXPECTED["mermaid"]
        or metadata.get("playwright") != EXPECTED["playwright-core"]
    ):
        return {"ready": False, "reason": "renderer_version_mismatch"}
    if settings.diagram_browser_sandbox and metadata.get("sandbox") is not True:
        return {"ready": False, "reason": "sandbox_unverified"}
    versions = {
        name: metadata.get(name, "") for name in ("node", "mermaid", "playwright", "browser")
    }
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", v)
        for v in versions.values()
    ):
        return {"ready": False, "reason": "renderer_metadata_invalid"}
    return {
        "ready": True,
        "reason": None,
        "versions": versions,
        "sandbox": metadata.get("sandbox") is True,
    }


def schema_ready(repository, settings):
    if repository is None:
        return False
    if not repository.durable:
        return settings.demo_mode and settings.app_env in ("development", "test")
    with DiagramStore(repository).transaction() as tx:
        return tx.connection.execute("""select
          to_regclass('api_private.diagram_templates') is not null
          and to_regclass('api_private.diagram_races') is not null
          and to_regclass('api_private.diagram_players') is not null
          and to_regclass('api_private.diagram_requests') is not null
          and to_regclass('api_private.diagram_races_room') is not null
          and to_regclass('api_private.diagram_players_user') is not null
          and to_regclass('api_private.diagram_races_template') is not null
          and to_regprocedure('api_private.diagram_forget_player()') is not null
          and to_regprocedure('api_private.diagram_invalidate_lesson()') is not null
          and exists(select 1 from pg_trigger where tgrelid=to_regclass('api_private.records')
              and tgname='diagram_source_changed' and tgenabled in ('O','A')
              and tgfoid=to_regprocedure('api_private.diagram_invalidate_lesson()')
              and not tgisinternal)
        """).fetchone()[0]


class DiagramPreflight:
    def __init__(
        self,
        *,
        runtime_probe=None,
        dependency_probe=None,
        clock=time.monotonic,
        success_ttl=60,
        failure_ttl=10,
        render_slots=None,
    ):
        self.probe = runtime_probe or browser_probe
        self.dependencies = dependency_probe or dependencies
        self.clock = clock
        self.success_ttl, self.failure_ttl = success_ttl, failure_ttl
        self.render_slots = render_slots
        self.lock = threading.Lock()
        self.cached = None
        self.cache_key = None
        self.expires = 0

    def check(self, repository, settings):
        if not settings.diagram_assembly_enabled:
            return {
                "enabled": False,
                "ready": True,
                "state": "disabled",
                "reason": None,
                "schema_ready": None,
                "renderer_ready": None,
                "checked_at": None,
            }
        result = {
            "enabled": True,
            "ready": False,
            "state": "unavailable",
            "reason": None,
            "schema_ready": False,
            "renderer_ready": False,
            "checked_at": None,
        }
        try:
            if not schema_ready(repository, settings):
                return result | {"reason": "migration_required"}
            result["schema_ready"] = True
            deps = self.dependencies(settings)
        except Exception:
            return result | {"reason": "storage_or_dependency_unavailable"}
        if not deps["ready"]:
            return result | {"reason": deps["reason"]}
        key = (
            settings.app_env,
            settings.diagram_node_executable,
            settings.diagram_browser_executable,
            settings.diagram_browser_sandbox,
            settings.diagram_render_timeout,
            tuple(sorted(deps.get("versions", {}).items())),
        )
        if self.cached is not None and key == self.cache_key and self.clock() < self.expires:
            return copy.deepcopy(self.cached)

        def deferred(reason):
            if (
                self.cached is not None
                and self.cached["ready"]
                and key == self.cache_key
                and self.clock() < self.expires + self.success_ttl
            ):
                return copy.deepcopy(self.cached) | {
                    "probe_deferred": True,
                    "deferred_reason": reason,
                }
            return result | {"state": "checking", "reason": reason}

        if not self.lock.acquire(blocking=False):
            return deferred("preflight_in_progress")
        acquired = False
        try:
            if self.cached is not None and key == self.cache_key and self.clock() < self.expires:
                return copy.deepcopy(self.cached)
            if self.render_slots is not None:
                acquired = self.render_slots.acquire(blocking=False)
                if not acquired:
                    return deferred("renderer_busy")
            try:
                probe = self.probe(settings)
            except Exception:
                probe = {"ready": False, "reason": "renderer_unavailable"}
            ready = probe.get("ready") is True
            result |= {
                "ready": ready,
                "renderer_ready": ready,
                "state": "ready" if ready else "unavailable",
                "reason": probe.get("reason"),
                "checked_at": time.time_ns() // 1_000_000,
            }
            if ready:
                result |= {
                    "versions": probe.get("versions", {}),
                    "sandbox": probe.get("sandbox", False),
                }
            self.cached = copy.deepcopy(result)
            self.cache_key = key
            self.expires = self.clock() + (self.success_ttl if ready else self.failure_ttl)
            return result
        finally:
            if acquired:
                self.render_slots.release()
            self.lock.release()


def main():
    from app.config import Settings
    from app.repository import MemoryRepository, PostgresRepository

    parser = argparse.ArgumentParser(
        description="Read-only diagram dependency/schema probe; never applies migrations"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Probe even if the feature is configured off; does not change configuration",
    )
    args = parser.parse_args()
    settings = Settings()
    effective = (
        settings.model_copy(update={"diagram_assembly_enabled": True}) if args.probe else settings
    )
    repository = (
        PostgresRepository.from_settings(settings)
        if settings.database_url
        else (
            MemoryRepository() if settings.demo_mode and settings.app_env != "production" else None
        )
    )
    try:
        result = DiagramPreflight().check(repository, effective)
        print(
            json.dumps({"configured_enabled": settings.diagram_assembly_enabled, "probe": result})
        )
    finally:
        if repository is not None and hasattr(repository, "close"):
            repository.close()
    raise SystemExit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
