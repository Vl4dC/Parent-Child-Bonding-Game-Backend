"""Trusted operator bridge for externally reviewed, request-specific guardian authority."""

import argparse

from app.approval_service import ApprovalService
from app.config import Settings
from app.repository import PostgresRepository


def main():
    parser = argparse.ArgumentParser(description="Record an externally reviewed authority grant")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--guardian-id", required=True)
    parser.add_argument("--evidence-reference", required=True)
    parser.add_argument("--lifetime-seconds", type=int, default=3600)
    args = parser.parse_args()
    settings = Settings()
    if not settings.learner_approval_enabled or not settings.database_url:
        parser.error("Enabled learner approval and PostgreSQL are required")
    repository = PostgresRepository.from_settings(settings)
    try:
        service = ApprovalService(repository, settings.learner_approval_secret.get_secret_value())
        service.grant_authority(
            args.request_id, args.guardian_id, args.evidence_reference, args.lifetime_seconds
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
