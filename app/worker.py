"""Run one durable generation job at a time: uv run python -m app.worker --once."""

import argparse
import logging
import time
from uuid import uuid4

import psycopg

from app.config import Settings
from app.generation import model_settings, reserve
from app.jobs import execute
from app.repository import PostgresRepository, current_session

log = logging.getLogger(__name__)


def work_once(repository, settings):
    lease = str(uuid4())
    with psycopg.connect(repository.url) as connection:
        claimed = connection.execute(
            "select api_private.claim_job(%s,%s,%s)",
            (lease, settings.job_attempt_limit, settings.generation_concurrency),
        ).fetchone()[0]
    if claimed is None:
        return False
    owner, job = claimed["owner_id"], claimed["job"]
    try:
        with repository.session(owner) as unit:
            # Recheck after taking the owner's lock: deletion/cancellation may have won
            # between claiming and execution. A plain read keeps status polling responsive.
            current_lease = unit.connection.execute(
                "select data->>'lease_token' from api_private.records where kind='jobs' and id=%s",
                (job["id"],),
            ).fetchone()
            if current_lease is None or current_lease[0] != lease:
                return True
            reserve(repository, owner, settings.generation_daily_limit)
            token = current_session.set(unit)
            model_token = model_settings.set(settings)
            try:
                result = execute(job)
                current = unit.get("jobs", job["id"])
                if current["lease_token"] != lease:
                    raise RuntimeError("Worker lease was replaced")
                current.update(status="succeeded", result=result, error=None)
            finally:
                model_settings.reset(model_token)
                current_session.reset(token)
    except Exception:
        # Do not log raw model/provider exceptions or include them in job output.
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
    repository = PostgresRepository(settings.database_url.get_secret_value())
    while True:
        found = work_once(repository, settings)
        if args.once:
            break
        if not found:
            time.sleep(1)


if __name__ == "__main__":
    main()
