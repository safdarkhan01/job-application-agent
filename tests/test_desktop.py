from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from job_agent.desktop import Desktop
from job_agent.models import Salary, digest
from job_agent.storage import Store
from tests.test_agent import FIXTURE, job
from tests.test_email_apply import ACCOUNT, FakeSMTP


class DesktopSmokeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            if "no display name" in str(exc):
                self.skipTest("Desktop smoke test requires a graphical display (use xvfb-run on Linux).")
            raise
        self.root.withdraw()
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.ui = Desktop(self.root, self.store)
        self.error_patch = patch("job_agent.desktop.messagebox.showerror", side_effect=AssertionError)
        self.error_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._close)

    def _close(self):
        self.root.update_idletasks()
        self.ui._shutdown()

    def _wait_for(self, condition):
        deadline = time.monotonic() + 6
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(condition(), "The desktop background operation did not finish.")

    def test_local_profile_discovery_preparation_approval_and_manual_completion(self):
        parsed, attachment = self.store.import_resume(FIXTURE)
        self.ui.profile.resume = attachment
        self.ui.p_name.set("Synthetic Candidate")
        self.ui.p_email.set("candidate@example.invalid")
        self.ui.p_titles.set(", ".join(parsed.titles))
        self.ui.p_skills.set(", ".join(parsed.skills))
        self.ui._save_profile()
        self.store.save_jobs([job(url="https://www.linkedin.com/jobs/view/0000000000/")])
        self.ui._show_jobs()
        self.ui.job_tree.selection_set(next(iter(self.ui.jobs)))
        self.ui._prepare()
        application_id = self.ui.selected_application
        self.assertIsNotNone(application_id)
        self.assertEqual("prepared", self.ui.applications.get(application_id)["state"])
        self.ui._approve()
        dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(1, len(dialogs))
        self.assertEqual("prepared", self.ui.applications.get(application_id)["state"])
        dialogs[0].destroy()
        self.ui.applications.approve(application_id, digest(self.ui.applications.get(application_id)["payload"]))
        self.ui._load_application()
        with patch("job_agent.desktop.messagebox.showinfo"), patch("job_agent.desktop.webbrowser.open", return_value=True) as browser:
            self.ui.handoff_button.invoke()
        self.assertEqual(1, browser.call_count)
        self.assertEqual("manual_handoff", self.ui.applications.get(application_id)["state"])
        with patch("job_agent.desktop.messagebox.askyesno", return_value=True):
            self.ui._confirm_manual()
        self.assertEqual("user-confirmed-manual", self.ui.applications.get(application_id)["receipt"])
        self.assertIn("user reported", self.ui.application_tree.item(application_id, "values")[4])

    def test_unsaved_application_edit_blocks_previously_approved_dispatch(self):
        self.ui.profile.name = "Synthetic Candidate"
        self.ui.profile.email = "candidate@example.invalid"
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        application_id, _ = self.ui.applications.prepare(job(), self.ui.profile)
        self.ui._show_applications()
        self.ui.application_tree.selection_set(application_id)
        self.ui._load_application()
        self.ui.applications.approve(application_id, digest(self.ui.applications.get(application_id)["payload"]))
        self.ui.a_letter.insert("1.0", "Changed after approval")
        with self.assertRaisesRegex(ValueError, "approved"):
            self.ui._dispatch()
        self.assertEqual("prepared", self.ui.applications.get(application_id)["state"])

    def test_catalog_and_multi_platform_board_configuration_render(self):
        self.ui._capabilities()
        self.ui._board_dialog()
        dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(2, len(dialogs))
        self.root.update_idletasks()
        for dialog in dialogs:
            dialog.destroy()

    def test_profile_refresh_uses_all_saved_identity_fields(self):
        self.ui.profile.name = "Original synthetic name"
        self.ui.profile.email = "old@example.invalid"
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        application_id, _ = self.ui.applications.prepare(job(), self.ui.profile)
        self.ui._show_applications()
        self.ui.application_tree.selection_set(application_id)
        self.ui._load_application()
        self.ui.applications.approve(application_id, digest(self.ui.applications.get(application_id)["payload"]))
        self.ui.profile.name = "Updated synthetic name"
        self.ui.profile.email = "updated@example.invalid"
        self.store.save_profile(self.ui.profile)
        self.ui._save_application(refresh_profile=True)
        saved = self.ui.applications.get(application_id)
        self.assertEqual("Updated synthetic name", saved["payload"]["candidate"]["name"])
        self.assertEqual("updated@example.invalid", saved["payload"]["candidate"]["email"])
        self.assertEqual("prepared", saved["state"])

    def test_final_approval_sends_email_in_background_without_freezing_tk(self):
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        self.ui.profile.name, self.ui.profile.email = "Synthetic Candidate", ACCOUNT.sender
        self.ui.mailer.configure(ACCOUNT, "synthetic-session-password")
        application_id, _ = self.ui.applications.prepare(job(), self.ui.profile)
        self.ui.applications.set_delivery(application_id, email=True, account=ACCOUNT,
            recipient="jobs@example.invalid", subject="Synthetic application",
            instructions="Please email your application and resume to jobs@example.invalid.",
            employer_accepts_email=True)
        self.ui._show_applications()
        self.ui.application_tree.selection_set(application_id)
        self.ui._load_application()
        smtp = FakeSMTP()
        started, release, heartbeat = threading.Event(), threading.Event(), threading.Event()

        def wait_for_user_test():
            started.set()
            if not release.wait(5):
                raise TimeoutError("Synthetic test did not release its mail transport.")
        smtp.on_data = wait_for_user_test
        with patch("job_agent.email_apply.smtplib.SMTP_SSL", return_value=smtp):
            dialog = self.ui._approve()
            self.assertEqual("prepared", self.ui.applications.get(application_id)["state"])
            self.ui.approve_send_button.invoke()
            self.assertTrue(started.wait(3))
            self.root.after(0, heartbeat.set)
            try:
                self.root.update()
                self.assertTrue(heartbeat.is_set())
                self.assertIn("send", self.ui.tasks)
                self.assertEqual("disabled", str(self.ui.a_letter["state"]))
                self.assertFalse(dialog.winfo_exists())
            finally:
                release.set()
            self._wait_for(lambda: "send" not in self.ui.tasks)
        saved = self.ui.applications.get(application_id)
        self.assertEqual("email_sent", saved["state"])
        self.assertIn("receipt unverified", self.ui.application_tree.item(application_id, "values")[4])
        self.assertTrue(smtp.message)

    def test_refresh_does_not_replace_unsaved_editor_content(self):
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        application_id, _ = self.ui.applications.prepare(job(), self.ui.profile)
        self.ui._show_applications()
        self.ui.application_tree.selection_set(application_id)
        self.ui._load_application()
        self.ui.a_letter.insert("end", "\nUnsaved synthetic edits")
        draft = self.ui.a_letter.get("1.0", "end-1c")
        self.ui._show_applications()
        self.root.update()
        self.assertEqual(draft, self.ui.a_letter.get("1.0", "end-1c"))

    def test_background_discovery_prepares_only_matches_without_changing_approved_payload(self):
        from job_agent.service import SearchResult
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        self.ui.p_name.set("Synthetic Candidate")
        self.ui.p_email.set(ACCOUNT.sender)
        self.ui.p_skills.set("Python, SQL")
        self.ui.p_titles.set("Software Engineer")
        self.ui.p_minimum.set("100000")
        self.ui._save_profile()
        existing = job(salary=Salary(110000, 130000, "USD"))
        application_id, _ = self.ui.applications.prepare(existing, self.ui.profile)
        self.ui.applications.approve(application_id, digest(self.ui.applications.get(application_id)["payload"]))
        snapshot = self.ui.applications.get(application_id)["approved_digest"]
        relevant = job(source_id="new", company="New Synthetic Company", title="Platform Specialist",
                       url="https://example.invalid/new", salary=Salary(120000, 140000, "USD"))
        unknown_pay = job(source_id="unknown", company="Unknown Pay Company",
                         url="https://example.invalid/unknown")

        def fake_discover(store, sources):
            store.save_jobs([existing, relevant, unknown_pay])
            return SearchResult([existing, relevant, unknown_pay], ["Synthetic public sources"])
        with patch("job_agent.desktop.discover", side_effect=fake_discover):
            self.ui._search(advance=True)
            self._wait_for(lambda: "search" not in self.ui.tasks)
        self.assertEqual(2, len(self.ui.applications.list()))
        self.assertEqual(snapshot, self.ui.applications.get(application_id)["approved_digest"])
        self.assertEqual("approved", self.ui.applications.get(application_id)["state"])
        self.assertTrue(self.ui.job_tree.exists(relevant.id))
        self.assertFalse(self.ui.job_tree.exists(unknown_pay.id))

    def test_optional_details_tracking_and_external_application_dialogs_render(self):
        _, self.ui.profile.resume = self.store.import_resume(FIXTURE)
        application_id, _ = self.ui.applications.prepare(job(), self.ui.profile)
        self.ui._show_applications()
        self.ui.application_tree.selection_set(application_id)
        self.ui._load_application()
        self.ui._answers_dialog()
        self.ui._stage_dialog()
        self.ui._external_dialog()
        self.ui._delivery_dialog()
        self.root.update_idletasks()
        dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(4, len(dialogs))
        for dialog in dialogs:
            dialog.destroy()

    def test_imported_sent_records_are_readonly_and_do_not_trap_application_switching(self):
        first, _ = self.ui.applications.import_external(
            job(), portal="LinkedIn", applied_at="2026-01-01", stage="interview")
        second, _ = self.ui.applications.import_external(
            job(company="Another synthetic company", source_id="second", url="https://example.invalid/second"),
            portal="Indeed", applied_at="2026-01-02", stage="submitted")
        self.ui._show_applications()
        self.ui.application_tree.selection_set(first)
        self.ui._load_application()
        self.assertEqual("disabled", str(self.ui.a_letter["state"]))
        self.assertTrue(all(str(entry["state"]) == "readonly" for entry in self.ui.identity_entries))
        self.ui.a_letter.insert("end", "This edit must not be possible")
        self.ui.application_tree.selection_set(second)
        self.ui._load_application()
        self.assertEqual(second, self.ui.selected_application)


if __name__ == "__main__":
    unittest.main()
