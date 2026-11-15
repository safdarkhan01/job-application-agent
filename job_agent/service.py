from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import uuid

from .connectors import Connector, FeedError, utc_now
from .models import Job, Profile, canonical_json, deduplicate, digest
from .storage import Store


TERMINAL = {"submitted", "unknown", "submitting"}


@dataclass
class SearchResult:
    jobs: list[Job]
    messages: list[str]


def discover(store: Store, sources: dict[str, Connector]) -> SearchResult:
    jobs, messages = [], []
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
            if cache:
                jobs.extend(cache[1])
            messages.append(f"{source.label}: {exc}" + (" Showing stale cached jobs." if cache else ""))
    unique = deduplicate(jobs)
    store.save_jobs(unique)
    return SearchResult(unique, messages)


class Applications:
    def __init__(self, store: Store, connectors: dict[str, Connector] | None = None, *, recover_interrupted=False):
        self.store = store
        self.connectors = connectors or {}
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

    def prepare(self, job: Job, profile: Profile) -> tuple[str, bool]:
        profile.validate()
        payload = {
            "job": {"title": job.title, "company": job.company, "url": job.url, "source": job.source,
                    "source_id": job.source_id},
            "candidate": {"name": profile.name, "email": profile.email,
                          "titles": profile.titles, "skills": profile.skills, "experience": profile.experience},
            "resume": dict(profile.resume), "cover_letter": "", "answers": {},
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
        return application_id, True

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

    def _validate_payload(self, payload: dict):
        candidate = payload["candidate"]
        if not candidate["name"].strip() or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate["email"]):
            raise ValueError("Approval requires your name and valid email. No identity facts are invented.")
        self.store.verify_resume(payload["resume"])
        connector = self.connectors.get(payload["job"]["source"])
        if connector and connector.capabilities.automatic_submission:
            missing = connector.missing_answers(json.loads(canonical_json(payload)))
            if missing:
                raise ValueError("Missing mandatory answers: " + ", ".join(missing))

    def approve(self, application_id: str, expected_digest: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            if current["state"] not in {"prepared", "rejected", "approved"}:
                raise ValueError("Only a prepared/rejected application can be approved. Edit a handoff to prepare again.")
            if expected_digest != digest(current["payload"]):
                raise ValueError("The payload changed since review. Review the current revision before approving.")
            self._validate_payload(current["payload"])
            db.execute("UPDATE applications SET approved_digest=? WHERE id=?",
                       (digest(current["payload"]), application_id))
            self._state(db, application_id, "approved", "User approved this exact payload and résumé hash.")

    def dispatch(self, application_id: str) -> tuple[str, str]:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, application_id)
            payload = current["payload"]
            if current["state"] != "approved" or current["approved_digest"] != digest(payload):
                raise ValueError("This exact application must be approved before continuing.")
            self._validate_payload(payload)
            connector = self.connectors.get(current["job"].source)
            if not connector or not connector.capabilities.automatic_submission:
                message = ("Manual handoff only: automatic submission is NOT IMPLEMENTED for this source. "
                           "Open the employer page, attach the approved "
                           "résumé, copy the reviewed content and answer mandatory questions. Login/CAPTCHA must be "
                           "completed by you. Nothing has been submitted by this app.")
                self._state(db, application_id, "manual_handoff", message)
                return "manual_handoff", message
            attempt_key = str(uuid.uuid4())
            db.execute("UPDATE applications SET attempt_key=? WHERE id=?", (attempt_key, application_id))
            self._state(db, application_id, "submitting", "Submission attempt durably recorded before network access.")
        # No built-in connector implements submission. Future authorized adapters must honor this contract.
        try:
            result = connector.submit(json.loads(canonical_json(payload)), attempt_key)
            if result.outcome not in {"accepted", "rejected", "unknown"}:
                raise ValueError("Submission integration returned an invalid outcome.")
            if result.outcome == "accepted" and not result.receipt:
                raise ValueError("Submission integration did not return a confirmation receipt.")
            state = {"accepted": "submitted", "rejected": "rejected", "unknown": "unknown"}[result.outcome]
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
