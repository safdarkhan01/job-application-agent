from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from job_agent.desktop import Desktop
from job_agent.models import digest
from job_agent.storage import Store
from tests.test_agent import FIXTURE, job


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
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._close)

    def _close(self):
        self.root.update_idletasks()
        self.root.destroy()

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
        with patch("job_agent.desktop.messagebox.showinfo"), patch("job_agent.desktop.webbrowser.open", return_value=True) as browser:
            self.ui.handoff_button.invoke()
        self.assertEqual(1, browser.call_count)
        self.assertEqual("manual_handoff", self.ui.applications.get(application_id)["state"])
        with patch("job_agent.desktop.messagebox.askyesno", return_value=True):
            self.ui._confirm_manual()
        self.assertEqual("user-confirmed-manual", self.ui.applications.get(application_id)["receipt"])
        self.assertIn("user reported", self.ui.application_tree.item(application_id, "values")[2])

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


if __name__ == "__main__":
    unittest.main()
