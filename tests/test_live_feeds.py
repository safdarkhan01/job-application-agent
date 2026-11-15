"""Opt-in public GET requests only. Never accesses a profile or submits an application."""

import os
import unittest

from job_agent.connectors import Arbeitnow, Ashby, Greenhouse, Jobicy, Lever, Remotive


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
