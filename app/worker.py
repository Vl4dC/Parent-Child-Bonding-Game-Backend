"""Run durable generation jobs without holding a database connection during model calls."""

import argparse
import copy
import logging
import time
from uuid import uuid4

from app.config import Settings
from app.generation import model_settings, reserve
from app.jobs import execute
from app.repository import MemoryRepository, PostgresRepository, current_session, encode

log = logging.getLogger(__name__)


class StaleJob(Exception):
    """The source changed or another worker replaced the lease."""


def snapshot(repository, owner, job, lease):
    """Read the minimal owned source graph, then release the database transaction."""
    buffer = MemoryRepository()
    with repository.session(owner, serialize=False) as unit:
        current = unit.get("jobs", job["id"])
        if current is None or current.get("lease_token") != lease:
            return None
        if job.get("source_id"):
            source = unit.get(job["source_kind"], job["source_id"])
            if source is not None and job["source_kind"] == "quizzes":
                unit.get("lessons", source["lesson_id"])
        versions = {key: value for key, value in unit.versions.items() if key[0] != "jobs"}
        for (kind, key), value in unit.cache.items():
            if kind == "jobs" or value is None:
                continue
            data = copy.deepcopy(encode(value))
            if kind in ("quizzes", "tournaments"):
                data.pop("questions", None)  # immutable questions have their own buffered record
            buffer.data[(owner, kind, key)] = data
    return buffer, versions


def generate(buffer, owner, job, settings):
    """The generation engines see a detached unit of work; no PostgreSQL connection is held."""
    with buffer.session(owner) as unit:
        token = current_session.set(unit)
        model_token = model_settings.set(settings)
        try:
            return execute(job)
        finally:
            model_settings.reset(model_token)
            current_session.reset(token)


def commit_result(repository, owner, job, lease, buffer, original, versions, result):
    with repository.session(owner) as unit:
        current = unit.get("jobs", job["id"])
        if current is None or current.get("lease_token") != lease:
            raise StaleJob()
        for (kind, key), expected in sorted(versions.items()):
            row = unit.connection.execute(
                "select version from api_private.records where kind=%s and id=%s for update",
                (kind, key),
            ).fetchone()
            if row is None or row[0] != expected:
                raise StaleJob()
        for address, data in buffer.data.items():
            if address in original and data == original[address]:
                continue
            _, kind, key = address
            if kind == "question_sets":
                continue  # quiz persistence writes and validates its immutable snapshot
            if kind in ("quizzes", "tournaments"):
                data = {
                    **data,
                    "questions": buffer.data[(owner, "question_sets", data["question_set_id"])][
                        "questions"
                    ],
                }
            unit.put(kind, key, data)
        current.update(status="succeeded", result=result, error=None)


def work_once(repository, settings):
    lease = str(uuid4())
    with repository.control() as connection:
        claimed = connection.execute(
            "select api_private.claim_job(%s,%s,%s)",
            (lease, settings.job_attempt_limit, settings.generation_concurrency),
        ).fetchone()[0]
    if claimed is None:
        return False
    owner, job = claimed["owner_id"], claimed["job"]
    try:
        captured = snapshot(repository, owner, job, lease)
        if captured is None:
            return True
        buffer, versions = captured
        original = (
            buffer.data.copy()
        )  # buffered writes replace values; source values are never mutated
        reserve(repository, owner, settings.generation_daily_limit)
        result = generate(buffer, owner, job, settings)
        commit_result(repository, owner, job, lease, buffer, original, versions, result)
    except Exception:
        log.warning("Generation job failed; attempt %s", job["attempts"])
        with repository.session(owner) as unit:
            current = unit.get("jobs", job["id"])
            if current and current.get("lease_token") == lease:
                current.update(
                    status="queued" if job["attempts"] < settings.job_attempt_limit else "failed",
                    error="Generation failed. Check configuration or try again later.",
                )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if not settings.generation_enabled or not settings.database_url:
        parser.error("GENERATION_ENABLED=true and DATABASE_URL are required")
    repository = PostgresRepository.from_settings(settings)
    try:
        while True:
            found = work_once(repository, settings)
            if args.once:
                break
            if not found:
                time.sleep(1)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
