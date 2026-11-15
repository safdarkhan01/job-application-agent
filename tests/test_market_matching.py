from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from job_agent.connectors import Discovery, utc_now
from job_agent.market import MarketQuery, build_report, research, saved_report
from job_agent.matching import rank
from job_agent.models import Profile, Salary
from tests.test_agent import StorageCase, job


class RankingRevisionTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.profile.minimum_salary = 100000

    def test_skills_and_pay_outweigh_title_and_promotion(self):
        relevant = job(title="Platform Specialist", salary=Salary(120000, 140000, "USD"))
        poor_fit = job(title="Senior Software Engineer", description="Java React Kubernetes AWS C++",
                       salary=Salary(300000, 350000, "USD"))
        matched, poor = rank(relevant, self.profile), rank(poor_fit, self.profile)
        self.assertEqual("mismatch", matched.role)
        self.assertTrue(matched.eligible)
        self.assertFalse(poor.eligible)
        self.assertGreater(matched.order, poor.order)

    def test_unknown_or_overlapping_offered_pay_is_not_fixed_by_market_estimates(self):
        for pay in (Salary(), Salary(80000, 150000, "USD")):
            self.assertFalse(rank(job(salary=pay), self.profile).eligible)

    def test_same_and_higher_levels_are_soft_preferences_not_invented_eligibility(self):
        profile = replace(self.profile, current_level="senior", include_higher=True)
        for title in ("Senior Software Engineer", "Staff Software Engineer", "Principal Software Engineer"):
            with self.subTest(title=title):
                match = rank(job(title=title, salary=Salary(120000, 150000, "USD")), profile)
                self.assertEqual("preferred", match.level)
                self.assertTrue(match.eligible)
        unknown = rank(job(salary=Salary(120000, 150000, "USD")), profile)
        self.assertEqual("unknown", unknown.level)

    def test_freshness_uses_publication_not_retrieval_and_is_secondary(self):
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        unknown = rank(job(fetched_at=now.isoformat()), self.profile, now=now)
        self.assertIsNone(unknown.age_days)
        known = rank(job(published_at="2026-11-14T12:00:00Z", fetched_at="2026-11-16T00:00:00Z"),
                     self.profile, now=now)
        self.assertEqual(2, known.age_days)
        self.assertTrue(known.recent)
        future = rank(job(published_at="2026-12-01T00:00:00Z"), self.profile, now=now)
        self.assertIsNone(future.age_days)
        closed = rank(job(hiring_status="closed", salary=Salary(120000, 150000, "USD")), self.profile)
        self.assertFalse(closed.eligible)


class MarketEvidenceTests(StorageCase):
    def samples(self):
        return [
            job(source="remotive", source_id="1", company="Synthetic One", url="https://example.invalid/one",
                salary=Salary(100000, 130000, "USD"), published_at="2026-11-10", fetched_at=utc_now()),
            job(source="jobicy", source_id="2", company="Synthetic Two", url="https://example.invalid/two",
                salary=Salary(110000, 140000, "USD"), published_at="2026-11-11", fetched_at=utc_now()),
            job(source="jobicy", source_id="3", company="Synthetic Three", url="https://example.invalid/three",
                salary=Salary(120000, 150000, "USD"), published_at="", fetched_at=utc_now()),
        ]

    def test_report_contains_actual_multi_site_posting_evidence_and_caveats(self):
        query = MarketQuery("Software Engineer", "USA", currency="USD")
        report = build_report(self.store, query, self.samples(), ["Synthetic sources"])
        self.assertTrue(report.sufficient)
        self.assertEqual(2, report.source_count)
        self.assertIn("110,000-140,000", report.summary)
        self.assertIn("Low confidence", report.summary)
        self.assertEqual("unknown", report.samples[-1]["published_at"])
        self.assertEqual("unspecified", report.samples[0]["basis"])
        self.assertEqual("Software Engineer", saved_report(self.store, query).query["role"])
        self.assertTrue(any("never" in note for note in report.caveats))

    def test_research_retrieves_public_evidence_without_sending_private_profile_data(self):
        sample = self.samples()
        from job_agent.service import SearchResult
        with patch("job_agent.market.discover", return_value=SearchResult(sample, ["Synthetic feed results"])) as fetch:
            report = research(self.store, MarketQuery("Software Engineer", "United States"))
        self.assertEqual({"remotive", "arbeitnow", "jobicy"}, set(fetch.call_args.args[1]))
        self.assertEqual(3, len(report.samples))
        self.assertNotIn("candidate@example.invalid", str(fetch.call_args))

    def test_location_currency_level_and_duplicate_caveats_do_not_fabricate_samples(self):
        query = MarketQuery("Software Engineer", "USA", "senior", "USD")
        samples = self.samples()
        samples += [
            replace(samples[0], source="another-feed"),
            job(source_id="4", company="Wrong country", location="Canada",
                url="https://example.invalid/canada", salary=Salary(130000, 150000, "USD")),
            job(source_id="5", company="Wrong currency", url="https://example.invalid/currency",
                salary=Salary(130000, 150000, "CAD")),
            job(source_id="6", company="Wrong level", title="Junior Software Engineer",
                url="https://example.invalid/level", salary=Salary(130000, 150000, "USD")),
        ]
        report = build_report(self.store, query, samples, [])
        self.assertEqual(3, len(report.samples))
        self.assertTrue(all(sample["level"] == "unspecified" for sample in report.samples))
        city = build_report(self.store, MarketQuery("Software Engineer", "Boston"), samples, [])
        self.assertFalse(city.sufficient)
        self.assertEqual([], city.samples)
        self.assertIn("Insufficient", city.summary)

    def test_base_total_and_unspecified_pay_are_not_mixed_to_invent_a_benchmark(self):
        samples = self.samples()
        samples[0].salary.basis = "base"
        samples[1].salary.basis = "total"
        samples[2].salary.basis = "unspecified"
        report = build_report(self.store, MarketQuery("Software Engineer", "USA"), samples, [])
        self.assertFalse(report.sufficient)
        self.assertIn("Insufficient", report.summary)
