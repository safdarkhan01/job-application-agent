from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import asdict
from datetime import date
import json
import logging
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from .connectors import connectors, utc_now
from .email_apply import EmailSender, MailAccount, email_body
from .market import MarketQuery, MarketReport, build_report, research, saved_report
from .matching import Match, rank
from .models import Job, Profile, Salary, deduplicate, digest, public_url
from .portals import PORTALS
from .service import Applications, TERMINAL, discover
from .storage import Store
from .tracking import APPLIED_STAGES, STAGES, STAGE_LABELS, Tracker, hiring_badge


LOG = logging.getLogger(__name__)
TECHNICAL_LABELS = {
    "prepared": "Draft", "approved": "Approved; not sent", "submitting": "Sending",
    "manual_handoff": "Manual; not sent", "email_sent": "Email sent; receipt unverified",
    "submitted": "Submitted", "rejected": "Send refused", "needs_input": "Needs attention",
    "unknown": "Uncertain; do not retry", "discarded": "Discarded",
}


def csv(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def money(job: Job) -> str:
    salary = job.salary
    if salary.minimum is None and salary.maximum is None:
        return "Not disclosed"
    low = f"{salary.minimum:,.0f}" if salary.minimum is not None else "?"
    high = f"{salary.maximum:,.0f}" if salary.maximum is not None else "?"
    return f"{salary.currency} {low}-{high}/{salary.period}"


class Desktop:
    def __init__(self, root: tk.Tk, store: Store):
        self.root, self.store = root, store
        self.profile = store.profile()
        self.board_settings = store.get("boards", {"board": store.get("greenhouse", ""), "lever": "", "ashby": "", "lever_eu": ""})
        self.sources = connectors(**self.board_settings)
        self.mailer = EmailSender()
        self.applications = Applications(store, self.sources, recover_interrupted=True, email_sender=self.mailer)
        self.tracker = Tracker(store)
        self.jobs: dict[str, Job] = {}
        self.matches: dict[str, Match] = {}
        self.selected_application: str | None = None
        self.application_answers: dict[str, str] = {}
        self.loaded_payload: dict | None = None
        self.search_messages: list[str] = []
        self.market_report: MarketReport | None = None
        self.messages: queue.Queue = queue.Queue()
        self.tasks: set[str] = set()
        self.sending_application_id: str | None = None
        root.title("Job Application Agent")
        root.geometry("1160x790")
        root.minsize(1000, 710)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f4f6fa", foreground="#17263c")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa")
        style.configure("TButton", padding=(12, 7))
        style.configure("Accent.TButton", background="#2459c4", foreground="white", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#19449d"), ("disabled", "#cad3e2")])
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground="#59677d", font=("Segoe UI", 9))
        style.configure("Notice.TLabel", foreground="#87550a", font=("Segoe UI", 9))
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 8), font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dce8ff")], foreground=[("selected", "#123a79")])
        style.configure("TNotebook.Tab", padding=(18, 9))
        header = ttk.Frame(root, padding=(20, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Job Application Agent", style="Header.TLabel").pack(side="left")
        ttk.Button(header, text="Help", command=self._help).pack(side="right")
        self.connection_badge = tk.StringVar(value="Email: not configured | Portal forms: manual")
        ttk.Label(header, textvariable=self.connection_badge, style="Muted.TLabel", padding=(12, 0)).pack(side="right")
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=16)
        self.profile_tab, self.connections_tab, self.discovery_tab, self.review_tab = (
            ttk.Frame(self.tabs, padding=16) for _ in range(4))
        for frame, title in ((self.profile_tab, "1  Profile"), (self.connections_tab, "2  Connections"),
                             (self.discovery_tab, "3  Jobs"), (self.review_tab, "4  Applications")):
            self.tabs.add(frame, text=title)
        self.status = tk.StringVar(value="Import your CV to start. Your profile stays local.")
        ttk.Label(root, textvariable=self.status, style="Muted.TLabel", padding=(20, 10), wraplength=1100).pack(fill="x")
        self._profile_ui()
        self._connections_ui()
        self._discovery_ui()
        self._review_ui()
        self._show_jobs()
        self._show_applications()
        root.after(100, self._drain_messages)
        root.after(1000, self._background_tick)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        if self.tasks and not messagebox.askyesno("Work in progress", "Close now? An in-flight send may need verification."):
            return
        self._shutdown()

    def _shutdown(self):
        self.mailer.clear()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def action(self, operation):
        try:
            return operation()
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("Needs attention", str(exc), parent=self.root)
            self.status.set(str(exc))
            return None

    @staticmethod
    def _entry(parent, label, value="", row=0, width=34):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=6)
        variable = tk.StringVar(value=value)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", pady=6)
        return variable

    @staticmethod
    def _text(parent, height=5, *, expand=True):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=expand, pady=5)
        text = tk.Text(frame, height=height, wrap="word", undo=True, font=("Segoe UI", 10),
                       background="white", foreground="#17263c", relief="flat", padx=10, pady=8)
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

    @staticmethod
    def _tree(parent, columns, *, height=8, expand=True):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=expand, pady=8)
        tree = ttk.Treeview(frame, columns=tuple(column[0] for column in columns),
                            show="headings", height=height, selectmode="browse")
        for column, title, width in columns:
            tree.heading(column, text=title)
            tree.column(column, width=width, minwidth=55)
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        horizontal = ttk.Scrollbar(frame, command=tree.xview, orient="horizontal")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        return tree

    def _dialog(self, title, geometry="820x600"):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        return dialog, frame

    def _details(self, title, content):
        dialog, frame = self._dialog(title)
        self._replace(self._text(frame, 20), content, True)
        ttk.Button(frame, text="Close", command=dialog.destroy).pack(anchor="e", pady=(8, 0))
        return dialog

    def _help(self):
        self._details("Help & privacy",
                      "1. Import a CV and review your profile.\n2. Choose public discovery sources or configure employer boards. "
                      "LinkedIn/Indeed account sign-in is unavailable, not connected.\n3. Review skill/pay/location matches. "
                      "Market evidence is not an employer offer.\n4. Review the exact application before sending.\n\n"
                      "Email apply is a separate channel for employers explicitly inviting applications by email. "
                      "Your own SMTP account must be configured for this session. Final approval sends the reviewed "
                      "message/answers/resume to that recipient. Other portal forms need manual completion.\n\n"
                      "No CV/profile data goes to discovery or market feeds. An approved email sends its disclosed "
                      "contents through your mail provider. No inbox access, portal scraping, external AI or telemetry. "
                      "SMTP passwords stay in memory until close; profile and history are local, not encrypted.\n\n"
                      "Open/recent does not mean an employer explicitly says actively hiring. Unknown pay/status remains unknown. "
                      "Listing availability and application stages are separate; manual tracking is user-reported.\n\n"
                      "Changing an application clears approval. Sent/uncertain sends are locked against accidental retries. "
                      "An outgoing SMTP acceptance does not prove employer delivery or receipt.\n\n"
                      "CV: PDF/DOCX/UTF-8 text up to 15 MB. Scanned PDFs need external OCR; DOC/encrypted PDFs unsupported. "
                      "Extracted skills/experience require your review.\n\n"
                      f"Local data: {self.store.directory}\nClose the app before backing up or deleting that app-specific directory.")

    def _profile_ui(self):
        top = ttk.Frame(self.profile_tab)
        top.pack(fill="x", pady=(0, 12))
        ttk.Button(top, text="Import CV", style="Accent.TButton", command=lambda: self.action(self._import)).pack(side="left")
        self.resume_label = tk.StringVar(value=self.profile.resume.get("filename", "No CV selected"))
        ttk.Label(top, textvariable=self.resume_label, padding=(12, 0)).pack(side="left")
        self.resume_text = self.store.get("resume_preview", "")
        ttk.Button(top, text="Preview", command=lambda: self._details("CV preview", self.resume_text or "Import a CV first.")).pack(side="right")
        columns = ttk.Frame(self.profile_tab)
        columns.pack(fill="x")
        identity = ttk.LabelFrame(columns, text="Your profile", padding=14)
        identity.pack(side="left", fill="both", expand=True, padx=(0, 10))
        identity.columnconfigure(1, weight=1)
        self.p_name = self._entry(identity, "Name", self.profile.name, 0)
        self.p_email = self._entry(identity, "Email", self.profile.email, 1)
        self.p_titles = self._entry(identity, "Job titles", ", ".join(self.profile.titles), 2)
        self.p_skills = self._entry(identity, "Skills", ", ".join(self.profile.skills), 3)
        preferences = ttk.LabelFrame(columns, text="Preferences", padding=14)
        preferences.pack(side="left", fill="both", expand=True)
        preferences.columnconfigure(1, weight=1)
        self.p_locations = self._entry(preferences, "Locations", ", ".join(self.profile.locations), 0)
        ttk.Label(preferences, text="Work style").grid(row=1, column=0, sticky="w")
        self.p_remote = tk.StringVar(value=self.profile.remote)
        ttk.Combobox(preferences, textvariable=self.p_remote, values=("any", "remote", "onsite"),
                     state="readonly").grid(row=1, column=1, sticky="ew", pady=6)
        self.p_minimum = self._entry(preferences, "Minimum pay",
                                    "" if self.profile.minimum_salary is None else str(self.profile.minimum_salary), 2)
        pay = ttk.Frame(preferences)
        pay.grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(preferences, text="Currency / period").grid(row=3, column=0, sticky="w")
        self.p_currency = tk.StringVar(value=self.profile.currency)
        self.p_period = tk.StringVar(value=self.profile.period)
        ttk.Entry(pay, textvariable=self.p_currency, width=8).pack(side="left", padx=(0, 8))
        ttk.Combobox(pay, textvariable=self.p_period, values=("year", "month", "hour"), width=12,
                     state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Label(self.profile_tab, text="Use commas for lists. Review all extracted facts.", style="Muted.TLabel").pack(anchor="w", pady=8)
        row = ttk.Frame(self.profile_tab)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Experience").pack(side="left")
        ttk.Button(row, text="Matching preferences", command=self._matching_preferences).pack(side="right")
        self.p_experience = self._text(self.profile_tab, 5)
        self.p_experience.insert("1.0", self.profile.experience)
        bottom = ttk.Frame(self.profile_tab)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, text="Stored on this device", style="Muted.TLabel").pack(side="left")
        ttk.Button(bottom, text="Next: Connections", style="Accent.TButton",
                   command=lambda: self.action(lambda: self._save_profile(advance=True))).pack(side="right")
        ttk.Button(bottom, text="Save", command=lambda: self.action(self._save_profile)).pack(side="right", padx=8)

    def _matching_preferences(self):
        dialog, frame = self._dialog("Matching preferences", "650x390")
        frame.columnconfigure(1, weight=1)
        skill = self._entry(frame, "Min. skill overlap (%)", str(self.profile.minimum_skill_percent), 0)
        ttk.Label(frame, text="Current level").grid(row=1, column=0, sticky="w")
        level = tk.StringVar(value=self.profile.current_level)
        ttk.Combobox(frame, textvariable=level, state="readonly", values=(
            "", "intern", "entry", "junior", "mid", "senior", "lead", "staff", "principal", "manager", "director")).grid(row=1, column=1, sticky="ew")
        higher = tk.BooleanVar(value=self.profile.include_higher)
        recent = tk.BooleanVar(value=self.profile.prefer_recent)
        ttk.Checkbutton(frame, text="Prefer same or higher level", variable=higher).grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Checkbutton(frame, text="Prefer recent postings", variable=recent).grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        days = self._entry(frame, "Recent means days", str(self.profile.recent_days), 4)
        ttk.Label(frame, text="Skills/pay come first. Level and publication date are tie-breaks, not qualifications.",
                  style="Muted.TLabel", wraplength=600).grid(row=5, column=0, columnspan=2, sticky="w", pady=12)

        def save():
            profile = self._read_profile()
            profile.minimum_skill_percent, profile.current_level = int(skill.get()), level.get()
            profile.include_higher, profile.prefer_recent, profile.recent_days = higher.get(), recent.get(), int(days.get())
            profile.validate()
            self.store.save_profile(profile)
            self.profile = profile
            self._show_jobs()
            dialog.destroy()
        ttk.Button(frame, text="Save", style="Accent.TButton", command=lambda: self.action(save)).grid(row=6, column=1, sticky="e")

    def _read_profile(self):
        profile = Profile(**{**asdict(self.profile),
            "name": self.p_name.get().strip(), "email": self.p_email.get().strip(),
            "titles": csv(self.p_titles.get()), "skills": csv(self.p_skills.get()),
            "experience": self.p_experience.get("1.0", "end-1c").strip(),
            "locations": csv(self.p_locations.get()), "remote": self.p_remote.get(),
            "minimum_salary": float(self.p_minimum.get()) if self.p_minimum.get().strip() else None,
            "currency": self.p_currency.get().strip().upper(), "period": self.p_period.get(),
        })
        profile.validate()
        return profile

    def _save_profile(self, advance=False):
        self.profile = self._read_profile()
        self.store.save_profile(self.profile)
        self._show_jobs()
        self.status.set("Profile saved. Existing application snapshots are unchanged.")
        if advance:
            self.tabs.select(self.connections_tab)
        return True

    def _import(self):
        path = filedialog.askopenfilename(title="Import CV",
                                         filetypes=[("Supported CVs", "*.pdf *.docx *.txt *.md"), ("All files", "*.*")])
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
        self.resume_text = parsed.text[:20000]
        self.store.set("resume_preview", self.resume_text)
        self.status.set("CV imported locally. Review the profile, then continue.")

    def _connections_ui(self):
        top = ttk.Frame(self.connections_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Discovery sources").pack(side="left")
        ttk.Button(top, text="Details", command=self._capabilities).pack(side="right")
        ttk.Button(top, text="Employer boards", command=self._board_dialog).pack(side="right", padx=8)
        selection = ttk.Frame(self.connections_tab)
        selection.pack(fill="x", pady=10)
        self.source_enabled = {source: tk.BooleanVar(value=self.store.get("enabled:" + source, True))
                               for source in ("remotive", "arbeitnow", "jobicy")}
        for key, label in (("remotive", "Remotive"), ("arbeitnow", "Arbeitnow"), ("jobicy", "Jobicy")):
            ttk.Checkbutton(selection, text=label, variable=self.source_enabled[key]).pack(side="left", padx=(0, 18))
        self.board_summary = tk.StringVar(value=self._board_summary())
        ttk.Label(selection, textvariable=self.board_summary, style="Muted.TLabel").pack(side="right")
        self.portal_tree = self._tree(self.connections_tab, (
            ("portal", "Portal", 170), ("access", "Discovery", 250), ("account", "Account / applying", 500)), height=7)
        for portal in PORTALS:
            available = portal.name in {"Remotive", "Arbeitnow", "Jobicy", "Greenhouse", "Lever", "Ashby"}
            self.portal_tree.insert("", "end", values=(portal.name,
                "Public API" if available else "Manual import only",
                "No applicant account needed; forms manual" if available else "Sign-in / automatic application unavailable"))
        mail = ttk.LabelFrame(self.connections_tab, text="Email applications", padding=12)
        mail.pack(fill="x", pady=10)
        self.mail_status = tk.StringVar(value="Not configured")
        ttk.Label(mail, textvariable=self.mail_status).pack(side="left")
        ttk.Button(mail, text="Mail account", command=self._mail_dialog).pack(side="right")
        ttk.Label(self.connections_tab, text="Email only when the employer invites it. Portal sign-in is not connected.",
                  style="Notice.TLabel").pack(anchor="w")
        bottom = ttk.Frame(self.connections_tab)
        bottom.pack(fill="x", pady=(14, 0))
        self.auto_refresh = tk.BooleanVar(value=self.store.get("background_discovery", False))
        ttk.Checkbutton(bottom, text="Refresh hourly while open", variable=self.auto_refresh,
                        command=lambda: self.store.set("background_discovery", self.auto_refresh.get())).pack(side="left")
        ttk.Button(bottom, text="Find matches", style="Accent.TButton",
                   command=lambda: self.action(lambda: self._search(advance=True))).pack(side="right")
        ttk.Label(self.connections_tab, text="Public GETs only; your CV/profile is not sent to discovery feeds.",
                  style="Muted.TLabel").pack(anchor="w", pady=6)

    def _capabilities(self):
        dialog, frame = self._dialog("Portal capabilities", "900x610")
        tree = self._tree(frame, (("name", "Portal", 180), ("access", "Access", 600)), height=8, expand=False)
        details = self._text(frame, 8)
        for index, portal in enumerate(PORTALS):
            tree.insert("", "end", iid=str(index), values=(portal.name, portal.access))

        def selected():
            return PORTALS[int(tree.selection()[0])] if tree.selection() else None

        def show(_=None):
            portal = selected()
            if portal:
                configured = [source.label for source in self.sources.values() if source.label.startswith(portal.name)]
                self._replace(details, f"{portal.setup}\n\nConfigured: {', '.join(configured) or 'None'}\n"
                              f"Portal automatic application: unavailable.\n{portal.submission_access}\n\n"
                              f"{portal.evidence_url}\n{portal.submission_reference_url}\n\n"
                              "Separate email delivery is possible only when the employer explicitly invites it; "
                              "it does not connect a portal account.", True)
        tree.bind("<<TreeviewSelect>>", show)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="Official reference", command=lambda: self.action(
            lambda: self._open_url(selected().submission_reference_url or selected().evidence_url) if selected() else None)).pack(side="left")
        ttk.Button(actions, text="Close", command=dialog.destroy).pack(side="right")
        tree.selection_set("0")
        show()

    def _board_summary(self):
        return f"{sum(len(csv(value)) for value in self.board_settings.values())}/10 employer boards"

    def _board_dialog(self):
        dialog, frame = self._dialog("Employer boards", "760x330")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Public board IDs, separated by commas. Discovery only; no account sign-in.",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=8)
        fields = {}
        for row, (key, label) in enumerate((
                ("board", "Greenhouse"), ("lever", "Lever"), ("lever_eu", "Lever EU"), ("ashby", "Ashby")), 1):
            fields[key] = self._entry(frame, label, self.board_settings.get(key, ""), row)

        def save():
            settings = {key: value.get().strip() for key, value in fields.items()}
            sources = connectors(**settings)
            self.store.set("boards", settings)
            self.board_settings, self.sources = settings, sources
            self.applications.connectors = sources
            self.board_summary.set(self._board_summary())
            self.status.set("Employer boards saved. Refresh jobs to load them.")
            dialog.destroy()
        ttk.Button(frame, text="Save", style="Accent.TButton", command=lambda: self.action(save)).grid(row=5, column=1, sticky="e", pady=12)

    def _mail_dialog(self):
        dialog, frame = self._dialog("Your mail account", "720x450")
        frame.columnconfigure(1, weight=1)
        saved = self.store.get("email_account", {})
        host = self._entry(frame, "SMTP host", saved.get("host", "smtp.gmail.com"), 0)
        username = self._entry(frame, "Username", saved.get("username", self.profile.email), 1)
        sender = self._entry(frame, "Sender email", saved.get("sender", self.profile.email), 2)
        security = tk.StringVar(value=saved.get("security", "ssl"))
        ttk.Label(frame, text="Security").grid(row=3, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=security, values=("ssl", "starttls"), state="readonly").grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="App / SMTP password").grid(row=4, column=0, sticky="w")
        password = tk.StringVar()
        ttk.Entry(frame, textvariable=password, show="*").grid(row=4, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="SSL uses 465; STARTTLS uses 587. Password is kept only until app close. "
                  "Your provider may require an app password or deny SMTP access.", style="Muted.TLabel",
                  wraplength=650).grid(row=5, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Label(frame, text="Saving does not contact the server or verify sign-in.", style="Notice.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=4)

        def save():
            account = MailAccount(host.get().strip(), 465 if security.get() == "ssl" else 587,
                                  security.get(), username.get().strip(), sender.get().strip())
            self.mailer.configure(account, password.get())
            self.store.set("email_account", asdict(account))
            password.set("")
            self.connection_badge.set("Email: session configured | Portal forms: manual")
            self.mail_status.set("Configured this session; sign-in not yet verified")
            self.status.set("Mail settings saved locally. Sign-in is checked only when you approve a send.")
            dialog.destroy()
        row = ttk.Frame(frame)
        row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Button(row, text="Provider help", command=lambda: self.action(lambda: self._open_url(
            "https://support.google.com/accounts/answer/185833"))).pack(side="left")
        ttk.Button(row, text="Use this session", style="Accent.TButton", command=lambda: self.action(save)).pack(side="right")

    def _discovery_ui(self):
        toolbar = ttk.Frame(self.discovery_tab)
        toolbar.pack(fill="x")
        self.refresh_button = ttk.Button(toolbar, text="Refresh", command=lambda: self.action(self._search))
        self.refresh_button.pack(side="left")
        ttk.Button(toolbar, text="Add job", command=self._manual_dialog).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Sources", command=lambda: self.tabs.select(self.connections_tab)).pack(side="left")
        ttk.Button(toolbar, text="Pay research", command=self._market_dialog).pack(side="right")
        ttk.Button(toolbar, text="Refresh log", command=lambda: self._details("Discovery log",
                   "\n".join(self.search_messages) or "No refresh yet.")).pack(side="right", padx=8)
        filters = ttk.Frame(self.discovery_tab)
        filters.pack(fill="x", pady=(12, 2))
        self.job_view = tk.StringVar(value="Recommended")
        for option in ("Recommended", "Needs review", "All"):
            ttk.Radiobutton(filters, text=option, value=option, variable=self.job_view,
                            command=self._render_jobs).pack(side="left", padx=(0, 16))
        self.jobs_count = tk.StringVar(value="No jobs yet")
        ttk.Label(filters, textvariable=self.jobs_count, style="Muted.TLabel").pack(side="right")
        self.job_tree = self._tree(self.discovery_tab, (
            ("score", "Fit", 50), ("title", "Role", 205), ("company", "Company", 145),
            ("pay", "Advertised pay", 200), ("skills", "Skills", 65), ("hiring", "Hiring", 95),
            ("source", "Source", 100), ("date", "Posted", 90)), height=10)
        self.job_tree.bind("<<TreeviewSelect>>", lambda _: self._job_details())
        self.job_tree.bind("<Double-1>", lambda _: self.action(self._job_details_dialog))
        self.job_summary = tk.StringVar(value="Find matches or add a posting.")
        ttk.Label(self.discovery_tab, textvariable=self.job_summary, wraplength=1040).pack(anchor="w", pady=8)
        buttons = ttk.Frame(self.discovery_tab)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="Review & apply", style="Accent.TButton",
                   command=lambda: self.action(self._prepare)).pack(side="right")
        ttk.Button(buttons, text="Discard", command=lambda: self.action(self._discard_job)).pack(side="right", padx=8)
        ttk.Button(buttons, text="Details", command=lambda: self.action(self._job_details_dialog)).pack(side="left")
        ttk.Button(buttons, text="Original posting", command=lambda: self.action(self._open_selected_job)).pack(side="left", padx=8)
        ttk.Label(self.discovery_tab, text="Pay/skill fit first. 'Open' is not an actively-hiring claim.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

    def _start_worker(self, key, work):
        if key in self.tasks:
            return
        self.tasks.add(key)

        def worker():
            try:
                self.messages.put((key, work(), None))
            except Exception as exc:
                self.messages.put((key, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def _background_tick(self):
        if self.auto_refresh.get() and self.profile.resume and "search" not in self.tasks:
            self.action(self._search)
        self.root.after(60 * 60 * 1000, self._background_tick)

    def _rank_jobs(self, jobs, profile):
        results = []
        for job in deduplicate(jobs):
            listing = self.tracker.availability(job)
            status = "closed" if listing.get("last_known_status") == "closed" else listing["status"]
            results.append((job, rank(job, profile, hiring=status)))
        return results

    def _search(self, advance=False):
        if "search" in self.tasks:
            return
        profile = self.store.profile()
        profile_version = digest(asdict(profile))
        if not profile.resume or not profile.skills:
            raise ValueError("Import and save your CV/profile before finding matches.")
        sources = connectors(**self.board_settings)
        active = {key: value for key, value in sources.items() if ":" in key or self.source_enabled[key].get()}
        if not active:
            raise ValueError("Choose a public source or employer board in Connections.")
        for key, value in self.source_enabled.items():
            self.store.set("enabled:" + key, value.get())
        self.sources = sources
        self.applications.connectors = sources
        self.refresh_button.configure(state="disabled")
        self.status.set("Finding matches in the background...")
        if advance:
            self.tabs.select(self.discovery_tab)

        def work():
            result = discover(self.store, active)
            ranked = self._rank_jobs(self.store.jobs(), profile)
            prepared = 0
            if digest(asdict(self.store.profile())) == profile_version:
                for job, match in sorted(ranked, key=lambda pair: pair[1].order, reverse=True):
                    if prepared >= 20:
                        break
                    if digest(asdict(self.store.profile())) != profile_version:
                        break
                    if match.eligible and not self.tracker.is_closed(job):
                        _, created = self.applications.prepare(job, profile)
                        prepared += int(created)
            report = None
            if profile.titles and profile.locations:
                report = build_report(self.store, MarketQuery(
                    profile.titles[0], profile.locations[0], profile.current_level, profile.currency),
                    result.jobs, result.messages)
            return result, ranked, prepared, report, profile_version
        self._start_worker("search", work)

    def _drain_messages(self):
        try:
            while True:
                kind, value, error = self.messages.get_nowait()
                self.tasks.discard(kind)
                if kind == "search":
                    self.refresh_button.configure(state="normal")
                if error is not None:
                    if kind == "send" and self.selected_application == self.sending_application_id:
                        self._show_applications()
                        self._load_application(force=True)
                    if kind == "send":
                        self.sending_application_id = None
                    self.status.set(f"{kind}: {type(error).__name__}: {error}")
                    messagebox.showerror("Needs attention", str(error), parent=self.root)
                    continue
                if kind == "search":
                    result, ranked, prepared, report, profile_version = value
                    self.search_messages = result.messages
                    if digest(asdict(self.profile)) == profile_version:
                        self._install_jobs(ranked)
                    else:
                        self._show_jobs()
                    self.market_report = report
                    self._show_applications()
                    self.status.set(f"{len(result.jobs)} feed jobs | {prepared} new drafts | "
                                    + (f"{result.failed_sources} sources need attention (Refresh log)" if result.failed_sources else "Refresh complete"))
                elif kind == "matches":
                    profile_version, ranked = value
                    if profile_version == digest(asdict(self.profile)):
                        self._install_jobs(ranked)
                    else:
                        self._show_jobs()
                elif kind == "send":
                    application_id, state, message = value
                    self._show_applications()
                    self._render_jobs()
                    if self.selected_application == application_id:
                        self._load_application(force=True)
                    self.sending_application_id = None
                    self.status.set(message)
                    if state in {"unknown", "needs_input", "rejected"}:
                        messagebox.showwarning("Application needs attention", message, parent=self.root)
                elif kind == "market":
                    self.market_report = value
                    self.status.set(value.summary)
                    self._show_market_report(value)
                elif kind == "listing":
                    self._show_jobs()
                    self._show_applications()
                    self.status.set("Listing: " + hiring_badge(value) + ". Application stage is unchanged.")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _show_jobs(self):
        jobs = self.store.jobs()
        if len(jobs) > 40:
            profile = Profile(**asdict(self.profile))
            self._start_worker("matches", lambda: (digest(asdict(profile)), self._rank_jobs(jobs, profile)))
        else:
            self._install_jobs(self._rank_jobs(jobs, self.profile))

    def _install_jobs(self, ranked):
        self.jobs = {job.id: job for job, _ in ranked}
        self.matches = {job.id: match for job, match in ranked}
        self._render_jobs()

    def _render_jobs(self):
        self.job_tree.delete(*self.job_tree.get_children())
        with self.store.connect() as db:
            progressed = {row["application_id"] for row in db.execute(
                "SELECT application_id,stage FROM application_tracking") if row["stage"] in APPLIED_STAGES}
        unavailable = {application["job"].id for application in self.applications.list()
                       if application["state"] in TERMINAL | {"discarded"} or application["id"] in progressed}
        visible = 0
        for job in sorted(self.jobs.values(), key=lambda value: self.matches[value.id].order, reverse=True):
            match = self.matches[job.id]
            view = self.job_view.get()
            if view != "All" and (job.id in unavailable or (view == "Recommended") != match.eligible):
                continue
            record = self.tracker.availability(job)
            source = self.sources[job.source].label if job.source in self.sources else job.source
            self.job_tree.insert("", "end", iid=job.id, values=(
                match.score, job.title, job.company, money(job), f"{match.skill_ratio:.0%}",
                hiring_badge(record), source, job.published_at[:10] if match.age_days is not None else "Unknown"))
            visible += 1
        self.jobs_count.set(f"{visible} shown / {len(self.jobs)} saved")
        if not visible:
            self.job_summary.set("No verified matches yet. Check Needs review or adjust your preferences.")

    def _selected_job(self):
        selected = self.job_tree.selection()
        if not selected:
            raise ValueError("Select a job first.")
        return self.jobs[selected[0]]

    def _job_details(self):
        if self.job_tree.selection():
            job = self._selected_job()
            self.job_summary.set(f"{job.title} | {job.company} | {job.location}")

    def _job_details_dialog(self):
        job = self._selected_job()
        match = self.matches[job.id]
        listing = self.tracker.availability(job)
        report = self.market_report
        market_text = ""
        if report:
            market_text = "\n\nMARKET CONTEXT (not an employer offer)\n" + report.summary + "\n" + report.context(job)
        dialog = self._details(job.title,
            f"{job.company} | {job.location}\n{job.url}\nSources: {', '.join(job.sources)}\n"
            f"Published: {job.published_at or 'unknown'} | Retrieved: {job.fetched_at or 'unknown'}\n"
            f"Hiring: {hiring_badge(listing)} | Checked: {listing['checked_at'] or 'unknown'}\n"
            f"Evidence: {listing['evidence']}\nProvenance: {listing['provenance']} | {listing['source_url']}\n\n"
            f"Advertised pay: {money(job)} | Basis: {job.salary.basis}\n"
            + "\n".join(match.reasons) + market_text + "\n\nPOSTING\n" + job.description)
        return dialog

    def _open_url(self, url):
        if not webbrowser.open(public_url(url)):
            raise ValueError("Could not open a browser. Copy the URL from Details.")

    def _open_selected_job(self):
        self._open_url(self._selected_job().url)

    def _manual_dialog(self):
        dialog, frame = self._dialog("Add a posting", "760x650")
        frame.columnconfigure(1, weight=1)
        fields = {}
        for row, (key, label) in enumerate((
                ("url", "Posting HTTPS URL"), ("title", "Role"), ("company", "Company"),
                ("location", "Location"), ("minimum", "Minimum pay"), ("maximum", "Maximum pay"),
                ("currency", "Currency")), 0):
            fields[key] = self._entry(frame, label, row=row)
        remote, period = tk.StringVar(value="unknown"), tk.StringVar(value="year")
        ttk.Label(frame, text="Work style / pay period").grid(row=7, column=0, sticky="w")
        combo = ttk.Frame(frame)
        combo.grid(row=7, column=1, sticky="ew")
        ttk.Combobox(combo, textvariable=remote, values=("unknown", "remote", "onsite"), width=14,
                     state="readonly").pack(side="left")
        ttk.Combobox(combo, textvariable=period, values=("year", "month", "hour"), width=14,
                     state="readonly").pack(side="left", padx=8)
        ttk.Label(frame, text="Description").grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))
        description = tk.Text(frame, height=7, wrap="word", font=("Segoe UI", 10))
        description.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=6)
        frame.rowconfigure(9, weight=1)
        ttk.Label(frame, text="Manual import only. No account/page access; hiring status starts unverified.",
                  style="Muted.TLabel").grid(row=10, column=0, columnspan=2, sticky="w", pady=6)

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
            self.job_view.set("All")
            self._show_jobs()
            if self.job_tree.exists(job.id):
                self.job_tree.selection_set(job.id)
                self.job_tree.see(job.id)
                self._job_details()
            self.status.set("Posting added locally. Hiring status unverified.")
            dialog.destroy()
        ttk.Button(frame, text="Add job", style="Accent.TButton", command=lambda: self.action(save)).grid(row=11, column=1, sticky="e", pady=8)

    def _prepare(self):
        job = self._selected_job()
        if self.tracker.is_closed(job):
            raise ValueError("This listing was reported closed. Recheck it before applying.")
        profile = self._read_profile()
        if asdict(profile) != asdict(self.store.profile()):
            raise ValueError("Save your profile before preparing an application.")
        application_id, created = self.applications.prepare(job, profile)
        self._show_applications()
        self.application_tree.selection_set(application_id)
        self._load_application()
        self.tabs.select(self.review_tab)
        self.status.set("Review your draft, delivery and answers before Apply." if created else "Existing application opened; no duplicate created.")

    def _discard_job(self):
        job = self._selected_job()
        application_id, _ = self.applications.prepare(job, self.store.profile())
        self.applications.discard(application_id)
        self._show_applications()
        self._render_jobs()
        self.status.set("Discarded locally. Nothing sent.")

    def _market_dialog(self):
        dialog, frame = self._dialog("Salary evidence", "760x400")
        frame.columnconfigure(1, weight=1)
        role = self._entry(frame, "Generic role", self.profile.titles[0] if self.profile.titles else "", 0)
        location = self._entry(frame, "Location", self.profile.locations[0] if self.profile.locations else "", 1)
        level = tk.StringVar(value=self.profile.current_level)
        ttk.Label(frame, text="Level (blank = unspecified)").grid(row=2, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=level, state="readonly", values=(
            "", "intern", "entry", "junior", "mid", "senior", "lead", "staff", "principal", "manager", "director")).grid(row=2, column=1, sticky="ew")
        currency = self._entry(frame, "Currency", self.profile.currency, 3)
        ttk.Label(frame, text="Retrieves public Remotive, Arbeitnow and Jobicy feeds. No CV/identity sent. "
                  "Advertised samples are not a salary survey or this employer's offer.",
                  style="Muted.TLabel", wraplength=700).grid(row=4, column=0, columnspan=2, sticky="w", pady=12)

        def start():
            query_value = MarketQuery(role.get().strip(), location.get().strip(), level.get(), currency.get().strip().upper())
            query_value.validate()
            self._start_worker("market", lambda: research(self.store, query_value))
            self.status.set("Retrieving salary evidence in the background...")
            dialog.destroy()
        ttk.Button(frame, text="Research", style="Accent.TButton", command=lambda: self.action(start)).grid(row=5, column=1, sticky="e")
        if self.profile.titles and self.profile.locations:
            cached = saved_report(self.store, MarketQuery(self.profile.titles[0], self.profile.locations[0],
                                                        self.profile.current_level, self.profile.currency))
            if cached:
                ttk.Button(frame, text="Last report", command=lambda: self._show_market_report(cached)).grid(row=5, column=0, sticky="w")

    def _show_market_report(self, report):
        lines = [report.summary, f"Query: {report.query} | Researched: {report.researched_at}",
                 "\nSOURCES\n" + "\n".join(report.source_results)]
        for sample in report.samples:
            lines.append(f"\n{sample['title']} | {sample['location']} | {sample['source']}\n{sample['url']}\n"
                         f"{sample['currency']} {sample['minimum']:g}-{sample['maximum']:g}/{sample['period']} | "
                         f"Basis: {sample['basis']} | Level: {sample['level']} ({sample['level_comparability']})\n"
                         f"Published: {sample['published_at']} | Retrieved: {sample['retrieved_at']}\n"
                         f"Evidence: {sample['salary_evidence']} | {sample['sample_size']}")
        lines.append("\nCAVEATS\n" + "\n".join(report.caveats))
        return self._details("Salary evidence", "\n".join(lines))

    def _review_ui(self):
        toolbar = ttk.Frame(self.review_tab)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="One tracker across portals").pack(side="left")
        ttk.Button(toolbar, text="Add past application", command=self._external_dialog).pack(side="right")
        self.application_tree = self._tree(self.review_tab, (
            ("title", "Role", 270), ("company", "Company", 170), ("portal", "Portal", 110),
            ("stage", "Stage", 160), ("state", "Send status", 220), ("listing", "Hiring", 100)),
            height=4, expand=False)
        self.application_tree.bind("<<TreeviewSelect>>", lambda _: self.action(self._load_application))
        form = ttk.Frame(self.review_tab)
        form.pack(fill="x", pady=4)
        form.columnconfigure(1, weight=1)
        self.a_name = self._entry(form, "Name", row=0)
        self.a_email = self._entry(form, "Email", row=1)
        self.identity_entries = [child for child in form.winfo_children() if isinstance(child, ttk.Entry)]
        row = ttk.Frame(self.review_tab)
        row.pack(fill="x", pady=6)
        self.delivery_summary = tk.StringVar(value="Delivery: manual website")
        ttk.Label(row, textvariable=self.delivery_summary).pack(side="left")
        self.delivery_button = ttk.Button(row, text="Change delivery", command=lambda: self.action(self._delivery_dialog))
        self.delivery_button.pack(side="right")
        ttk.Label(self.review_tab, text="Message / cover letter").pack(anchor="w")
        self.a_letter = self._text(self.review_tab, 5)
        editing = ttk.Frame(self.review_tab)
        editing.pack(fill="x")
        self.save_application_button = ttk.Button(editing, text="Save", command=lambda: self.action(self._save_application))
        self.save_application_button.pack(side="left")
        self.use_profile_button = ttk.Button(editing, text="Use profile", command=lambda: self.action(lambda: self._save_application(True)))
        self.use_profile_button.pack(side="left", padx=6)
        self.answers_button = ttk.Button(editing, text="Answers", command=lambda: self.action(self._answers_dialog))
        self.answers_button.pack(side="left")
        ttk.Button(editing, text="History", command=lambda: self.action(self._history_dialog)).pack(side="left", padx=6)
        ttk.Button(editing, text="Update stage", command=lambda: self.action(self._stage_dialog)).pack(side="left")
        self.apply_button = ttk.Button(editing, text="Apply", style="Accent.TButton", command=lambda: self.action(self._approve))
        self.apply_button.pack(side="right")
        self.handoff_button = ttk.Button(editing, text="Open website", command=lambda: self.action(self._dispatch))
        self.handoff_button.pack(side="right", padx=6)
        bottom = ttk.Frame(self.review_tab)
        bottom.pack(fill="x", pady=(10, 0))
        self.mark_submitted_button = ttk.Button(bottom, text="Mark submitted", command=lambda: self.action(self._confirm_manual))
        self.mark_submitted_button.pack(side="left")
        self.resolve_button = ttk.Button(bottom, text="Resolve uncertain", command=lambda: self.action(self._reconcile))
        self.resolve_button.pack(side="left", padx=6)
        ttk.Label(bottom, text="Portal status sync unavailable. Stages are user-reported.", style="Muted.TLabel").pack(side="right")

    def _show_applications(self):
        selected = self.selected_application
        self.application_tree.delete(*self.application_tree.get_children())
        for application in self.applications.list():
            job = application["job"]
            tracking = self.tracker.summary(application)
            state = TECHNICAL_LABELS.get(application["state"], application["state"])
            if application["state"] == "submitted" and application["receipt"].startswith("user-"):
                state = "Submitted (user reported)"
            self.application_tree.insert("", "end", iid=application["id"], values=(
                job.title, job.company, tracking["portal"] or job.source, STAGE_LABELS[tracking["stage"]],
                state, hiring_badge(self.tracker.availability(job))))
        if selected and self.application_tree.exists(selected):
            self.application_tree.selection_set(selected)

    def _editor_dirty(self):
        if self.loaded_payload is None:
            return False
        candidate = self.loaded_payload["candidate"]
        return (self.a_name.get() != candidate["name"] or self.a_email.get() != candidate["email"]
                or self.a_letter.get("1.0", "end-1c") != self.loaded_payload["cover_letter"]
                or self.application_answers != self.loaded_payload["answers"])

    def _load_application(self, force=False):
        selected = self.application_tree.selection()
        if not selected:
            return
        if not force and self._editor_dirty():
            if selected[0] != self.selected_application:
                self.application_tree.selection_set(self.selected_application)
                raise ValueError("Save your current draft before switching applications.")
            return
        application = self.applications.get(selected[0])
        self.selected_application = selected[0]
        payload = application["payload"]
        self.loaded_payload = payload
        self.a_name.set(payload["candidate"]["name"])
        self.a_email.set(payload["candidate"]["email"])
        self._replace(self.a_letter, payload["cover_letter"])
        self.application_answers = dict(payload["answers"])
        email = payload.get("delivery", {}).get("method") == "email"
        self.delivery_summary.set("Email to " + payload["delivery"]["recipient"] if email else "Delivery: manual website")
        locked = application["state"] in TERMINAL or ("send" in self.tasks and self.sending_application_id == application["id"])
        self._lock_editor(locked)
        self.handoff_button.configure(text="Send approved email" if email else "Open website",
                                      state="normal" if not locked and application["state"] in {"approved", "manual_handoff"} else "disabled")
        self.mark_submitted_button.configure(state="normal" if application["state"] == "manual_handoff" else "disabled")
        self.resolve_button.configure(state="normal" if application["state"] == "unknown" else "disabled")

    def _lock_editor(self, locked):
        for button in (self.apply_button, self.save_application_button, self.delivery_button,
                       self.use_profile_button, self.answers_button):
            button.configure(state="disabled" if locked else "normal")
        for entry in self.identity_entries:
            entry.configure(state="readonly" if locked else "normal")
        self.a_letter.configure(state="disabled" if locked else "normal")

    def _application_id(self):
        if not self.selected_application:
            raise ValueError("Select an application first.")
        return self.selected_application

    def _save_application(self, refresh_profile=False):
        application_id = self._application_id()
        profile = self.store.profile() if refresh_profile else None
        changed = self.applications.edit(
            application_id, name=profile.name if profile else self.a_name.get(),
            email=profile.email if profile else self.a_email.get(),
            cover_letter=self.a_letter.get("1.0", "end-1c"), answers=self.application_answers,
            profile=profile,
        )
        self._show_applications()
        self._load_application(force=True)
        self.status.set("Saved. Review required again." if changed else "No changes.")
        return changed

    def _answers_dialog(self):
        self._application_id()
        dialog, frame = self._dialog("Application answers", "760x520")
        tree = self._tree(frame, (("question", "Question", 310), ("answer", "Your answer", 370)), height=5)
        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        question = self._entry(form, "Question", row=0)
        answer = self._entry(form, "Answer", row=1)
        answers = dict(self.application_answers)

        def render():
            tree.delete(*tree.get_children())
            for index, (key, value) in enumerate(answers.items()):
                tree.insert("", "end", iid=str(index), values=(key, value))

        def select(_=None):
            if tree.selection():
                values = tree.item(tree.selection()[0], "values")
                question.set(values[0])
                answer.set(values[1])

        def add():
            if not question.get().strip() or not answer.get().strip():
                raise ValueError("Enter both a question and your truthful answer.")
            answers[question.get().strip()] = answer.get().strip()
            render()

        def remove():
            answers.pop(question.get().strip(), None)
            render()

        def save():
            self.application_answers = answers
            self._save_application()
            dialog.destroy()
        tree.bind("<<TreeviewSelect>>", select)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="Add / update", command=lambda: self.action(add)).pack(side="left")
        ttk.Button(actions, text="Remove", command=remove).pack(side="left", padx=8)
        ttk.Button(actions, text="Save answers", style="Accent.TButton", command=lambda: self.action(save)).pack(side="right")
        render()

    def _delivery_dialog(self):
        self._save_application()
        application = self.applications.get(self._application_id())
        current = application["payload"].get("delivery", {})
        dialog, frame = self._dialog("Application delivery", "780x610")
        frame.columnconfigure(1, weight=1)
        method = tk.StringVar(value="email" if current.get("method") == "email" else "manual")
        modes = ttk.Frame(frame)
        modes.grid(row=0, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Radiobutton(modes, text="Manual website", value="manual", variable=method).pack(side="left")
        ttk.Radiobutton(modes, text="Email (only when advertised)", value="email", variable=method).pack(side="left", padx=16)
        recipient = self._entry(frame, "Employer recipient", current.get("recipient", ""), 1)
        subject = self._entry(frame, "Subject", current.get("subject", "Application: " + application["job"].title), 2)
        ttk.Label(frame, text="Employer's email-application instructions").grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 6))
        instructions = tk.Text(frame, height=7, wrap="word", font=("Segoe UI", 10))
        instructions.grid(row=4, column=0, columnspan=2, sticky="nsew")
        instructions.insert("1.0", current.get("instructions", ""))
        frame.rowconfigure(4, weight=1)
        confirmed = tk.BooleanVar(value=current.get("employer_accepts_email", False))
        ttk.Checkbutton(frame, text="This posting invites email applications; all required answers/materials are included.",
                        variable=confirmed).grid(row=5, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Label(frame, text="No guessed recipients or portal bypass. Final Apply shows the exact email before sending.",
                  style="Muted.TLabel", wraplength=720).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)

        def save():
            self.applications.set_delivery(application["id"], recipient=recipient.get(), subject=subject.get(),
                instructions=instructions.get("1.0", "end-1c"), employer_accepts_email=confirmed.get(),
                account=self.mailer.account, email=method.get() == "email")
            self._show_applications()
            self._load_application()
            self.status.set("Delivery saved. Review required before sending.")
            dialog.destroy()
        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Button(actions, text="Mail account", command=self._mail_dialog).pack(side="left")
        ttk.Button(actions, text="Save", style="Accent.TButton", command=lambda: self.action(save)).pack(side="right")

    def _approve(self):
        self._save_application()
        application_id = self._application_id()
        application = self.applications.get(application_id)
        payload = application["payload"]
        reviewed_digest = digest(payload)
        email = payload.get("delivery", {}).get("method") == "email"
        dialog, frame = self._dialog("Review before sending" if email else "Review manual application", "860x670")
        ttk.Label(frame, text="Approval sends this email + resume." if email else "Manual website only. Approval does not submit.",
                  style="Notice.TLabel").pack(anchor="w", pady=(0, 8))
        if email:
            delivery = payload["delivery"]
            account = delivery["account"]
            preview = (f"To: {delivery['recipient']}\nFrom: {payload['candidate']['name']} <{account['sender']}>\n"
                       f"Subject: {delivery['subject']}\nSMTP: {account['host']}:{account['port']} ({account['security']})"
                       f"\nAccount: {account['username']}\n\n" + email_body(payload)
                       + "\nEMPLOYER INSTRUCTIONS\n" + delivery["instructions"])
        else:
            preview = (f"{application['job'].title} | {application['job'].company}\n"
                       f"Candidate: {payload['candidate']['name']} <{payload['candidate']['email']}>\n\n"
                       + email_body(payload))
        preview += (f"\nJOB: {payload['job']['url']}\nRESUME: {payload['resume'].get('filename', 'MISSING')}\n"
                    f"SHA-256: {payload['resume'].get('sha256', 'MISSING')}")
        self._replace(self._text(frame, 18), preview, True)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Resume", command=lambda: self.action(lambda: self._open_resume(payload["resume"]))).pack(side="left")
        ttk.Button(actions, text="Full payload", command=lambda: self._details("Exact local payload", json.dumps(
            payload, indent=2, ensure_ascii=False))).pack(side="left", padx=8)

        def approve():
            self.applications.approve(application_id, reviewed_digest)
            dialog.destroy()
            self._show_applications()
            self._load_application()
            if email:
                self._send_email(application_id)
            else:
                self.status.set("Approved locally. Complete the application on the website.")
        self.approve_send_button = ttk.Button(actions, text="Approve & send" if email else "Approve for manual",
                                             style="Accent.TButton", command=lambda: self.action(approve))
        self.approve_send_button.pack(side="right")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=8)
        dialog.grab_set()
        return dialog

    def _open_resume(self, resume):
        self.store.verify_resume(resume)
        if not webbrowser.open(Path(resume["path"]).resolve().as_uri()):
            raise ValueError("Could not open the local resume. Copy its path from Full payload.")

    def _send_email(self, application_id):
        if "send" in self.tasks:
            raise ValueError("Another email is in progress. Wait for its result.")
        self.sending_application_id = application_id
        self.status.set("Sending the approved email in the background...")
        self._lock_editor(True)
        self.handoff_button.configure(state="disabled")
        self._start_worker("send", lambda: (application_id, *self.applications.dispatch(application_id)))

    def _dispatch(self):
        application_id = self._application_id()
        current = self.applications.get(application_id)
        if current["state"] not in TERMINAL:
            self._save_application()
            current = self.applications.get(application_id)
        if current["payload"].get("delivery", {}).get("method") == "email":
            if current["state"] != "approved":
                raise ValueError("Review and approve this exact email before sending.")
            self._send_email(application_id)
            return
        if current["state"] != "manual_handoff":
            _, message = self.applications.dispatch(application_id)
            self.status.set(message)
        self._show_applications()
        self._load_application()
        self._open_url(current["payload"]["job"]["url"])
        self.status.set("Website opened. Nothing submitted by this app.")

    def _confirm_manual(self):
        application_id = self._application_id()
        if messagebox.askyesno("Record manual submission", "Did you actually submit on the employer website?",
                              parent=self.root):
            self.applications.confirm_manual(application_id)
            self._show_applications()
            self._render_jobs()
            self._load_application()
            self.status.set("Recorded as user-reported submission.")

    def _history_dialog(self):
        application = self.applications.get(self._application_id())
        tracking = self.tracker.summary(application)
        listing = self.tracker.availability(application["job"])
        lines = [f"Send status: {TECHNICAL_LABELS.get(application['state'], application['state'])}",
                 f"Receipt/provenance: {application['receipt'] or 'none'}",
                 f"Stage: {tracking['stage']} | {tracking['provenance']} | Updated: {tracking['updated_at']}",
                 "Automatic employer application-stage sync: unavailable. Sending permission does not grant inbox access.",
                 f"\nListing: {hiring_badge(listing)} | {listing['checked_at'] or 'unknown'}\n"
                 f"{listing['provenance']} | {listing['evidence']}\n{listing['source_url']}\n\nAPPLICATION EVENTS"]
        lines.extend(f"{event['at']} | {event['state']} | {event['message']}" for event in self.applications.events(application["id"]))
        lines.append("\nSTAGE HISTORY")
        lines.extend(f"{event['at']} | {event['stage']} | {event['provenance']} | {event['evidence']}"
                     for event in self.tracker.history(application["id"]))
        return self._details("Application history", "\n".join(lines))

    def _stage_dialog(self):
        application = self.applications.get(self._application_id())
        tracking = self.tracker.summary(application)
        dialog, frame = self._dialog("Update tracking", "740x460")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Application stage (user-reported)").grid(row=0, column=0, sticky="w")
        stage = tk.StringVar(value=tracking["stage"])
        ttk.Combobox(frame, textvariable=stage, values=STAGES, state="readonly").grid(row=0, column=1, sticky="ew")
        when = self._entry(frame, "Applied date", tracking["applied_at"], 1)
        note = self._entry(frame, "Evidence / note", row=2)
        listing = self.tracker.availability(application["job"])
        ttk.Separator(frame).grid(row=3, column=0, columnspan=2, sticky="ew", pady=14)
        ttk.Label(frame, text="Listing availability (separate)").grid(row=4, column=0, sticky="w")
        availability = tk.StringVar(value=listing["status"])
        ttk.Combobox(frame, textvariable=availability, values=("unknown", "open", "active", "closed"),
                     state="readonly").grid(row=4, column=1, sticky="ew")
        listing_evidence = self._entry(frame, "Listing evidence", listing["evidence"], 5)
        ttk.Label(frame, text="Active means an explicit hiring claim, not just a visible/recent listing. "
                  "Stage changes do not withdraw/update anything on the portal.", style="Muted.TLabel",
                  wraplength=680).grid(row=6, column=0, columnspan=2, sticky="w", pady=10)

        def save():
            self.tracker.update_stage(application["id"], stage.get(), applied_at=when.get().strip(), evidence=note.get())
            if availability.get() != listing["status"] or listing_evidence.get() != listing["evidence"]:
                self.tracker.set_availability(application["job"], availability.get(), evidence=listing_evidence.get())
            self._show_applications()
            self._show_jobs()
            self.status.set("Tracking updated (user-reported).")
            dialog.destroy()
        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)
        if application["job"].source == "jobicy":
            ttk.Button(actions, text="Check listing (Jobicy)", command=lambda: self._start_worker(
                "listing", lambda: self.tracker.refresh_availability(application["job"]))).pack(side="left")
        ttk.Button(actions, text="Save", style="Accent.TButton", command=lambda: self.action(save)).pack(side="right")

    def _external_dialog(self):
        dialog, frame = self._dialog("Track an existing application", "760x520")
        frame.columnconfigure(1, weight=1)
        fields = {}
        for row, (key, label, value) in enumerate((
                ("portal", "Portal", ""), ("url", "Original job URL", ""), ("title", "Role", ""),
                ("company", "Company", ""), ("location", "Location", ""),
                ("date", "Applied date (YYYY-MM-DD)", date.today().isoformat()),
                ("note", "Evidence / note", ""))):
            fields[key] = self._entry(frame, label, value, row)
        stage = tk.StringVar(value="submitted")
        ttk.Label(frame, text="Current stage").grid(row=7, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=stage, values=tuple(value for value in STAGES if value not in {"prepared", "discarded"}),
                     state="readonly").grid(row=7, column=1, sticky="ew")
        ttk.Label(frame, text="Record only: no reapplication, account scraping or automatic stage sync.",
                  style="Muted.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=14)

        def save():
            url = public_url(fields["url"].get())
            job = Job("external:" + fields["portal"].get().strip(), url, fields["title"].get().strip(),
                      fields["company"].get().strip(), fields["location"].get().strip(), url)
            application_id, created = self.applications.import_external(
                job, portal=fields["portal"].get(), applied_at=fields["date"].get().strip(),
                stage=stage.get(), evidence=fields["note"].get())
            self._show_applications()
            self.application_tree.selection_set(application_id)
            self._load_application()
            self.status.set("Application recorded (user-reported)." if created else "Linked to existing record; no duplicate application.")
            dialog.destroy()
        ttk.Button(frame, text="Add to tracker", style="Accent.TButton", command=lambda: self.action(save)).grid(
            row=9, column=1, sticky="e")

    def _reconcile(self):
        application_id = self._application_id()
        current = self.applications.get(application_id)
        if current["state"] != "unknown":
            raise ValueError("Select an uncertain send first.")
        submitted = messagebox.askyesnocancel("Verify first", "Has the employer confirmed receipt?\n"
            "Yes: confirmed received. No: confirmed NOT received. Cancel: still uncertain.", parent=self.root)
        if submitted is None:
            return
        evidence = simpledialog.askstring("Verification", "Employer/provider reference or verification details:", parent=self.root)
        if evidence is not None:
            self.applications.reconcile(application_id, submitted=submitted, evidence=evidence)
            self._show_applications()
            self._load_application()


def main(argv=None):
    parser = ArgumentParser(description="Local-first desktop job application assistant")
    parser.add_argument("--smoke-test", action="store_true", help="Open/close the actual UI with temporary data and no network")
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
                             "Do not retry an uncertain send.", parent=root)

    root.report_callback_exception = report_error
    instance_lock = None
    temporary = tempfile.TemporaryDirectory() if options.smoke_test else None
    try:
        store = Store(Path(temporary.name) if temporary else None)
        instance_lock = store.acquire_instance_lock()
        app = Desktop(root, store)
    except (ValueError, OSError, sqlite3.Error) as exc:
        if not options.smoke_test:
            messagebox.showerror("Cannot open local data", str(exc), parent=root)
        else:
            LOG.error("Startup failed: %s", exc)
        root.destroy()
        if instance_lock:
            instance_lock.close()
        if temporary:
            temporary.cleanup()
        raise SystemExit(1) from exc
    try:
        root.deiconify()
        if options.smoke_test:
            root.after(1000, app._shutdown)
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
