"""Pending learner approval, with no account activation or automatic family access."""

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.approval_store import ApprovalStore

REQUEST_LIFETIME = 7 * 86400
MESSAGE = "If eligible, the guardian will receive instructions."


class ApprovalService:
    def __init__(self, repository, secret, clock=time.time):
        self.store = ApprovalStore(repository)
        self.secret = secret.encode()
        self.clock = clock

    def digest(self, *parts):
        return hmac.new(
            self.secret,
            json.dumps(parts, separators=(",", ":"), sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()

    def receipt(self, request_id):
        return "approval_status_" + self.digest("receipt", request_id)

    def proof(self, record):
        return self.digest("guardian-proof", record["id"], record["generation"])

    def fingerprint(self, payload):
        return self.digest("payload", payload)

    def charge(self, tx, scope, maximum, window):
        now = int(self.clock())
        bucket = now // window
        key = self.digest("limit", scope, window, bucket)
        entry = tx.get("limits", key) or {"count": 0, "expires_at": (bucket + 1) * window}
        if entry["count"] >= maximum:
            raise HTTPException(
                429, "Too many attempts. Please wait.", headers={"Retry-After": str(window)}
            )
        entry["count"] += 1
        tx.put("limits", key, entry)

    def reserve(self, scope, maximum=120, window=60):
        # Commit independently of the operation so invalid proofs/receipts consume limits too.
        with self.store.transaction() as tx:
            self.charge(tx, scope, maximum, window)

    def state(self, record):
        if record["status"] == "pending" and record["expires_at"] <= self.clock():
            return "expired"
        return record["status"]

    def public(self, record):
        return {
            "request_id": record["id"],
            "status": self.state(record),
            "version": record["version"],
            "expires_at": record["expires_at"],
            "activated": False,
        }

    def queue(self, tx, record):
        mail_id = str(uuid4())
        tx.put(
            "outbox",
            mail_id,
            {
                "id": mail_id,
                "request_id": record["id"],
                "generation": record["generation"],
                "status": "queued",
                "attempts": 0,
                "next_attempt_at": int(self.clock()),
                "lease_until": 0,
            },
        )

    def create(self, payload, key, client):
        now = int(self.clock())
        idem = self.digest("create", client, key)
        fingerprint = self.fingerprint(payload)
        with self.store.transaction() as tx:
            saved = tx.get("idempotency", idem)
            if saved:
                if not hmac.compare_digest(saved["fingerprint"], fingerprint):
                    raise HTTPException(409, "Idempotency key was used for different details.")
                record = tx.get("requests", saved["request_id"])
                if record is None or record["expires_at"] <= now:
                    raise HTTPException(410, "Registration request has expired.")
                return saved["response"] | {"receipt": self.receipt(record["id"])}
            self.charge(tx, "recipient:" + payload["guardian_email"], 3, 3600)
            self.charge(tx, "recipient-day:" + payload["guardian_email"], 10, 86400)
            self.charge(tx, "create-client:" + client, 10, 3600)
            self.charge(tx, "create-global", 1000, 3600)
            request_id = str(uuid4())
            record = {
                "id": request_id,
                **payload,
                "receipt_hash": self.digest("receipt-hash", self.receipt(request_id)),
                "status": "pending",
                "version": 1,
                "generation": 1,
                "created_at": now,
                "expires_at": now + REQUEST_LIFETIME,
                "last_sent_at": now,
            }
            tx.put("requests", request_id, record)
            self.queue(tx, record)
            response = self.public(record) | {"message": MESSAGE}
            tx.put(
                "idempotency",
                idem,
                {"fingerprint": fingerprint, "request_id": request_id, "response": response},
            )
            return response | {"receipt": self.receipt(request_id)}

    def owned_receipt(self, tx, request_id, receipt):
        record = tx.get("requests", request_id)
        if not record or not hmac.compare_digest(
            record["receipt_hash"], self.digest("receipt-hash", receipt)
        ):
            raise HTTPException(404, "Registration request not found.")
        if record["expires_at"] <= self.clock():
            raise HTTPException(410, "Registration receipt has expired.")
        return record

    def status(self, request_id, receipt):
        with self.store.transaction() as tx:
            return self.public(self.owned_receipt(tx, request_id, receipt))

    def resend(self, request_id, receipt, key):
        with self.store.transaction() as tx:
            record = self.owned_receipt(tx, request_id, receipt)
            idem = self.digest("resend", request_id, key)
            if tx.get("idempotency", idem):
                return {"message": MESSAGE}
            if self.state(record) != "pending":
                raise HTTPException(409, "Registration request is no longer pending.")
            now = int(self.clock())
            if now < record["last_sent_at"] + 60:
                raise HTTPException(429, "Wait before resending.", headers={"Retry-After": "60"})
            self.charge(tx, "recipient:" + record["guardian_email"], 3, 3600)
            self.charge(tx, "recipient-day:" + record["guardian_email"], 10, 86400)
            record["generation"] += 1
            record["version"] += 1
            record["last_sent_at"] = now
            tx.put("requests", request_id, record)
            self.queue(tx, record)
            tx.put("idempotency", idem, {"request_id": request_id})
            return {"message": MESSAGE}

    def authority(self, tx, record, user_id):
        grant = tx.get("authority", record["id"] + ":" + user_id)
        return bool(grant and grant["expires_at"] > self.clock())

    def grant_authority(self, request_id, user_id, evidence_reference, lifetime=3600):
        """Trusted operator only, after external authority review; never an HTTP self-grant."""
        user_id = str(UUID(user_id))
        if not evidence_reference.strip() or not 1 <= lifetime <= REQUEST_LIFETIME:
            raise ValueError("An authority evidence reference and bounded lifetime are required")
        with self.store.transaction() as tx:
            record = tx.get("requests", request_id)
            if not record or self.state(record) != "pending":
                raise ValueError("A pending request is required")
            tx.put(
                "authority",
                request_id + ":" + user_id,
                {
                    "request_id": request_id,
                    "guardian_id": user_id,
                    "evidence_hash": self.digest("authority-evidence", evidence_reference),
                    "expires_at": min(record["expires_at"], int(self.clock()) + lifetime),
                },
            )

    def list_requests(self, email, user_id, after, limit):
        with self.store.transaction() as tx:
            rows = tx.list("requests", after=after, limit=limit + 1, email=email)
            return {
                "items": [
                    self.public(record)
                    | {
                        "learner_display_name": record["learner_display_name"],
                        "learner_birth_year": record["learner_birth_year"],
                        "can_decide": self.state(record) == "pending"
                        and self.authority(tx, record, user_id),
                    }
                    for _, record in rows[:limit]
                ],
                "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
            }

    def decide(self, request_id, email, user_id, payload, key):
        with self.store.transaction() as tx:
            record = tx.get("requests", request_id)
            if not record or record["guardian_email"] != email:
                raise HTTPException(404, "Registration request not found.")
            if not self.authority(tx, record, user_id):
                raise HTTPException(403, "Request-specific guardian authority is required.")
            idem = self.digest("decision", request_id, user_id, key)
            fingerprint = self.fingerprint(payload)
            saved = tx.get("decisions", idem)
            if saved:
                if not hmac.compare_digest(saved["fingerprint"], fingerprint):
                    raise HTTPException(409, "Conflicting decision retry.")
                return saved["response"]
            if self.state(record) != "pending" or record["version"] != payload["expected_version"]:
                raise HTTPException(409, "Request changed or is no longer pending.")
            if not hmac.compare_digest(self.proof(record).encode(), payload["proof"].encode()):
                raise HTTPException(403, "Invalid or superseded approval proof.")
            record["status"] = "approved" if payload["decision"] == "approve" else "declined"
            record["version"] += 1
            record["decided_by"] = user_id
            record["decided_at"] = int(self.clock())
            tx.put("requests", request_id, record)
            response = self.public(record)
            tx.put("decisions", idem, {"fingerprint": fingerprint, "response": response})
            return response

    def claim_mail(self):
        now = int(self.clock())
        with self.store.transaction() as tx:
            # Bounded indexed scan: delivered history never lengthens the claim transaction.
            for key, mail in tx.due_mail(now):
                record = tx.get("requests", mail["request_id"])
                if (
                    not record
                    or self.state(record) != "pending"
                    or record["generation"] != mail["generation"]
                ):
                    mail["status"] = "cancelled"
                    tx.put("outbox", key, mail)
                    continue
                if mail["attempts"] >= 3:
                    mail["status"] = "failed"
                    tx.put("outbox", key, mail)
                    continue
                lease = secrets.token_hex(24)
                mail.update(
                    status="running",
                    attempts=mail["attempts"] + 1,
                    lease_until=now + 60,
                    lease_hash=self.digest("mail-lease", lease),
                )
                tx.put("outbox", key, mail)
                return mail, record, lease
            return None

    def finish_mail(self, mail_id, lease, success):
        with self.store.transaction() as tx:
            mail = tx.get("outbox", mail_id)
            if not mail or not hmac.compare_digest(
                mail.get("lease_hash", ""), self.digest("mail-lease", lease)
            ):
                return False
            if mail["status"] != "running" or mail["lease_until"] <= self.clock():
                return False
            mail.update(
                status="sent" if success else ("failed" if mail["attempts"] >= 3 else "queued"),
                lease_until=0,
                next_attempt_at=int(self.clock()) + 60 * 2 ** (mail["attempts"] - 1),
            )
            mail.pop("lease_hash", None)
            tx.put("outbox", mail_id, mail)
            return True

    @staticmethod
    def birth_year_is_plausible(year):
        # Intake only, not a legal cutoff or proof of age. Exact eligibility remains undecided.
        current_year = datetime.now(timezone.utc).year
        return current_year - 120 <= year <= current_year
