from __future__ import annotations

from datetime import date, datetime, timezone
import json

from .connectors import FeedError, Jobicy, utc_now
from .models import Job, canonical_json
from .storage import Store


STAGES = ("prepared", "sent_unconfirmed", "submitted", "screening", "interview", "offer",
          "rejected", "withdrawn", "closed", "discarded")
STAGE_LABELS = {
    "prepared": "Prepared", "sent_unconfirmed": "Sent; receipt unverified", "submitted": "Submitted",
    "screening": "Screening", "interview": "Interview", "offer": "Offer", "rejected": "Rejected",
    "withdrawn": "Withdrawn", "closed": "Closed", "discarded": "Discarded",
}
APPLIED_STAGES = {"submitted", "screening", "interview", "offer", "rejected", "withdrawn", "closed"}


def application_date(value: str, *, required=False) -> str:
    if not value and not required:
        return ""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Use an application date in YYYY-MM-DD format.") from exc
    if parsed > date.today():
        raise ValueError("The application date cannot be in the future.")
    return parsed.isoformat()


class Tracker:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def record(db, application_id: str, stage: str, provenance: str, evidence: str,
               *, applied_at: str | None = None, portal: str | None = None):
        if stage not in STAGES:
            raise ValueError("Choose a supported application stage.")
        previous = db.execute("SELECT * FROM application_tracking WHERE application_id=?", (application_id,)).fetchone()
        when = previous["applied_at"] if previous and applied_at is None else applied_at or ""
        origin = previous["portal"] if previous and portal is None else portal or ""
        now = utc_now()
        db.execute("INSERT OR REPLACE INTO application_tracking VALUES (?,?,?,?,?,?,?)",
                   (application_id, stage, when, origin, now, provenance, evidence))
        db.execute("INSERT INTO tracking_events(application_id,at,stage,provenance,evidence) VALUES (?,?,?,?,?)",
                   (application_id, now, stage, provenance, evidence))

    def update_stage(self, application_id: str, stage: str, *, applied_at: str | None = None, evidence: str = ""):
        when = application_date(applied_at) if applied_at is not None else None
        with self.store.connect() as db:
            if not db.execute("SELECT id FROM applications WHERE id=?", (application_id,)).fetchone():
                raise ValueError("Application does not exist.")
            self.record(db, application_id, stage, "user-reported",
                        evidence.strip() or "Stage entered by the user; not synchronized from the employer.",
                        applied_at=when)
            if stage in APPLIED_STAGES:
                row = db.execute("SELECT state FROM applications WHERE id=?", (application_id,)).fetchone()
                if row["state"] == "approved":
                    now = utc_now()
                    db.execute("UPDATE applications SET state='prepared',approved_digest=NULL,updated_at=? WHERE id=?",
                               (now, application_id))
                    db.execute("INSERT INTO events(application_id,at,state,message) VALUES (?,?,?,?)",
                               (application_id, now, "prepared", "Tracked progress changed; previous send approval cleared."))

    def summary(self, application: dict) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM application_tracking WHERE application_id=?", (application["id"],)).fetchone()
        if row:
            return dict(row)
        state = application["state"]
        stage = {"email_sent": "sent_unconfirmed", "submitted": "submitted", "discarded": "discarded"}.get(state, "prepared")
        return {"stage": stage, "applied_at": "", "portal": application["job"].source,
                "updated_at": application["updated_at"], "provenance": "legacy local record",
                "evidence": "No authorized employer application-stage synchronization is available."}

    def history(self, application_id: str) -> list[dict]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM tracking_events WHERE application_id=? ORDER BY id", (application_id,))]

    def availability(self, job: Job) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT data FROM listing_checks WHERE job_id=?", (job.id,)).fetchone()
        if row:
            result = json.loads(row["data"])
        else:
            result = {"status": job.hiring_status, "evidence": job.hiring_evidence or "No source hiring-status evidence.",
                      "checked_at": job.hiring_checked_at, "provenance": "source" if job.hiring_checked_at else "unverified",
                      "source": job.source, "source_url": job.url, "last_known_status": job.hiring_status}
        if result["status"] in {"open", "active"}:
            try:
                checked = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - checked).total_seconds() > 48 * 3600
            except (ValueError, TypeError):
                stale = True
            if stale:
                result = {**result, "status": "unknown", "evidence": "Evidence is over 48 hours old or undated. Last known: "
                          + result["status"] + ". " + result["evidence"]}
        return result

    def is_closed(self, job: Job) -> bool:
        result = self.availability(job)
        return result["status"] == "closed" or result.get("last_known_status") == "closed"

    def set_availability(self, job: Job, status: str, *, evidence: str, provenance: str = "user-reported",
                         checked_at: str = "", source: str = "", source_url: str = "") -> dict:
        if status not in {"active", "open", "closed", "unknown"}:
            raise ValueError("Choose active claim, open, closed, or unknown for the listing.")
        if status != "unknown" and not evidence.strip():
            raise ValueError("Record the source/reference for a listing availability claim.")
        previous = self.availability(job)
        result = {
            "status": status, "evidence": evidence, "provenance": provenance,
            "checked_at": checked_at or utc_now(), "source": source or job.source,
            "source_url": source_url or job.url,
            "last_known_status": status if status != "unknown" else previous.get("last_known_status", "unknown"),
        }
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO listing_checks VALUES (?,?)", (job.id, canonical_json(result)))
        return result

    def refresh_availability(self, job: Job) -> dict:
        if job.source != "jobicy":
            raise ValueError("Automatic listing checks are available only for Jobicy. Application-stage sync is unavailable.")
        now = datetime.now(timezone.utc).timestamp()
        key = "listing-attempt:" + job.source_id
        previous = self.store.get(key, {})
        if now - previous.get("at", 0) < 60:
            if previous.get("success"):
                return self.availability(job)
            raise FeedError("The last listing check failed. Wait one minute, then check again.")
        self.store.set(key, {"at": now, "success": False})
        try:
            record = Jobicy().check_status([job.source_id])[job.source_id]
        except FeedError as exc:
            self.set_availability(job, "unknown", evidence=str(exc), provenance="check failed",
                                  source_url="https://jobicy.com/api/jobs/status?ids=" + job.source_id)
            raise
        result = self.set_availability(job, record["status"], evidence=record["evidence"], provenance="source",
                                       checked_at=record["checked_at"],
                                       source_url="https://jobicy.com/api/jobs/status?ids=" + job.source_id)
        self.store.set(key, {"at": now, "success": True})
        return result


def hiring_badge(record: dict) -> str:
    status = record["status"]
    if status == "active":
        return "Active claim" + (" (you)" if record["provenance"] == "user-reported" else "")
    if status == "closed":
        return "Closed" + (" (you)" if record["provenance"] == "user-reported" else "")
    if status == "open":
        return "Open" + (" (you)" if record["provenance"] == "user-reported" else "")
    return "Unverified"
