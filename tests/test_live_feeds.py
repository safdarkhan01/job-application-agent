"""Opt-in public GET requests only. Never accesses a profile or submits an application."""

import os
from pathlib import Path
import tempfile
import unittest

from job_agent.connectors import Arbeitnow, Ashby, Greenhouse, Jobicy, Lever, Remotive
from job_agent.market import MarketQuery, research
from job_agent.storage import Store
from job_agent.tracking import Tracker


@unittest.skipUnless(os.environ.get("JOB_AGENT_LIVE_FEEDS") == "1", "Set JOB_AGENT_LIVE_FEEDS=1 for public-feed smoke checks")
class LiveFeedTests(unittest.TestCase):
    def test_public_sources_return_real_normalized_postings(self):
        for connector in (Remotive(), Arbeitnow()):
            with self.subTest(source=connector.id):
                result = connector.discover()
                self.assertGreater(len(result.jobs), 0)
                self.assertFalse(connector.capabilities.automatic_submission)
                self.assertTrue(all(job.url.startswith("https://") for job in result.jobs))
                print(f"\n{connector.label}: {len(result.jobs)} real jobs normalized; {result.rejected} malformed rejected.")

    def test_additional_public_sources_and_documented_employer_boards(self):
        for connector in (Jobicy(), Greenhouse("stripe"), Lever("leverdemo"), Ashby("Ashby")):
            with self.subTest(source=connector.id):
                result = connector.discover()
                self.assertGreater(len(result.jobs), 0)
                self.assertFalse(connector.capabilities.automatic_submission)
                print(f"\n{connector.label}: {len(result.jobs)} real jobs normalized; {result.rejected} malformed rejected.")

    def test_generic_salary_evidence_and_selected_listing_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            report = research(store, MarketQuery("Software Engineer", "United States", currency="USD"))
            self.assertEqual(3, len(report.source_results))
            self.assertTrue(all(sample["url"].startswith("https://") for sample in report.samples))
            self.assertTrue(report.researched_at)
            self.assertTrue(all(sample["retrieved_at"] != "unknown" for sample in report.samples))
            print("\nActual salary evidence:", report.summary)
            for result in report.source_results:
                print(result)
            cache = store.cache("jobicy")
            self.assertIsNotNone(cache)
            self.assertGreater(len(cache[1]), 0)
            listing = Tracker(store).refresh_availability(cache[1][0])
            self.assertIn(listing["status"], {"open", "closed", "unknown"})
            self.assertNotEqual("active", listing["status"])
            print("Selected Jobicy listing availability:", listing["status"], listing["checked_at"])
