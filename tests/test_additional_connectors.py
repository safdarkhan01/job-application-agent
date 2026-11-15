import unittest
from unittest.mock import patch

from job_agent.connectors import Ashby, FeedError, Jobicy, Lever, connectors


class AdditionalConnectorTests(unittest.TestCase):
    def test_lever_global_and_eu_keep_structured_salary_and_hybrid_is_not_remote(self):
        fixture = [{
            "id": "synthetic-lever", "text": "Software Engineer",
            "categories": {"location": "London", "allLocations": ["London", "Manchester"]},
            "descriptionPlain": "Build Python services.",
            "lists": [{"text": "Required", "content": "<p>SQL</p>"}],
            "additionalPlain": "Synthetic fixture only.",
            "hostedUrl": "https://jobs.lever.co/synthetic/fixture",
            "workplaceType": "hybrid",
            "salaryRange": {"min": 80000, "max": 100000, "currency": "GBP", "interval": "per-year"},
        }]
        for eu in (False, True):
            with self.subTest(eu=eu), patch("job_agent.connectors.fetch_json", return_value=fixture) as fetch:
                posting = Lever("synthetic", eu=eu).discover().jobs[0]
                self.assertIn("api.eu.lever.co" if eu else "api.lever.co", fetch.call_args[0][0])
                self.assertEqual("London; Manchester", posting.location)
                self.assertFalse(posting.remote)
                self.assertEqual(80000, posting.salary.minimum)
                self.assertEqual("GBP", posting.salary.currency)
                self.assertIn("SQL", posting.skills)

    def test_ashby_excludes_unlisted_and_does_not_infer_fully_remote_from_isremote(self):
        visible = {
            "title": "Data Engineer", "location": "New York",
            "secondaryLocations": [{"location": "Boston"}],
            "isListed": True, "isRemote": True, "workplaceType": "Hybrid",
            "descriptionPlain": "Python SQL", "jobUrl": "https://jobs.ashbyhq.com/synthetic/fixture",
            "compensation": {"summaryComponents": [
                {"compensationType": "EquityPercentage", "interval": "NONE", "minValue": 1, "maxValue": 2},
                {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
                 "minValue": 100000, "maxValue": 120000},
            ]},
        }
        hidden = {**visible, "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/synthetic/unlisted"}
        with patch("job_agent.connectors.fetch_json", return_value={"jobs": [visible, hidden]}):
            result = Ashby("synthetic").discover()
        self.assertEqual(1, len(result.jobs))
        posting = result.jobs[0]
        self.assertFalse(posting.remote)
        self.assertEqual("New York; Boston", posting.location)
        self.assertEqual(100000, posting.salary.minimum)
        self.assertEqual("year", posting.salary.period)
        del visible["workplaceType"]
        with patch("job_agent.connectors.fetch_json", return_value={"jobs": [visible]}):
            self.assertIsNone(Ashby("synthetic").discover().jobs[0].remote)

    def test_jobicy_retains_attribution_link_and_structured_pay_without_credentials(self):
        fixture = {"success": True, "jobs": [{
            "id": 123, "url": "https://jobicy.com/jobs/synthetic-123", "jobTitle": "Software Engineer",
            "companyName": "Synthetic Company", "jobGeo": "Anywhere", "jobDescription": "<p>Python &amp; SQL</p>",
            "salaryMin": 100000, "salaryMax": 120000, "salaryCurrency": "USD", "salaryPeriod": "yearly",
        }]}
        with patch("job_agent.connectors.fetch_json", return_value=fixture) as fetch:
            posting = Jobicy().discover().jobs[0]
        self.assertEqual("https://jobicy.com/api/jobs?count=100", fetch.call_args[0][0])
        self.assertEqual("https://jobicy.com/jobs/synthetic-123", posting.url)
        self.assertEqual("Jobicy structured salary", posting.salary.evidence)
        self.assertTrue(posting.remote)
        self.assertEqual(100000, posting.salary.minimum)

    def test_malformed_feeds_and_rows_are_explicit(self):
        for source in (Lever("synthetic"), Ashby("synthetic"), Jobicy()):
            with self.subTest(source=source.id), patch("job_agent.connectors.fetch_json", return_value=None):
                with self.assertRaises(FeedError):
                    source.discover()
        with patch("job_agent.connectors.fetch_json", return_value=[{"text": "Broken posting"}]):
            self.assertEqual(1, Lever("synthetic").discover().rejected)
        with patch("job_agent.connectors.fetch_json", return_value={"success": False, "jobs": []}):
            with self.assertRaises(FeedError):
                Jobicy().discover()

    def test_employer_configuration_is_bounded_and_validated(self):
        sources = connectors("greenhouse-one, greenhouse-two", lever="lever-one", ashby="ashby-one", lever_eu="lever-eu")
        self.assertEqual(8, len(sources))
        self.assertIn("jobicy", sources)
        self.assertIn("lever-eu:lever-eu", sources)
        for config in ({"lever": "https://example.invalid"}, {"ashby": "../bad"},
                       {"board": ",".join("board" + str(index) for index in range(11))}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                connectors(**config)
        self.assertTrue(all(not source.capabilities.automatic_submission for source in sources.values()))
