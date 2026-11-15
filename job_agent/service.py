from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
import uuid

from .connectors import Connector, FeedError, utc_now
from .email_apply import EmailSender, MailAccount, validate_email_delivery
from .models import Job, Profile, canonical_json, deduplicate, digest
from .storage import Store
from .tracking import APPLIED_STAGES, STAGES, Tracker, application_date


TERMINAL = {"submitted", "email_sent", "unknown", "submitting"}


@dataclass
class SearchResult:
    jobs: list[Job]
    messages: list[str]
    failed_sources: int = 0


def discover(store: Store, sources: dict[str, Connector]) -> SearchResult:
    jobs, messages = [], []
    failed_sources = 0
    now = datetime.now(timezone.utc).timestamp()
    for source in sources.values():
        cache = store.cache(source.id)
        if cache and now - cache[0] < source.cache_seconds:
            jobs.extend(cache[1])
            messages.append(f"{source.label}: {len(cache[1])} cached listings (refresh interval respected).")
            continue
        # Record the attempt time, not just successes, to avoid repeated requests on errors.
        attempted = store.get("attempt:" + source.id, 0)
        if now - attempted < source.cache_seconds:
            failed_sources += 1
            if cache:
                jobs.extend(cache[1])
            messages.append(f"{source.label}: previous refresh failed; waiting for refresh interval. "
                            + ("Showing stale cached jobs." if cache else "No cached jobs."))
            continue
        store.set("attempt:" + source.id, now)
        try:
            result = source.discover()
            store.cache_jobs(source.id, result.jobs)
            jobs.extend(result.jobs)
            messages.append(f"{source.label}: {len(result.jobs)} listings; {result.rejected} malformed listings rejected.")
        except FeedError as exc:
            failed_sources += 1
            if cache:
                jobs.extend(cache[1])
            messages.append(f"{source.label}: {exc}" + (" Showing stale cached jobs." if cache else ""))
    unique = deduplicate(jobs)
    store.save_jobs(unique)
    return SearchResult(unique, messages, failed_sources)


class Applications:
    def __init__(self, store: Store, connectors: dict[str, Connector] | None = None, *, recover_interrupted=False,
                 email_sender: EmailSender | None = None):
        self.store = store
        self.connectors = connectors or {}
        self.email_sender = email_sender or EmailSender()
        self.tracker = Tracker(store)
        # An interrupted in-flight attempt cannot safely be retried.
        with store.connect() as db:
            rows = db.execute("SELECT id FROM applications WHERE state='submitting'").fetchall() if recover_interrupted else []
            for row in rows:
                self._state(db, row["id"], "unknown",
                            "App restarted during submission. Verify with the employer before any further action.")

    def _state(self, db, application_id: str, state: str, message: str, receipt: str = ""):
        now = utc_now()
        db.execute("UPDATE applications SET state=?,updated_at=?,receipt=? WHERE id=?",
                   (state, now, receipt, application_id))
        db.execute("INSERT INTO events(application_id,at,state,message) VALUES (?,?,?,?)",
                   (application_id, now, state, message))
        if state == "email_sent":
            self.tracker.record(db, application_id, "sent_unconfirmed", "outgoing SMTP server",
                                "Email accepted for delivery; employer receipt/application stage is unverified.")
        elif state == "submitted":
            reported = receipt.startswith(("user-", "user:"))
            self.tracker.record(db, application_id, "submitted", "user-reported" if reported else "provider receipt",
                                message, applied_at=now[:10])
        elif state == "discarded":
            self.tracker.record(db, application_id, "discarded", "user-reported", "Discarded locally; no external action.")

    def prepare(self, job: Job, profile: Profile) -> tuple[str, bool]:
        profile.validate()
        payload = {
            "job": {"title": job.title, "company": job.company, "location": job.location, "url": job.url, "source": job.source,
                    "source_id": job.source_id},
            "candidate": {"name": profile.name, "email": profile.email,
                          "titles": profile.titles, "skills": profile.skills, "experience": profile.experience},
            "resume": dict(profile.resume),
            "cover_letter": f"Please consider my application for {job.title} at {job.company}. "
                            f"My resume is attached.\n\n{profile.name}".strip(),
            "answers": {},
        }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key in job.keys:
                existing = db.execute("SELECT application_id FROM application_keys WHERE key=?", (key,)).fetchone()
                if existing:
                    db.executemany("INSERT OR IGNORE INTO application_keys VALUES (?,?)",
                                   [(alias, existing["application_id"]) for alias in job.keys])
                    return existing["application_id"], False
            application_id = str(uuid.uuid4())
            db.execute("INSERT INTO applications(id,job,payload,state,updated_at) VALUES (?,?,?,?,?)",
                       (application_id, canonical_json(job.to_dict()), canonical_json(payload), "prepared", utc_now()))
            db.executemany("INSERT INTO application_keys VALUES (?,?)",
                           [(key, application_id) for key in job.keys])
            self._state(db, application_id, "prepared", "Prepared locally; nothing sent.")
            self.tracker.record(db, application_id, "prepared", "local preparation", "Nothing sent.", portal=job.source)
        return application_id, True

    def discard(self, application_id: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] in TERMINAL:
                raise ValueError("A sent/uncertain application cannot be discarded. Update its tracked stage instead.")
            db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))
            self._state(db, application_id, "discarded", "Discarded by the user; nothing sent.")

    def import_external(self, job: Job, *, portal: str, applied_at: str, stage: str, evidence: str = "") -> tuple[str, bool]:
        when = application_date(applied_at, required=True)
        if not portal.strip() or len(portal) > 120:
            raise ValueError("Enter the portal where you already applied.")
        if stage not in STAGES or stage in {"prepared", "discarded"}:
            raise ValueError("Choose the current stage of the existing application.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = None
            for key in job.keys:
                row = db.execute("SELECT application_id FROM application_keys WHERE key=?", (key,)).fetchone()
                if row:
                    existing = row["application_id"]
                    break
            application_id = existing or str(uuid.uuid4())
            if existing is None:
                payload = {"job": {"title": job.title, "company": job.company, "location": job.location, "url": job.url, "source": job.source,
                                   "source_id": job.source_id},
                           "candidate": {"name": "", "email": "", "titles": [], "skills": [], "experience": ""},
                           "resume": {}, "cover_letter": "", "answers": {}, "delivery": {"method": "external"}}
                db.execute("INSERT INTO applications(id,job,payload,state,updated_at) VALUES (?,?,?,?,?)",
                           (application_id, canonical_json(job.to_dict()), canonical_json(payload), "submitted", utc_now()))
            current = self._get(db, application_id)
            if current["state"] not in TERMINAL or existing is None:
                db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))
                self._state(db, application_id, "submitted",
                            "User reports applying outside this app via " + portal.strip() + "; not independently verified.",
                            "user-confirmed-external")
            db.executemany("INSERT OR IGNORE INTO application_keys VALUES (?,?)",
                           [(key, application_id) for key in job.keys])
            self.tracker.record(db, application_id, stage, "user-reported",
                                f"External record via {portal.strip()}; applied {when}. "
                                + (evidence.strip() or "User reports applying outside this agent; automatic stage sync unavailable."),
                                applied_at=when, portal=portal.strip())
        if existing is None:
            self.store.save_jobs([job])
        return application_id, existing is None

    def get(self, application_id: str) -> dict:
        with self.store.connect() as db:
            return self._get(db, application_id)

    def _get(self, db, application_id: str) -> dict:
        row = db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row:
            raise ValueError("Application does not exist.")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["job"] = Job.from_dict(json.loads(value["job"]))
        return value

    def list(self) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute("SELECT id FROM applications ORDER BY updated_at DESC").fetchall()
            return [self._get(db, row["id"]) for row in rows]

    def events(self, application_id: str) -> list[dict]:
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events WHERE application_id=? ORDER BY id",
                                                   (application_id,))]

    def edit(self, application_id: str, *, name: str, email: str, cover_letter: str, answers: dict,
             profile: Profile | None = None) -> bool:
        if not isinstance(answers, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in answers.items()):
            raise ValueError("Answers must be a JSON object mapping question names to text answers.")
        if len(canonical_json(answers)) > 50_000 or len(cover_letter) > 50_000:
            raise ValueError("Application answers or letter exceed the 50,000-character limit.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] in TERMINAL:
                raise ValueError("Submitted or uncertain applications are locked. Do not retry blindly.")
            payload = current["payload"]
            original = canonical_json(payload)
            if profile is not None:
                profile.validate()
                payload["candidate"].update(titles=profile.titles, skills=profile.skills, experience=profile.experience)
                payload["resume"] = dict(profile.resume)
            payload["candidate"].update(name=name.strip(), email=email.strip())
            payload["cover_letter"] = cover_letter
            payload["answers"] = answers
            if canonical_json(payload) == original:
                return False
            db.execute("UPDATE applications SET payload=?,approved_digest=NULL,attempt_key=NULL WHERE id=?",
                       (canonical_json(payload), application_id))
            self._state(db, application_id, "prepared", "Payload edited. Previous approval invalidated.")
        return True

    def _validate_payload(self, payload: dict, posting: Job):
        if self.tracker.is_closed(posting):
            raise ValueError("This listing was reported closed. Recheck availability before applying.")
        candidate = payload["candidate"]
        if not candidate["name"].strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate["email"]):
            raise ValueError("Approval requires your name and valid email. No identity facts are invented.")
        self.store.verify_resume(payload["resume"])
        delivery = payload.get("delivery", {})
        if delivery.get("method") == "email":
            self.email_sender.validate(payload)
            return
        if delivery and delivery.get("method") != "manual":
            raise ValueError("Unsupported delivery method. Choose email (only when advertised) or manual website.")
        connector = self.connectors.get(payload["job"]["source"])
        if connector and connector.capabilities.automatic_submission:
            missing = connector.missing_answers(json.loads(canonical_json(payload)))
            if missing:
                raise ValueError("Missing mandatory answers: " + ", ".join(missing))

    def set_delivery(self, application_id: str, *, recipient: str = "", subject: str = "",
                     instructions: str = "", employer_accepts_email: bool = False,
                     account: MailAccount | None = None, email: bool = False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] in TERMINAL:
                raise ValueError("Sent or uncertain applications are locked. Do not retry blindly.")
            payload = current["payload"]
            original = canonical_json(payload)
            if email:
                if account is None:
                    raise ValueError("Connect your mail account before selecting email delivery.")
                account.validate()
                payload["delivery"] = {
                    "method": "email", "recipient": recipient.strip(), "subject": subject.strip(),
                    "instructions": instructions.strip(), "employer_accepts_email": employer_accepts_email,
                    "account": asdict(account),
                }
                # The message may still be a draft; complete content is checked again at approval.
                draft = {**payload, "cover_letter": payload["cover_letter"] or "Draft"}
                validate_email_delivery(draft)
            else:
                payload["delivery"] = {"method": "manual"}
            if canonical_json(payload) == original:
                return False
            db.execute("UPDATE applications SET payload=?,approved_digest=NULL,attempt_key=NULL WHERE id=?",
                       (canonical_json(payload), application_id))
            self._state(db, application_id, "prepared", "Delivery changed. Previous approval invalidated; nothing sent.")
            return True

    def approve(self, application_id: str, expected_digest: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] not in {"prepared", "rejected", "needs_input", "approved"}:
                raise ValueError("Only a prepared/rejected application can be approved. Edit a handoff to prepare again.")
            if expected_digest != digest(current["payload"]):
                raise ValueError("The payload changed since review. Review the current revision before approving.")
            self._validate_payload(current["payload"], current["job"])
            stage = self.tracker.summary(current)["stage"]
            if stage in APPLIED_STAGES:
                raise ValueError("The tracker reports this application already progressed. Verify that record instead of applying again.")
            db.execute("UPDATE applications SET approved_digest=? WHERE id=?",
                       (digest(current["payload"]), application_id))
            self._state(db, application_id, "approved", "User approved this exact payload and résumé hash.")

    def dispatch(self, application_id: str) -> tuple[str, str]:
        initial = self.get(application_id)
        if initial["state"] != "approved" or initial["approved_digest"] != digest(initial["payload"]):
            raise ValueError("This exact application must be approved before continuing.")
        if self.tracker.is_closed(initial["job"]):
            raise ValueError("This listing was reported closed. Do not send an application.")
        if self.tracker.summary(initial)["stage"] in APPLIED_STAGES:
            raise ValueError("The tracker reports this application already progressed. Verify that record instead of sending again.")
        if initial["payload"].get("delivery", {}).get("method") == "email" and initial["job"].source == "jobicy":
            try:
                availability = self.tracker.refresh_availability(initial["job"])
            except FeedError:
                availability = {"status": "unknown"}
            if availability["status"] in {"unknown", "closed"}:
                with self.store.connect() as db:
                    current = self._get(db, application_id)
                    if current["state"] == "approved" and current["approved_digest"] == initial["approved_digest"]:
                        db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))
                        self._state(db, application_id, "needs_input",
                                    "Listing availability is " + availability["status"] + "; verify the original posting before renewed approval.")
                return "needs_input", "Listing availability is " + availability["status"] + "; email was not sent."
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            payload = current["payload"]
            if current["state"] != "approved" or current["approved_digest"] != digest(payload):
                raise ValueError("This exact application must be approved before continuing.")
            self._validate_payload(payload, current["job"])
            if self.tracker.summary(current)["stage"] in APPLIED_STAGES:
                raise ValueError("Application stage changed since review. Verify the tracked application instead of sending again.")
            email_delivery = payload.get("delivery", {}).get("method") == "email"
            connector = self.connectors.get(current["job"].source)
            if not email_delivery and (not connector or not connector.capabilities.automatic_submission):
                message = ("Manual website: no authorized portal submission connection. "
                           "Open the employer page, attach the approved "
                           "résumé, copy the reviewed content and answer mandatory questions. Login/CAPTCHA must be "
                           "completed by you. Nothing has been submitted by this app.")
                self._state(db, application_id, "manual_handoff", message)
                return "manual_handoff", message
            attempt_key = str(uuid.uuid4())
            db.execute("UPDATE applications SET attempt_key=? WHERE id=?", (attempt_key, application_id))
            self._state(db, application_id, "submitting", "Submission attempt durably recorded before network access.")
        try:
            sender = self.email_sender if email_delivery else connector
            result = sender.submit(json.loads(canonical_json(payload)), attempt_key)
            if result.outcome not in {"accepted", "sent", "rejected", "needs_input", "unknown"}:
                raise ValueError("Submission integration returned an invalid outcome.")
            if result.outcome in {"accepted", "sent"} and not result.receipt:
                raise ValueError("Submission integration did not return a confirmation receipt.")
            state = {"accepted": "submitted", "sent": "email_sent", "rejected": "rejected",
                     "needs_input": "needs_input", "unknown": "unknown"}[result.outcome]
            message, receipt = result.message, result.receipt
        except Exception as exc:
            # Once the network boundary is crossed, even a timeout may mean the server accepted it.
            state, message, receipt = "unknown", f"Submission outcome uncertain ({type(exc).__name__}). Verify with the employer; no automatic retry.", ""
        with self.store.connect() as db:
            self._state(db, application_id, state, message, receipt)
            db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))
        return state, message

    def confirm_manual(self, application_id: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] != "manual_handoff":
                raise ValueError("Only a manual handoff can be recorded as user-confirmed submitted.")
            self._state(db, application_id, "submitted",
                        "User reports completion on the employer website; not independently verified by the app.",
                        "user-confirmed-manual")
            db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))

    def reconcile(self, application_id: str, *, submitted: bool, evidence: str):
        if not evidence.strip():
            raise ValueError("Record employer confirmation/reference before resolving an uncertain outcome.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] != "unknown":
                raise ValueError("Only an uncertain submission can be reconciled.")
            self._state(db, application_id, "submitted" if submitted else "rejected",
                        "User verified outcome with employer: " + evidence,
                        "user-verified:" + evidence if submitted else "")
            db.execute("UPDATE applications SET approved_digest=NULL WHERE id=?", (application_id,))
