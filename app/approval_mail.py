"""Explicit, bounded outbox worker. API startup never sends mail."""

import argparse
import smtplib
import ssl
from email.message import EmailMessage

from pydantic import EmailStr, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.approval_service import ApprovalService
from app.config import Settings
from app.repository import PostgresRepository


class SMTPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APPROVAL_SMTP_", hide_input_in_errors=True)
    host: str
    port: int = Field(465, ge=1, le=65535)
    username: str
    password: SecretStr
    sender: EmailStr


class SMTPSender:
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, message):
        email = EmailMessage()
        email["From"] = str(self.settings.sender)
        email["To"] = message["recipient"]
        email["Subject"] = "QuestNest learner approval request"
        domain = str(self.settings.sender).rsplit("@", 1)[1]
        email["Message-ID"] = f"<{message['mail_id']}@{domain}>"
        email.set_content(
            "A learner registration request is waiting for review.\n"
            f"Request ID: {message['request_id']}\n"
            f"One-time approval proof: {message['proof']}\n\n"
            "Review using the request ID, recipient email and current approval proof. "
            "Mailbox access alone does not establish guardian authority. "
            "Ignore this email if you did not expect it. "
            "Approval does not activate a learner account.\n"
        )
        with smtplib.SMTP_SSL(
            self.settings.host, self.settings.port, timeout=10, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(self.settings.username, self.settings.password.get_secret_value())
            refused = smtp.send_message(email)
            if refused:
                raise RuntimeError("Recipient was not accepted")


def deliver_one(service, sender):
    claimed = service.claim_mail()
    if claimed is None:
        return False
    mail, record, lease = claimed
    message = {
        "mail_id": mail["id"],
        "request_id": record["id"],
        "recipient": record["guardian_email"],
        "proof": service.proof(record),
    }
    success = False
    try:
        # No database connection/lock is held across SMTP I/O. Never log the message or error.
        sender(message)
        success = True
    except Exception:
        pass
    service.finish_mail(mail["id"], lease, success)
    return True


def main():
    parser = argparse.ArgumentParser(description="Deliver at most one learner-approval email")
    parser.add_argument("--once", required=True, action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if not args.once or not settings.learner_approval_enabled or not settings.database_url:
        parser.error("Enabled learner approval and PostgreSQL are required")
    mail_settings = SMTPSettings()
    repository = PostgresRepository.from_settings(settings)
    try:
        service = ApprovalService(repository, settings.learner_approval_secret.get_secret_value())
        deliver_one(service, SMTPSender(mail_settings))
    finally:
        repository.close()


if __name__ == "__main__":
    main()
