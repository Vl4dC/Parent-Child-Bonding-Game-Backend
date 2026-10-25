"""Read-only deployment gate for Clerk, duel and card-attempt schema, optional renderer."""

import argparse
import json

from app.clerk_auth import ClerkVerifier
from app.config import Settings
from app.diagram_preflight import DiagramPreflight
from app.practice_readiness import check
from app.repository import PostgresRepository


def inspect(settings, repository, verifier, *, renderer=False):
    if not settings.clerk_issuer:
        return {"ready": False, "reason": "clerk_configuration_required"}
    if repository is None or not repository.durable:
        return {"ready": False, "reason": "durable_database_required"}
    try:
        storage = bool(repository.ready())
    except Exception:
        storage = False
    effective = settings.model_copy(
        update={
            "duels_enabled": True,
            "flashcard_attempts_enabled": True,
            "diagram_assembly_enabled": renderer,
        }
    )
    features = {
        "clerk": verifier.readiness(),
        "duels": check(repository, effective, "duels"),
        "flashcard_attempts": check(repository, effective, "flashcard_attempts"),
    }
    if renderer:
        features["diagram_assembly"] = DiagramPreflight().check(repository, effective)
    return {
        "ready": storage and all(f["ready"] for f in features.values()),
        "durable": True,
        "storage_ready": storage,
        "features": features,
        "configured_enabled": {
            "duels": settings.duels_enabled,
            "flashcard_attempts": settings.flashcard_attempts_enabled,
            "diagram_assembly": settings.diagram_assembly_enabled,
        },
        "configuration_changed": False,
        "migrations_applied": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--renderer",
        action="store_true",
        help="Also render a fixed synthetic diagram; never uses saved learner content",
    )
    args = parser.parse_args()
    repository = verifier = None
    try:
        settings = Settings()
        verifier = ClerkVerifier(settings)
        repository = PostgresRepository.from_settings(settings) if settings.database_url else None
        result = inspect(settings, repository, verifier, renderer=args.renderer)
    except Exception:
        result = {"ready": False, "reason": "configuration_or_dependency_unavailable"}
    finally:
        if verifier is not None:
            verifier.close()
        if repository is not None:
            repository.close()
    print(json.dumps(result))
    raise SystemExit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
