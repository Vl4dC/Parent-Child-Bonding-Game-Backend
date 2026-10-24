"""Pure lifecycle/source rules shared by the game and explicit memory adapter."""

import hashlib
import json

SNAPSHOT_VERSION = "mermaid-12.2.0-frozen-v1"
TERMINAL = {"finished", "expired", "cancelled"}


def source_fingerprint(visual, revision):
    value = {"visual": visual, "lesson_revision": revision, "snapshot": SNAPSHOT_VERSION}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_reason(lesson, template):
    if lesson is None:
        return "source_unavailable"
    if lesson.get("status") != "approved":
        return "source_unapproved"
    visual = next((v for v in lesson.get("visuals", []) if v["id"] == template["visual_id"]), None)
    if visual is None or visual.get("kind") != "diagram":
        return "source_changed"
    if source_fingerprint(visual, lesson.get("revision", 1)) != template["source_hash"]:
        return "source_changed"
    return None


def phase(race, players, now):
    if race["status"] in TERMINAL or race["status"] == "waiting":
        return race["status"]
    if all(p["completed_at"] is not None or p["forfeited"] for p in players):
        return "finished"
    if now < race["started_at"]:
        return "countdown"
    if now >= race["deadline_at"]:
        return "expired"
    return "running"


def invalidate(race, players, now, reason):
    """Monotonic transition; a revoked source never reopens on later reapproval."""
    if race["status"] in TERMINAL:
        return False
    current = phase(race, players, now)
    if current in ("finished", "expired"):
        race["status"] = current
        race["terminal_reason"] = "deadline_elapsed" if current == "expired" else None
    else:
        race["status"] = "cancelled"
        race["terminal_reason"] = reason
    race["version"] += 1
    return True
