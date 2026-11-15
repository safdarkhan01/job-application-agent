from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from job_agent.connectors import FeedError, Jobicy, utc_now
from job_agent.models import digest
from job_agent.service import Applications
from job_agent.tracking import Tracker, hiring_badge
from tests.test_agent import StorageCase, job


class TrackingTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.tracker = Tracker(self.store)

    def test_external_import_deduplicates_an_existing_draft_and_prevents_reapply(self):
        posting = job()
        application_id = self.prepare(posting)
        self.approve(application_id)
        linked, created = self.apps.import_external(
            posting, portal="LinkedIn", applied_at="2026-01-02", stage="interview",
            evidence="Synthetic user-reported interview; no real account accessed.")
        self.assertEqual(application_id, linked)
        self.assertFalse(created)
        saved = self.apps.get(application_id)
        self.assertEqual("submitted", saved["state"])
        self.assertIsNone(saved["approved_digest"])
        self.assertEqual("user-confirmed-external", saved["receipt"])
        tracking = self.tracker.summary(saved)
        self.assertEqual("interview", tracking["stage"])
        self.assertEqual("LinkedIn", tracking["portal"])
        self.assertEqual("2026-01-02", tracking["applied_at"])
        self.assertEqual("user-reported", tracking["provenance"])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.assertEqual((application_id, False), self.apps.prepare(posting, self.profile))

    def test_new_external_application_needs_no_resume_and_is_not_resubmittable(self):
        application_id, created = self.apps.import_external(
            job(), portal="Indeed", applied_at="2026-01-01", stage="submitted")
        self.assertTrue(created)
        self.assertEqual({}, self.apps.get(application_id)["payload"]["resume"])
        with self.assertRaises(ValueError):
            self.approve(application_id)
        reopened = Applications(self.store)
        self.assertEqual("Indeed", self.tracker.summary(reopened.get(application_id))["portal"])
        self.tracker.update_stage(application_id, "screening", evidence="Synthetic update")
        self.assertEqual("2026-01-01", self.tracker.summary(reopened.get(application_id))["applied_at"])

    def test_stage_and_listing_availability_remain_separate_from_send_state(self):
        application_id = self.prepare()
        posting = self.apps.get(application_id)["job"]
        self.tracker.update_stage(application_id, "rejected", evidence="User reports a rejection email.")
        self.assertEqual("prepared", self.apps.get(application_id)["state"])
        self.assertEqual("unknown", self.tracker.availability(posting)["status"])
        self.tracker.set_availability(posting, "closed", evidence="User reports explicit closure on the employer page.")
        self.assertEqual("rejected", self.tracker.summary(self.apps.get(application_id))["stage"])
        with self.assertRaisesRegex(ValueError, "closed"):
            self.approve(application_id)
        self.assertGreaterEqual(len(self.tracker.history(application_id)), 2)

    def test_open_recent_listing_is_not_an_active_hiring_claim(self):
        posting = job(hiring_status="open", hiring_checked_at=utc_now(),
                      hiring_evidence="Listed in the public feed", published_at=utc_now())
        self.assertEqual("Open", hiring_badge(self.tracker.availability(posting)))
        posting.hiring_status = "active"
        posting.hiring_evidence = "Explicit synthetic employer actively-hiring claim."
        self.assertEqual("Active claim", hiring_badge(self.tracker.availability(posting)))
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        posting.hiring_checked_at = old
        self.assertEqual("Unverified", hiring_badge(self.tracker.availability(posting)))

    def test_jobicy_explicit_status_sync_does_not_change_application_stage(self):
        posting = job(source="jobicy", source_id="123")
        application_id = self.prepare(posting)
        payload = {"success": True, "checkedAt": utc_now(), "jobs": [{"id": 123, "status": "active"}]}
        with patch("job_agent.connectors.fetch_json", return_value=payload) as fetch:
            checked = self.tracker.refresh_availability(posting)
            again = self.tracker.refresh_availability(posting)
        self.assertEqual(1, fetch.call_count)
        self.assertEqual("open", checked["status"])
        self.assertEqual(checked, again)
        self.assertEqual("prepared", self.tracker.summary(self.apps.get(application_id))["stage"])
        self.assertEqual("prepared", self.apps.get(application_id)["state"])

    def test_failed_check_is_unknown_not_rejection_or_closed(self):
        posting = job(source="jobicy", source_id="123")
        application_id = self.prepare(posting)
        with patch("job_agent.connectors.fetch_json", side_effect=FeedError("Synthetic blocked")):
            with self.assertRaises(FeedError):
                self.tracker.refresh_availability(posting)
        self.assertEqual("unknown", self.tracker.availability(posting)["status"])
        self.assertEqual("prepared", self.tracker.summary(self.apps.get(application_id))["stage"])
        self.assertEqual("prepared", self.apps.get(application_id)["state"])

    def test_discard_invalidates_approval_but_cannot_discard_a_sent_application(self):
        application_id = self.prepare()
        self.approve(application_id)
        self.apps.discard(application_id)
        self.assertEqual("discarded", self.apps.get(application_id)["state"])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.apps.import_external(job(), portal="LinkedIn", applied_at="2026-01-01", stage="submitted")
        with self.assertRaises(ValueError):
            self.apps.discard(application_id)

    def test_unknown_technical_outcome_is_not_erased_by_manual_stage_import(self):
        application_id = self.prepare()
        with self.store.connect() as db:
            db.execute("UPDATE applications SET state='unknown' WHERE id=?", (application_id,))
        self.apps.import_external(job(), portal="Synthetic portal", applied_at="2026-01-01", stage="screening")
        self.assertEqual("unknown", self.apps.get(application_id)["state"])
        self.assertEqual("screening", self.tracker.summary(self.apps.get(application_id))["stage"])
        with self.assertRaises(ValueError):
            self.approve(application_id)

    def test_stage_progress_after_approval_blocks_duplicate_send(self):
        application_id = self.prepare()
        self.approve(application_id)
        self.tracker.update_stage(application_id, "interview", evidence="User reports a prior application interview.")
        self.assertIsNone(self.apps.get(application_id)["approved_digest"])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
