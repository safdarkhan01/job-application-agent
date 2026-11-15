from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import asdict
import json
import logging
from pathlib import Path
import queue
import sqlite3
import threading
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from .connectors import connectors, utc_now
from .matching import rank
from .models import Job, Profile, Salary, deduplicate, digest, public_url
from .portals import PORTALS
from .service import Applications, discover
from .storage import Store


LOG = logging.getLogger(__name__)


def csv(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def money(job: Job) -> str:
    salary = job.salary
    if salary.minimum is None and salary.maximum is None:
        return "Unknown" + (" (" + salary.evidence + ")" if salary.evidence else "")
    low = f"{salary.minimum:g}" if salary.minimum is not None else "?"
    high = f"{salary.maximum:g}" if salary.maximum is not None else "?"
    return f"{salary.currency} {low} - {high} / {salary.period}"


class Desktop:
    def __init__(self, root: tk.Tk, store: Store):
        self.root, self.store = root, store
        self.profile = store.profile()
        self.board_settings = store.get("boards", {"board": store.get("greenhouse", ""), "lever": "", "ashby": "", "lever_eu": ""})
        self.sources = connectors(**self.board_settings)
        self.applications = Applications(store, self.sources, recover_interrupted=True)
        self.jobs: dict[str, Job] = {}
        self.selected_application: str | None = None
        self.messages: queue.Queue = queue.Queue()
        self.busy = False
        root.title("Job Application Agent | Local-first")
        root.geometry("1220x860")
        root.minsize(1000, 740)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", padding=(10, 6))
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Subhead.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Notice.TLabel", foreground="#92400e", font=("Segoe UI", 10, "bold"))
        header = ttk.Frame(root, padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Job Application Agent", style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text="Discover real postings. Prepare and review locally. Submit manually on the employer website.").pack(anchor="w")
        ttk.Label(header, text="Automatic submission: NOT IMPLEMENTED. Supported APIs provide discovery only or require "
                  "employer-owned submission access.", style="Notice.TLabel", wraplength=1100).pack(anchor="w", pady=(5, 0))
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=14)
        self.profile_tab = ttk.Frame(self.tabs, padding=12)
        self.discovery_tab = ttk.Frame(self.tabs, padding=12)
        self.review_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.profile_tab, text="1  Resume & preferences")
        self.tabs.add(self.discovery_tab, text="2  Discover & match")
        self.tabs.add(self.review_tab, text="3  Prepare & manual apply")
        self.status = tk.StringVar(value="Local data: " + str(store.directory))
        ttk.Label(root, textvariable=self.status, padding=(16, 8), wraplength=1150).pack(fill="x")
        self._profile_ui()
        self._discovery_ui()
        self._review_ui()
        self._show_jobs()
        self._show_applications()
        root.after(100, self._drain_messages)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        if self.busy and not messagebox.askyesno("Discovery in progress", "Close while public feeds are refreshing?"):
            return
        self.root.destroy()

    def action(self, operation):
        try:
            return operation()
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("Action could not be completed", str(exc), parent=self.root)
            self.status.set("Action failed. " + str(exc))
            return None

    @staticmethod
    def _entry(parent, label, value="", row=0, width=48):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=4)
        variable = tk.StringVar(value=value)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", pady=4)
        return variable

    @staticmethod
    def _text(parent, height=5):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=4)
        text = tk.Text(frame, height=height, wrap="word", undo=True, font=("Segoe UI", 10))
        scrollbar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    @staticmethod
    def _replace(text, value, readonly=False):
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", value)
        if readonly:
            text.configure(state="disabled")

    def _profile_ui(self):
        top = ttk.Frame(self.profile_tab)
        top.pack(fill="x")
        ttk.Button(top, text="Import resume (PDF / DOCX / text)", command=lambda: self.action(self._import)).pack(side="left")
        self.resume_label = tk.StringVar(value=self.profile.resume.get("filename", "No resume imported"))
        ttk.Label(top, textvariable=self.resume_label, padding=8).pack(side="left")
        ttk.Label(self.profile_tab, text="Local parsing only. Extracted keywords are suggestions, not verified qualifications. "
                  "Review every field. Name and location are never guessed.", wraplength=1100).pack(anchor="w", pady=6)
        columns = ttk.Frame(self.profile_tab)
        columns.pack(fill="x")
        identity = ttk.LabelFrame(columns, text="Editable profile", padding=10)
        identity.pack(side="left", fill="both", expand=True, padx=(0, 8))
        identity.columnconfigure(1, weight=1)
        self.p_name = self._entry(identity, "Full name", self.profile.name, 0)
        self.p_email = self._entry(identity, "Email", self.profile.email, 1)
        self.p_titles = self._entry(identity, "Desired titles, comma-separated", ", ".join(self.profile.titles), 2)
        self.p_skills = self._entry(identity, "Skills, comma-separated", ", ".join(self.profile.skills), 3)
        preferences = ttk.LabelFrame(columns, text="Search preferences", padding=10)
        preferences.pack(side="left", fill="both", expand=True)
        preferences.columnconfigure(1, weight=1)
        self.p_locations = self._entry(preferences, "Locations, comma-separated", ", ".join(self.profile.locations), 0)
        ttk.Label(preferences, text="Work arrangement").grid(row=1, column=0, sticky="w")
        self.p_remote = tk.StringVar(value=self.profile.remote)
        ttk.Combobox(preferences, textvariable=self.p_remote, values=("any", "remote", "onsite"),
                     state="readonly").grid(row=1, column=1, sticky="ew", pady=4)
        self.p_minimum = self._entry(preferences, "Minimum pay (blank = no filter)",
                                     "" if self.profile.minimum_salary is None else str(self.profile.minimum_salary), 2)
        self.p_currency = self._entry(preferences, "Currency (USD / EUR / INR / ...)", self.profile.currency, 3)
        ttk.Label(preferences, text="Pay period").grid(row=4, column=0, sticky="w")
        self.p_period = tk.StringVar(value=self.profile.period)
        ttk.Combobox(preferences, textvariable=self.p_period, values=("year", "month", "hour"),
                     state="readonly").grid(row=4, column=1, sticky="ew")
        ttk.Label(self.profile_tab, text="Experience evidence (editable; no inferred years or employers)",
                  style="Subhead.TLabel").pack(anchor="w", pady=(8, 0))
        self.p_experience = self._text(self.profile_tab, 3)
        self.p_experience.insert("1.0", self.profile.experience)
        ttk.Label(self.profile_tab, text="Extracted resume preview (local only; existing applications keep their own immutable resume copy)").pack(anchor="w")
        self.resume_preview = self._text(self.profile_tab, 5)
        self._replace(self.resume_preview, self.store.get("resume_preview", "Import a synthetic or your own resume to begin."), True)
        bottom = ttk.Frame(self.profile_tab)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Save profile & preferences", command=lambda: self.action(self._save_profile)).pack(side="left")
        ttk.Label(bottom, text="No external AI, telemetry, account sign-up, or profile upload. Local data is not encrypted.",
                  wraplength=800, padding=8).pack(side="left")

    def _read_profile(self):
        profile = Profile(
            name=self.p_name.get().strip(), email=self.p_email.get().strip(),
            titles=csv(self.p_titles.get()), skills=csv(self.p_skills.get()),
            experience=self.p_experience.get("1.0", "end-1c").strip(),
            locations=csv(self.p_locations.get()), remote=self.p_remote.get(),
            minimum_salary=float(self.p_minimum.get()) if self.p_minimum.get().strip() else None,
            currency=self.p_currency.get().strip().upper(), period=self.p_period.get(),
            resume=self.profile.resume,
        )
        profile.validate()
        return profile

    def _save_profile(self):
        self.profile = self._read_profile()
        self.store.save_profile(self.profile)
        self._show_jobs()
        self.status.set("Profile saved locally. Existing application snapshots are unchanged until you refresh them.")
        return True

    def _import(self):
        path = filedialog.askopenfilename(title="Import a resume - processed locally",
                                         filetypes=[("Supported resumes", "*.pdf *.docx *.txt *.md"), ("All files", "*.*")])
        if not path:
            return
        parsed, attachment = self.store.import_resume(Path(path))
        self.profile.resume = attachment
        self.resume_label.set(attachment["filename"])
        self.p_titles.set(", ".join(parsed.titles))
        self.p_skills.set(", ".join(parsed.skills))
        if not self.p_email.get().strip():
            self.p_email.set(parsed.email)
        self._replace(self.p_experience, parsed.experience)
        self._replace(self.resume_preview, parsed.text[:20000], True)
        self.store.set("resume_preview", parsed.text[:20000])
        self.status.set("Resume parsed locally. Review extracted suggestions and save the profile. No data was uploaded.")

    def _discovery_ui(self):
        toolbar = ttk.Frame(self.discovery_tab)
        toolbar.pack(fill="x")
        self.refresh_button = ttk.Button(toolbar, text="Refresh public feeds", command=lambda: self.action(self._search))
        self.refresh_button.pack(side="left")
        ttk.Button(toolbar, text="Import posting / LinkedIn URL", command=self._manual_dialog).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Portal catalog & capabilities", command=self._capabilities).pack(side="right")
        settings = ttk.Frame(self.discovery_tab)
        settings.pack(fill="x", pady=6)
        self.source_enabled = {source: tk.BooleanVar(value=self.store.get("enabled:" + source, True))
                               for source in ("remotive", "arbeitnow", "jobicy")}
        for key, label in (("remotive", "Remotive"), ("arbeitnow", "Arbeitnow"), ("jobicy", "Jobicy")):
            ttk.Checkbutton(settings, text=label, variable=self.source_enabled[key]).pack(side="left", padx=(0, 8))
        ttk.Button(settings, text="Configure Greenhouse / Lever / Ashby boards",
                   command=self._board_dialog).pack(side="left", padx=6)
        self.board_summary = tk.StringVar(value=self._board_summary())
        ttk.Label(settings, textvariable=self.board_summary).pack(side="left")
        ttk.Label(self.discovery_tab, text="Refresh contacts only the selected public feeds (your IP/User-Agent are visible). "
                  "Your resume/profile is never sent. Not a search of every site. Cached jobs may be closed.",
                  wraplength=1100).pack(anchor="w", pady=4)
        self.job_tree = ttk.Treeview(self.discovery_tab, columns=("score", "title", "company", "location", "pay", "review"),
                                     show="headings", height=10, selectmode="browse")
        for column, title, width in (("score", "Score", 55), ("title", "Job title", 230),
                                     ("company", "Company", 160), ("location", "Location", 190),
                                     ("pay", "Compensation", 210), ("review", "Fit", 130)):
            self.job_tree.heading(column, text=title)
            self.job_tree.column(column, width=width, minwidth=45)
        self.job_tree.pack(fill="both", expand=True)
        self.job_tree.bind("<<TreeviewSelect>>", lambda _: self._job_details())
        self.job_details = self._text(self.discovery_tab, 7)
        self._replace(self.job_details, "Refresh public feeds or manually import a posting. Select a row to see matching evidence.", True)
        buttons = ttk.Frame(self.discovery_tab)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Prepare selected application", command=lambda: self.action(self._prepare)).pack(side="left")
        ttk.Button(buttons, text="Open original posting", command=lambda: self.action(self._open_selected_job)).pack(side="left", padx=8)

    def _capabilities(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Portal catalog - real discovery versus manual access")
        dialog.geometry("950x680")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="No built-in connector submits applications. An employer/partner API is not the same as "
                  "permission to automate a candidate account. Search links and manual URLs are not API integrations.",
                  wraplength=900).pack(anchor="w", pady=6)
        tree = ttk.Treeview(frame, columns=("name", "access"), show="headings", height=12, selectmode="browse")
        tree.heading("name", text="Portal / ATS")
        tree.heading("access", text="Actual discovery capability")
        tree.column("name", width=200)
        tree.column("access", width=650)
        tree.pack(fill="x")
        details = self._text(frame, 7)
        for index, portal in enumerate(PORTALS):
            tree.insert("", "end", iid=str(index), values=(portal.name, portal.access))

        def selected():
            return PORTALS[int(tree.selection()[0])] if tree.selection() else None

        def show(_=None):
            portal = selected()
            if portal:
                configured = [source.label for source in self.sources.values()
                              if source.label.startswith(portal.name)]
                text = (f"{portal.name}: {portal.access}\n{portal.setup}\n"
                        f"Configured discovery: {', '.join(configured) or 'None'}\n"
                        "Automatic submission: NOT IMPLEMENTED.\n"
                        f"Submission access blocker: {portal.submission_access}\n"
                        f"Discovery reference: {portal.evidence_url}\n"
                        f"Submission reference: {portal.submission_reference_url or portal.evidence_url}\n"
                        "References were assessed during implementation; access/terms can change.")
                self._replace(details, text, True)
        tree.bind("<<TreeviewSelect>>", show)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="Open official reference",
                   command=lambda: self.action(lambda: self._open_url(selected().evidence_url) if selected() else None)).pack(side="left")
        ttk.Button(actions, text="Open submission reference",
                   command=lambda: self.action(lambda: self._open_url(
                       selected().submission_reference_url or selected().evidence_url) if selected() else None)).pack(side="left", padx=6)
        ttk.Button(actions, text="Configure employer boards", command=self._board_dialog).pack(side="left", padx=6)
        ttk.Button(actions, text="Import posting manually", command=self._manual_dialog).pack(side="left")
        tree.selection_set("0")
        show()

    def _board_summary(self):
        count = sum(len(csv(value)) for value in self.board_settings.values())
        return f"{count}/10 employer boards configured"

    def _board_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configure public employer job boards")
        dialog.geometry("850x390")
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Enter comma-separated public employer board identifiers, not full URLs. "
                  "Up to 10 total boards. No employer API keys are needed for discovery; application submission "
                  "is NOT connected. ATS sources are employer-specific, not global portal search.", wraplength=790).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=8)
        values = {}
        for row, (key, label) in enumerate((
                ("board", "Greenhouse board tokens"),
                ("lever", "Lever global site slugs"),
                ("lever_eu", "Lever EU site slugs"),
                ("ashby", "Ashby board names")), start=1):
            values[key] = self._entry(frame, label, self.board_settings.get(key, ""), row=row)
        ttk.Label(frame, text="Find the identifier in an employer's public board URL, e.g. "
                  "jobs.lever.co/COMPANY or jobs.ashbyhq.com/BOARD. "
                  "Opening the catalog/reference does not connect any account.", wraplength=790).grid(
                      row=5, column=0, columnspan=2, sticky="w", pady=12)

        def save():
            settings = {key: value.get().strip() for key, value in values.items()}
            sources = connectors(**settings)
            self.store.set("boards", settings)
            self.board_settings, self.sources = settings, sources
            self.applications.connectors = sources
            self.board_summary.set(self._board_summary())
            self.status.set("Public employer boards saved locally. Refresh feeds to retrieve postings.")
            dialog.destroy()
        ttk.Button(frame, text="Save discovery configuration", command=lambda: self.action(save)).grid(
            row=6, column=0, columnspan=2, sticky="w")

    def _search(self):
        if self.busy:
            return
        sources = connectors(**self.board_settings)
        active = {key: value for key, value in sources.items()
                  if ":" in key or self.source_enabled[key].get()}
        if not active:
            raise ValueError("Select at least one source or configure an employer board.")
        for key, value in self.source_enabled.items():
            self.store.set("enabled:" + key, value.get())
        self.sources = sources
        self.applications.connectors = sources
        self.busy = True
        self.refresh_button.configure(state="disabled")
        self.status.set("Refreshing selected public feeds. Local profile data is not transmitted.")

        def worker():
            try:
                self.messages.put(("search", discover(self.store, active)))
            except Exception as exc:
                self.messages.put(("error", f"Discovery failed ({type(exc).__name__}): {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_messages(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                self.busy = False
                self.refresh_button.configure(state="normal")
                if kind == "search":
                    self._show_jobs()
                    self.status.set(f"{len(value.jobs)} distinct feed jobs retrieved. Select a job for match reasons.")
                    self._replace(self.job_details, "\n".join(value.messages) + "\n\n"
                                  "Attribution: Remotive (remotive.com), Arbeitnow (arbeitnow.com), Jobicy (jobicy.com), "
                                  "and configured employer ATS boards. Open the source link for authoritative details.", True)
                else:
                    self.status.set(value)
                    messagebox.showerror("Discovery failed", value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _show_jobs(self):
        self.jobs = {job.id: job for job in deduplicate(self.store.jobs())}
        self.job_tree.delete(*self.job_tree.get_children())
        ordered = sorted(self.jobs.values(), key=lambda job: rank(job, self.profile).score, reverse=True)
        for job in ordered:
            match = rank(job, self.profile)
            fit = "Candidate fit" if match.eligible else "Needs review"
            if match.role == "mismatch" or match.location == "mismatch" or match.compensation == "below":
                fit = "Outside preferences"
            self.job_tree.insert("", "end", iid=job.id, values=(
                match.score, job.title, job.company, job.location, money(job), fit))

    def _selected_job(self):
        selected = self.job_tree.selection()
        if not selected:
            raise ValueError("Select a job first.")
        return self.jobs[selected[0]]

    def _job_details(self):
        if not self.job_tree.selection():
            return
        job = self._selected_job()
        match = rank(job, self.profile)
        self._replace(self.job_details, (
            f"{job.title} | {job.company}\n{job.url}\n"
            f"Sources: {', '.join(job.sources)} | Retrieved: {job.fetched_at or 'manual import'}\n"
            f"Score: {match.score}/100 (ranking heuristic, not a probability)\n"
            + "\n".join(match.reasons)
            + "\nApplication: manual handoff only. Check availability, required questions and eligibility on the original site.\n\n"
            + job.description), True)

    def _open_url(self, url):
        if not webbrowser.open(public_url(url)):
            raise ValueError("No browser could be opened. Copy the posting URL from the details panel.")

    def _open_selected_job(self):
        self._open_url(self._selected_job().url)

    def _manual_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Manual posting import - LinkedIn and other sites")
        dialog.geometry("790x670")
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Paste the posting yourself. This does not fetch the page or access your LinkedIn profile.",
                  wraplength=730).grid(row=0, column=0, columnspan=2, sticky="w", pady=6)
        fields = {}
        for row, (key, label) in enumerate((
                ("url", "Posting HTTPS URL"), ("title", "Job title"), ("company", "Company"),
                ("location", "Location / eligible countries"), ("minimum", "Minimum pay (optional)"),
                ("maximum", "Maximum pay (optional)"), ("currency", "Currency (e.g. INR)")), start=1):
            fields[key] = self._entry(frame, label, row=row)
        remote = tk.StringVar(value="unknown")
        period = tk.StringVar(value="year")
        ttk.Label(frame, text="Work arrangement").grid(row=8, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=remote, values=("unknown", "remote", "onsite"), state="readonly").grid(row=8, column=1, sticky="ew")
        ttk.Label(frame, text="Pay period").grid(row=9, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=period, values=("year", "month", "hour"), state="readonly").grid(row=9, column=1, sticky="ew")
        ttk.Label(frame, text="Posting description (paste, do not include private profile information)").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=8)
        description = tk.Text(frame, height=8, wrap="word")
        description.grid(row=11, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(11, weight=1)

        def save():
            url = public_url(fields["url"].get())
            salary = Salary(
                float(fields["minimum"].get()) if fields["minimum"].get().strip() else None,
                float(fields["maximum"].get()) if fields["maximum"].get().strip() else None,
                fields["currency"].get().strip().upper(), period.get(), "User-entered from the original posting")
            job = Job("manual", url, fields["title"].get().strip(), fields["company"].get().strip(),
                      fields["location"].get().strip(), url, description.get("1.0", "end-1c"),
                      {"unknown": None, "remote": True, "onsite": False}[remote.get()],
                      salary=salary, fetched_at=utc_now())
            self.store.save_jobs([job])
            self._show_jobs()
            self.job_tree.selection_set(job.id)
            self.job_tree.see(job.id)
            self._job_details()
            self.status.set("Posting imported locally. No website or LinkedIn profile was accessed.")
            dialog.destroy()
        ttk.Button(frame, text="Import locally", command=lambda: self.action(save)).grid(row=12, column=0, sticky="w", pady=10)

    def _prepare(self):
        job = self._selected_job()
        profile = self._read_profile()
        if asdict(profile) != asdict(self.store.profile()):
            raise ValueError("Save your profile and preferences before preparing an application.")
        application_id, created = self.applications.prepare(job, self.profile)
        self._show_applications()
        self.application_tree.selection_set(application_id)
        self._load_application()
        self.tabs.select(self.review_tab)
        self.status.set("Prepared locally; review the full payload." if created else
                        "Existing application opened. Duplicate preparation/submission prevented.")

    def _review_ui(self):
        self.application_tree = ttk.Treeview(self.review_tab, columns=("title", "company", "state"),
                                             show="headings", height=5, selectmode="browse")
        for column, title, width in (("title", "Job", 390), ("company", "Company", 250), ("state", "Durable state", 180)):
            self.application_tree.heading(column, text=title)
            self.application_tree.column(column, width=width)
        self.application_tree.pack(fill="x")
        self.application_tree.bind("<<TreeviewSelect>>", lambda _: self.action(self._load_application))
        self.review_notebook = ttk.Notebook(self.review_tab)
        self.review_notebook.pack(fill="both", expand=True, pady=8)
        edit = ttk.Frame(self.review_notebook, padding=8)
        preview = ttk.Frame(self.review_notebook, padding=8)
        self.review_notebook.add(edit, text="Edit concrete application")
        self.review_notebook.add(preview, text="Full payload & audit history")
        form = ttk.Frame(edit)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.a_name = self._entry(form, "Candidate name", row=0)
        self.a_email = self._entry(form, "Candidate email", row=1)
        ttk.Label(edit, text="Cover letter (write/review your own; no unsupported claims are generated)").pack(anchor="w")
        self.a_letter = self._text(edit, 4)
        ttk.Label(edit, text='Answers as JSON text, e.g. {"Work authorization": "Your truthful answer"}. '
                  "Missing site questions still require manual completion.", wraplength=1050).pack(anchor="w")
        self.a_answers = self._text(edit, 3)
        ttk.Label(preview, text="This snapshot, including the resume SHA-256, is what approval covers. "
                  "The resume itself remains a separate local attachment.", wraplength=1050).pack(anchor="w")
        self.payload_preview = self._text(preview, 9)
        self.history = self._text(preview, 4)
        row = ttk.Frame(self.review_tab)
        row.pack(fill="x")
        ttk.Button(row, text="Save revision (invalidates approval)", command=lambda: self.action(self._save_application)).pack(side="left")
        ttk.Button(row, text="Use current profile / resume", command=lambda: self.action(lambda: self._save_application(True))).pack(side="left", padx=6)
        ttk.Button(row, text="Review & approve local payload", command=lambda: self.action(self._approve)).pack(side="left")
        row2 = ttk.Frame(self.review_tab)
        row2.pack(fill="x", pady=(6, 0))
        self.handoff_button = ttk.Button(row2, text="Open site for manual submission", command=lambda: self.action(self._dispatch))
        self.handoff_button.pack(side="left")
        ttk.Button(row2, text="I completed the manual application", command=lambda: self.action(self._confirm_manual)).pack(side="left", padx=6)
        ttk.Button(row2, text="Resolve uncertain outcome", command=lambda: self.action(self._reconcile)).pack(side="left")
        ttk.Label(self.review_tab, text="Automatic submission is NOT IMPLEMENTED. Approval only records the local intended payload. "
                  "Manual completion is recorded only on your confirmation; uncertain attempts are never retried automatically.",
                  wraplength=1100).pack(anchor="w", pady=(8, 0))

    def _show_applications(self):
        selected = self.selected_application
        self.application_tree.delete(*self.application_tree.get_children())
        for application in self.applications.list():
            job = application["job"]
            state = {"prepared": "prepared (local only)", "approved": "approved (local only)",
                     "manual_handoff": "manual handoff; not sent"}.get(application["state"], application["state"])
            if application["state"] == "submitted" and application["receipt"] == "user-confirmed-manual":
                state = "submitted (user reported)"
            self.application_tree.insert("", "end", iid=application["id"],
                                         values=(job.title, job.company, state))
        if selected and self.application_tree.exists(selected):
            self.application_tree.selection_set(selected)

    def _load_application(self):
        selected = self.application_tree.selection()
        if not selected:
            return
        application = self.applications.get(selected[0])
        self.selected_application = selected[0]
        payload = application["payload"]
        self.a_name.set(payload["candidate"]["name"])
        self.a_email.set(payload["candidate"]["email"])
        self._replace(self.a_letter, payload["cover_letter"])
        self._replace(self.a_answers, json.dumps(payload["answers"], indent=2, ensure_ascii=False))
        self._replace(self.payload_preview, json.dumps(payload, indent=2, ensure_ascii=False), True)
        self._replace(self.history, "State: " + application["state"] + "\n" +
                      "\n".join(f"{event['at']} | {event['state']} | {event['message']}"
                                for event in self.applications.events(application["id"])), True)

    def _application_id(self):
        if not self.selected_application:
            raise ValueError("Prepare or select an application first.")
        return self.selected_application

    def _save_application(self, refresh_profile=False):
        application_id = self._application_id()
        profile = self.store.profile() if refresh_profile else None
        changed = self.applications.edit(
            application_id, name=profile.name if profile else self.a_name.get(),
            email=profile.email if profile else self.a_email.get(),
            cover_letter=self.a_letter.get("1.0", "end-1c"),
            answers=json.loads(self.a_answers.get("1.0", "end-1c") or "{}"),
            profile=profile,
        )
        self._show_applications()
        self._load_application()
        self.status.set("Revision saved. Previous approval invalidated." if changed else "Payload unchanged.")
        return changed

    def _approve(self):
        self._save_application()
        self.review_notebook.select(1)
        payload = self.applications.get(self._application_id())["payload"]
        application_id = self._application_id()
        reviewed_digest = digest(payload)
        dialog = tk.Toplevel(self.root)
        dialog.title("Review intended manual application - nothing will be sent")
        dialog.geometry("900x700")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Automatic submission is NOT IMPLEMENTED. Approval only saves your consent to this local intended "
                  "payload. Review all facts, answers, letter, target URL, and attachment; then complete submission yourself "
                  "on the employer website.", wraplength=850).pack(anchor="w")
        content = self._text(frame, 20)
        self._replace(content, json.dumps(payload, indent=2, ensure_ascii=False), True)
        ttk.Button(frame, text="Open local resume for review",
                   command=lambda: self.action(lambda: self._open_resume(payload["resume"]))).pack(anchor="w", pady=6)

        def approve():
            self.applications.approve(application_id, reviewed_digest)
            dialog.destroy()
            self._show_applications()
            self._load_application()
            self.status.set("Exact application approved. Nothing submitted. Continue to the manual handoff.")
        ttk.Button(frame, text="I approve this local payload for manual application",
                   command=lambda: self.action(approve)).pack(side="left")
        ttk.Button(frame, text="Cancel", command=dialog.destroy).pack(side="right")
        dialog.transient(self.root)
        dialog.grab_set()

    def _open_resume(self, resume):
        self.store.verify_resume(resume)
        if not webbrowser.open(Path(resume["path"]).resolve().as_uri()):
            raise ValueError("Could not open the local resume. Open its path from the payload in your file manager.")

    def _dispatch(self):
        self._save_application()
        application_id = self._application_id()
        state, message = self.applications.dispatch(application_id)
        self._show_applications()
        self._load_application()
        messagebox.showinfo("Application: " + state, message, parent=self.root)
        self.status.set(message)
        if state == "manual_handoff":
            self._open_url(self.applications.get(application_id)["payload"]["job"]["url"])

    def _confirm_manual(self):
        application_id = self._application_id()
        if messagebox.askyesno("Confirm your manual completion",
                              "Did you actually complete submission on the employer's website?\n"
                              "The app cannot verify this. This records your confirmation, not an API submission.",
                              parent=self.root):
            self.applications.confirm_manual(application_id)
            self._show_applications()
            self._load_application()
            self.status.set("Recorded as user-confirmed manual submission, not app-verified submission.")

    def _reconcile(self):
        application_id = self._application_id()
        current = self.applications.get(application_id)
        if current["state"] != "unknown":
            raise ValueError("Select an application with an unknown submission outcome.")
        submitted = messagebox.askyesnocancel(
            "Verify with employer first", "Has the employer confirmed receipt?\n"
            "Yes: confirmed received. No: confirmed NOT received. Cancel: still uncertain.", parent=self.root)
        if submitted is None:
            return
        evidence = simpledialog.askstring("Employer verification", "Employer confirmation/reference or verification details:", parent=self.root)
        if evidence is None:
            return
        self.applications.reconcile(application_id, submitted=submitted, evidence=evidence)
        self._show_applications()
        self._load_application()


def main(argv=None):
    parser = ArgumentParser(description="Local-first desktop job application assistant")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Open and close the real desktop with isolated temporary synthetic data, without network access")
    options = parser.parse_args(argv)
    root = tk.Tk()
    root.withdraw()
    callback_failed = False

    def report_error(exc_type, exc_value, traceback):
        nonlocal callback_failed
        callback_failed = True
        LOG.error("Unhandled desktop error", exc_info=(exc_type, exc_value, traceback))
        if options.smoke_test:
            root.destroy()
            return
        messagebox.showerror("Unexpected error", f"{exc_type.__name__}: {exc_value}\n"
                             "The action did not complete. Do not retry an uncertain submission.", parent=root)

    root.report_callback_exception = report_error
    instance_lock = None
    temporary = tempfile.TemporaryDirectory() if options.smoke_test else None
    try:
        store = Store(Path(temporary.name) if temporary else None)
        instance_lock = store.acquire_instance_lock()
        Desktop(root, store)
    except (ValueError, OSError, sqlite3.Error) as exc:
        if not options.smoke_test:
            messagebox.showerror("Cannot open local data", str(exc), parent=root)
        else:
            LOG.error("Packaged startup failed: %s", exc)
        root.destroy()
        if instance_lock:
            instance_lock.close()
        if temporary:
            temporary.cleanup()
        raise SystemExit(1) from exc
    try:
        root.deiconify()
        if options.smoke_test:
            root.after(1000, root.destroy)
        root.mainloop()
    finally:
        if instance_lock:
            instance_lock.close()
        if temporary:
            temporary.cleanup()
    if callback_failed and options.smoke_test:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
