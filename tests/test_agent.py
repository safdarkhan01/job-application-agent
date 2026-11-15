from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from job_agent.connectors import (
    Arbeitnow, Capabilities, Discovery, FeedError, Greenhouse, Remotive, SubmissionResult, connectors, parse_salary,
)
from job_agent.matching import rank
from job_agent.models import Job, Profile, Salary, canonical_url, deduplicate, digest, public_url
from job_agent.resume import read_resume
from job_agent.service import Applications, discover
from job_agent.storage import Store


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_resume.txt"


def job(**changes):
    values = dict(source="manual", source_id="fixture-1", title="Software Engineer",
                  company="Synthetic Company", location="United States", url="https://example.invalid/jobs/1",
                  remote=True, description="Python and SQL development.", salary=Salary())
    values.update(changes)
    return Job(**values)


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name))
        _, attachment = self.store.import_resume(FIXTURE)
        self.profile = Profile(name="Synthetic Candidate", email="candidate@example.invalid",
                               titles=["Software Engineer"], skills=["Python", "SQL"],
                               locations=["USA"], remote="remote", resume=attachment)
        self.apps = Applications(self.store)

    def prepare(self, posting=None):
        return self.apps.prepare(posting or job(), self.profile)[0]

    def approve(self, application_id):
        self.apps.approve(application_id, digest(self.apps.get(application_id)["payload"]))

    def edit(self, application_id, **changes):
        payload = self.apps.get(application_id)["payload"]
        values = dict(name=payload["candidate"]["name"], email=payload["candidate"]["email"],
                      cover_letter=payload["cover_letter"], answers=payload["answers"])
        values.update(changes)
        return self.apps.edit(application_id, **values)


class ResumeTests(StorageCase):
    def test_text_suggestions_have_evidence_without_guessed_name(self):
        parsed = read_resume(FIXTURE)
        self.assertIn("Python", parsed.skills)
        self.assertIn("Software Engineer", parsed.titles)
        self.assertIn("2021", parsed.experience)
        self.assertNotIn("AWS", parsed.skills)
        self.assertFalse(hasattr(parsed, "name"))

    def test_docx_import(self):
        path = Path(self.temporary.name) / "fixture.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml",
                             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             "<w:body><w:p><w:r><w:t>Synthetic Data Analyst with Python and SQL experience."
                             "</w:t></w:r></w:p></w:body></w:document>")
        parsed = read_resume(path)
        self.assertIn("Data Analyst", parsed.titles)
        self.assertEqual(["Python", "SQL"], parsed.skills)

    def test_unsupported_and_empty_formats_are_explicit(self):
        for suffix, data, expected in ((".doc", b"legacy", "Unsupported"),
                                        (".txt", b"\x00binary", "binary"),
                                        (".txt", b" ", "No usable"),
                                        (".docx", b"not a zip", "could not be read")):
            path = Path(self.temporary.name) / ("bad" + suffix)
            path.write_bytes(data)
            with self.subTest(suffix=suffix, data=data), self.assertRaisesRegex(ValueError, expected):
                read_resume(path)

    def test_asset_is_copied_and_hash_verified(self):
        source = Path(self.temporary.name) / "source.txt"
        source.write_text(FIXTURE.read_text(), encoding="utf-8")
        _, attachment = self.store.import_resume(source)
        source.write_text("changed source", encoding="utf-8")
        self.store.verify_resume(attachment)
        Path(attachment["path"]).write_text("tampered local attachment", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.verify_resume(attachment)

    def test_profile_and_applications_survive_reopen(self):
        self.store.save_profile(self.profile)
        application_id = self.prepare()
        self.approve(application_id)
        reopened = Store(Path(self.temporary.name))
        self.assertEqual("Synthetic Candidate", reopened.profile().name)
        self.assertEqual("approved", Applications(reopened).get(application_id)["state"])

    def test_second_desktop_instance_is_rejected(self):
        handle = self.store.acquire_instance_lock()
        try:
            with self.assertRaisesRegex(ValueError, "already"):
                self.store.acquire_instance_lock()
        finally:
            handle.close()
        self.store.acquire_instance_lock().close()


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(titles=["Software Engineer"], skills=["Python", "SQL"],
                               locations=["USA"], remote="remote", minimum_salary=100000,
                               currency="USD", period="year")

    def test_role_skills_location_and_known_salary_rank_at_100(self):
        result = rank(job(salary=Salary(110000, 130000, "USD", "year", "USD 110k-130k/year")), self.profile)
        self.assertEqual(100, result.score)
        self.assertTrue(result.eligible)
        self.assertEqual("meets", result.compensation)
        self.assertTrue(any("Python, SQL" in reason for reason in result.reasons))

    def test_unknown_salary_is_not_a_meeting_salary(self):
        result = rank(job(), self.profile)
        self.assertEqual("unknown", result.compensation)
        self.assertEqual(70, result.score)
        self.assertFalse(result.eligible)

    def test_salary_ranges_and_currency(self):
        for salary, expected in (
            (Salary(80000, 120000, "USD"), "possible"),
            (Salary(None, 120000, "USD"), "possible"),
            (Salary(70000, 90000, "USD"), "below"),
            (Salary(120000, 130000, "CAD"), "unknown"),
            (Salary(10000, 11000, "USD", "month"), "meets"),
            (Salary(50, 60, "USD", "hour"), "meets"),
        ):
            with self.subTest(salary=salary):
                self.assertEqual(expected, rank(job(salary=salary), self.profile).compensation)

    def test_unknown_and_mismatched_remote_eligibility(self):
        self.assertEqual("unknown", rank(job(location="Remote"), self.profile).location)
        self.assertEqual("mismatch", rank(job(location="Australia"), self.profile).location)
        self.assertEqual("mismatch", rank(job(remote=False), self.profile).location)
        self.assertEqual("unknown", rank(job(remote=None), self.profile).location)
        self.assertEqual("match", rank(job(location="Worldwide"), self.profile).location)

    def test_title_not_description_drives_role_and_word_boundaries(self):
        result = rank(job(title="Sales Specialist", description="Work with Software Engineer colleagues"), self.profile)
        self.assertEqual("mismatch", result.role)
        profile = replace(self.profile, skills=["Java", "C"])
        result = rank(job(description="JavaScript development"), profile)
        self.assertEqual([], result.matched_skills)
        self.assertFalse(result.eligible)

    def test_invalid_pay_is_rejected(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Profile(minimum_salary=value).validate()

    def test_salary_parser_is_conservative(self):
        self.assertEqual(100000, parse_salary("USD 100k - 130k per year").minimum)
        self.assertEqual(None, parse_salary("$100k - $130k").minimum)
        self.assertEqual(None, parse_salary("competitive").minimum)
        self.assertEqual(None, parse_salary("up to USD 130k per year").minimum)
        self.assertEqual(130000, parse_salary("up to USD 130k per year").maximum)
        self.assertIsNone(parse_salary("USD 100k or EUR 110k annually").minimum)
        self.assertIsNone(parse_salary("USD 100k per year / hour").minimum)


class ApplicationTests(StorageCase):
    def test_no_dispatch_without_approval(self):
        application_id = self.prepare()
        with self.assertRaisesRegex(ValueError, "approved"):
            self.apps.dispatch(application_id)
        self.assertEqual("prepared", self.apps.get(application_id)["state"])

    def test_every_payload_edit_invalidates_approval_but_noop_does_not(self):
        application_id = self.prepare()
        self.approve(application_id)
        self.assertFalse(self.edit(application_id))
        self.assertEqual("approved", self.apps.get(application_id)["state"])
        for change in ({"cover_letter": "Synthetic truthful letter"}, {"answers": {"Question": "Answer"}},
                       {"email": "changed@example.invalid"}, {"name": "Changed synthetic name"}):
            self.approve(application_id)
            self.assertTrue(self.edit(application_id, **change))
            self.assertEqual("prepared", self.apps.get(application_id)["state"])
            self.assertIsNone(self.apps.get(application_id)["approved_digest"])

    def test_stale_review_cannot_approve_a_different_revision(self):
        application_id = self.prepare()
        reviewed = digest(self.apps.get(application_id)["payload"])
        self.edit(application_id, cover_letter="New revision")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            self.apps.approve(application_id, reviewed)

    def test_profile_refresh_invalidates_approval(self):
        application_id = self.prepare()
        self.approve(application_id)
        self.profile.skills.append("Docker")
        self.edit(application_id, profile=self.profile)
        self.assertEqual("prepared", self.apps.get(application_id)["state"])

    def test_missing_identity_or_tampered_resume_blocks_approval_and_dispatch(self):
        application_id = self.prepare()
        self.edit(application_id, email="")
        with self.assertRaisesRegex(ValueError, "email"):
            self.approve(application_id)
        self.edit(application_id, email=self.profile.email)
        self.approve(application_id)
        Path(self.profile.resume["path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.apps.dispatch(application_id)

    def test_duplicate_preparation_by_business_identity_and_url(self):
        first = self.prepare()
        second, created = self.apps.prepare(job(source="remotive", source_id="remote-123",
                                               url="https://example.invalid/another-source"), self.profile)
        self.assertFalse(created)
        self.assertEqual(first, second)
        second, created = self.apps.prepare(job(source="remotive", source_id="remote-123", title="Renamed syndicated title",
                                               url="https://example.invalid/another-source"), self.profile)
        self.assertFalse(created)
        self.assertEqual(first, second)
        second, created = self.apps.prepare(job(title="Renamed title", url="https://example.invalid/jobs/1?utm_source=x"),
                                           self.profile)
        self.assertFalse(created)
        self.assertEqual(first, second)

    def test_manual_linkedin_workflow_does_not_access_profile_or_submit(self):
        posting = job(url="https://www.linkedin.com/jobs/view/0000000000/",
                      source_id="synthetic-linkedin", description="Synthetic posting, never fetched.")
        application_id = self.prepare(posting)
        self.approve(application_id)
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network allowed")):
            state, message = self.apps.dispatch(application_id)
        self.assertEqual("manual_handoff", state)
        self.assertIn("Nothing has been submitted", message)
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.apps.confirm_manual(application_id)
        saved = self.apps.get(application_id)
        self.assertEqual("submitted", saved["state"])
        self.assertEqual("user-confirmed-manual", saved["receipt"])
        with self.assertRaises(ValueError):
            self.edit(application_id, cover_letter="Too late")

    def test_approval_on_every_production_connector_only_creates_a_manual_handoff(self):
        sources = connectors("synthetic-gh", lever="synthetic-lever", ashby="synthetic-ashby",
                             lever_eu="synthetic-eu")
        self.apps = Applications(self.store, sources)
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network allowed")) as network:
            for index, source_id in enumerate(sources):
                with self.subTest(source=source_id):
                    application_id = self.prepare(job(
                        source=source_id, company=f"Synthetic Company {index}",
                        source_id=f"synthetic-posting-{index}", url=f"https://example.invalid/jobs/{index}"))
                    self.approve(application_id)
                    self.assertEqual("approved", self.apps.get(application_id)["state"])
                    self.assertEqual("manual_handoff", self.apps.dispatch(application_id)[0])
                    self.assertEqual("", self.apps.get(application_id)["receipt"])
                    with self.assertRaisesRegex(ValueError, "approved"):
                        self.apps.dispatch(application_id)
            network.assert_not_called()

    def test_answers_require_an_object_of_text(self):
        application_id = self.prepare()
        for answers in ([], {"Question": True}, {"Question": {"nested": "answer"}}):
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, "Answers"):
                self.edit(application_id, answers=answers)


class FakeAuthorizedConnector:
    id = "synthetic"
    label = "Offline synthetic adapter (not shipped in the app)"
    cache_seconds = 3600
    capabilities = Capabilities("Synthetic tests only", "Synthetic tests only", True)

    def __init__(self):
        self.calls = []
        self.result = SubmissionResult("accepted", "Synthetic acceptance", "synthetic-receipt")
        self.error = None
        self.on_submit = None

    def missing_answers(self, payload):
        return [] if payload["answers"].get("mandatory") else ["mandatory"]

    def submit(self, payload, idempotency_key):
        self.calls.append((payload, idempotency_key))
        if self.on_submit:
            self.on_submit(payload, idempotency_key)
        if self.error:
            raise self.error
        return self.result


class SubmissionContractTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.connector = FakeAuthorizedConnector()
        self.apps = Applications(self.store, {"synthetic": self.connector})

    def prepared(self):
        application_id = self.prepare(job(source="synthetic"))
        self.edit(application_id, answers={"mandatory": "Synthetic answer"})
        return application_id

    def test_missing_mandatory_answers_block_approval(self):
        application_id = self.prepare(job(source="synthetic"))
        with self.assertRaisesRegex(ValueError, "mandatory"):
            self.approve(application_id)
        self.assertFalse(self.connector.calls)

    def test_authorized_adapter_gets_only_the_approved_payload_once(self):
        application_id = self.prepared()
        self.approve(application_id)
        payload = self.apps.get(application_id)["payload"]
        state, _ = self.apps.dispatch(application_id)
        self.assertEqual("submitted", state)
        self.assertEqual(payload, self.connector.calls[0][0])
        self.assertTrue(self.connector.calls[0][1])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.assertEqual(1, len(self.connector.calls))

    def test_inflight_attempt_is_durable_and_a_concurrent_dispatch_is_blocked(self):
        application_id = self.prepared()
        self.approve(application_id)
        started, release = threading.Event(), threading.Event()

        def in_flight(payload, key):
            saved = self.apps.get(application_id)
            self.assertEqual("submitting", saved["state"])
            self.assertEqual(key, saved["attempt_key"])
            started.set()
            if not release.wait(5):
                raise TimeoutError("Synthetic test did not release its adapter")

        self.connector.on_submit = in_flight
        with ThreadPoolExecutor(max_workers=1) as executor:
            attempt = executor.submit(self.apps.dispatch, application_id)
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaisesRegex(ValueError, "approved"):
                    self.apps.dispatch(application_id)
            finally:
                release.set()
            self.assertEqual("submitted", attempt.result()[0])
        self.assertEqual(1, len(self.connector.calls))

    def test_timeout_is_durable_unknown_never_automatically_retried(self):
        application_id = self.prepared()
        self.approve(application_id)
        self.connector.error = TimeoutError("Synthetic timeout")
        state, _ = self.apps.dispatch(application_id)
        self.assertEqual("unknown", state)
        with self.assertRaises(ValueError):
            self.approve(application_id)
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.assertEqual("unknown", Applications(self.store).get(application_id)["state"])
        self.assertEqual(1, len(self.connector.calls))
        with self.assertRaises(ValueError):
            self.apps.reconcile(application_id, submitted=False, evidence="")
        self.apps.reconcile(application_id, submitted=False, evidence="Synthetic employer confirms not received")
        self.assertEqual("rejected", self.apps.get(application_id)["state"])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)

    def test_acceptance_without_receipt_is_unknown(self):
        application_id = self.prepared()
        self.approve(application_id)
        self.connector.result = SubmissionResult("accepted", "No receipt")
        self.assertEqual("unknown", self.apps.dispatch(application_id)[0])

    def test_crash_recovery_marks_inflight_as_unknown(self):
        application_id = self.prepared()
        with self.store.connect() as db:
            db.execute("UPDATE applications SET state='submitting',attempt_key='synthetic-key' WHERE id=?",
                       (application_id,))
        self.assertEqual("submitting", Applications(self.store).get(application_id)["state"])
        recovered = Applications(self.store, recover_interrupted=True)
        self.assertEqual("unknown", recovered.get(application_id)["state"])
        self.assertIn("restarted", recovered.events(application_id)[-1]["message"])


class ConnectorTests(StorageCase):
    def test_tracking_url_and_cross_source_deduplication(self):
        self.assertEqual(canonical_url("https://example.invalid/jobs/1"),
                         canonical_url("https://example.invalid/jobs/1/?utm_source=test#tracking"))
        merged = deduplicate([job(), job(source="remotive", source_id="2", url="https://example.invalid/jobs/2")])
        self.assertEqual(1, len(merged))
        self.assertEqual(["manual", "remotive"], merged[0].sources)
        self.assertEqual(2, len(deduplicate([job(), job(source_id="different-posting", location="Canada",
                                                      url="https://example.invalid/jobs/2")])))

    def test_bridging_duplicates_merge_all_aliases(self):
        merged = deduplicate([
            job(),
            job(source="remotive", source_id="2", company="Alternate name", url="https://example.invalid/jobs/2"),
            job(source="arbeitnow", source_id="3", url="https://example.invalid/jobs/2"),
        ])
        self.assertEqual(1, len(merged))
        self.assertIn("source:remotive:2", merged[0].keys)
        self.assertEqual(["arbeitnow", "manual", "remotive"], merged[0].sources)

    def test_private_or_unsafe_posting_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "http://example.invalid", "https://127.0.0.1/jobs",
                    "https://localhost/jobs", "https://name:password@example.invalid/jobs", "https://10.0.0.1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)

    def test_remotive_normalization_rejects_bad_items_and_preserves_source(self):
        payload = {"jobs": [
            {"id": 1, "title": "Software Engineer", "company_name": "Synthetic Company",
             "candidate_required_location": "Worldwide", "url": "https://remotive.com/remote-jobs/software-dev/synthetic-1",
             "description": "<p>Python &amp; SQL</p><script>hidden</script>", "salary": "USD 100k-130k per year"},
            {"id": 2, "title": "broken"},
        ]}
        with patch("job_agent.connectors.fetch_json", return_value=payload):
            result = Remotive().discover()
        self.assertEqual(1, len(result.jobs))
        self.assertEqual(1, result.rejected)
        self.assertEqual("Python & SQL", result.jobs[0].description)
        self.assertTrue(result.jobs[0].remote)
        self.assertEqual(100000, result.jobs[0].salary.minimum)

    def test_arbeitnow_and_greenhouse_normalize_without_inventing_pay_or_remote(self):
        with patch("job_agent.connectors.fetch_json", return_value={"data": [{
                "slug": "fixture", "title": "Engineer", "company_name": "Synthetic Company",
                "location": "Berlin", "url": "https://example.invalid/fixture", "description": "Python", "remote": False}]}):
            posting = Arbeitnow().discover().jobs[0]
            self.assertIsNone(posting.salary.minimum)
            self.assertFalse(posting.remote)
        with patch("job_agent.connectors.fetch_json", return_value={"jobs": [{
                "id": 1, "title": "Engineer", "location": {"name": "Remote"},
                "absolute_url": "https://example.invalid/fixture", "content": "Python"}]}):
            posting = Greenhouse("synthetic-board").discover().jobs[0]
            self.assertIsNone(posting.remote)
        with self.assertRaises(ValueError):
            Greenhouse("../unsafe")

    def test_malformed_feed_and_network_failures_are_explicit(self):
        with patch("job_agent.connectors.fetch_json", return_value=[]), self.assertRaises(FeedError):
            Remotive().discover()
        source = Remotive()
        with patch.object(source, "discover", side_effect=FeedError("Synthetic offline")) as fetch:
            result = discover(self.store, {source.id: source})
            self.assertEqual([], result.jobs)
            self.assertTrue(any("Synthetic offline" in message for message in result.messages))
            again = discover(self.store, {source.id: source})
            self.assertTrue(any("previous refresh failed" in message for message in again.messages))
            self.assertEqual(1, fetch.call_count)

    def test_feed_cache_is_persistent_and_does_not_send_profile(self):
        source = Remotive()
        with patch.object(source, "discover", return_value=Discovery([job(source="remotive")])) as fetch:
            discover(self.store, {source.id: source})
            result = discover(Store(Path(self.temporary.name)), {source.id: source})
        self.assertEqual(1, fetch.call_count)
        self.assertEqual(1, len(result.jobs))
        self.assertIn("cached", result.messages[0])


if __name__ == "__main__":
    unittest.main()
