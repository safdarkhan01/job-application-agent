from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys

from .models import Job, Profile, canonical_json
from .resume import MAX_RESUME_BYTES, read_resume


def data_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "JobApplicationAgent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JobApplicationAgent"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "job-application-agent"


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assets = self.directory / "resumes"
        self.assets.mkdir(exist_ok=True, mode=0o700)
        self.database = self.directory / "agent.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY, job TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, approved_digest TEXT, attempt_key TEXT,
                    receipt TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS application_keys (
                    key TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
                    at TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL
                );
            """)
        if os.name != "nt":
            self.database.chmod(0o600)

    def acquire_instance_lock(self):
        """Hold the returned file open for the lifetime of the desktop process."""
        handle = (self.directory / "desktop.lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ValueError("Another Job Application Agent window is already using this local data. Close it first.") from exc
        return handle

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: object):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical_json(value)))

    def profile(self) -> Profile:
        return Profile(**self.get("profile", {}))

    def save_profile(self, profile: Profile):
        profile.validate()
        self.set("profile", asdict(profile))

    def import_resume(self, path: Path):
        if not path.is_file() or path.stat().st_size > MAX_RESUME_BYTES:
            raise ValueError("Choose a résumé no larger than 15 MB.")
        suffix = path.suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md"}:
            raise ValueError("Unsupported résumé format. Choose PDF, DOCX, TXT, or Markdown.")
        with path.open("rb") as stream:
            data = stream.read(MAX_RESUME_BYTES + 1)
        if len(data) > MAX_RESUME_BYTES:
            raise ValueError("Choose a résumé no larger than 15 MB.")
        sha = hashlib.sha256(data).hexdigest()
        target = self.assets / (sha + suffix)
        existed = target.exists()
        if not existed:
            with tempfile.NamedTemporaryFile(dir=self.assets, delete=False) as temporary:
                temporary.write(data)
                temporary_path = Path(temporary.name)
            try:
                temporary_path.replace(target)
            finally:
                temporary_path.unlink(missing_ok=True)
        try:
            parsed = read_resume(target)
        except Exception:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        return parsed, {"path": str(target), "sha256": sha, "filename": path.name}

    def verify_resume(self, resume: dict) -> None:
        path = Path(resume.get("path", ""))
        if not path.resolve().is_relative_to(self.assets.resolve()) or not path.is_file():
            raise ValueError("The approved local résumé is missing. Import it again and prepare a new revision.")
        if path.stat().st_size > MAX_RESUME_BYTES:
            raise ValueError("The stored résumé exceeds the size limit.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != resume.get("sha256"):
            raise ValueError("The local résumé changed. Import it again; approval cannot be reused.")

    def save_jobs(self, jobs: list[Job]):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO jobs VALUES (?,?)",
                           [(job.id, canonical_json(job.to_dict())) for job in jobs])

    def jobs(self) -> list[Job]:
        with self.connect() as db:
            return [Job.from_dict(json.loads(row["data"])) for row in db.execute("SELECT data FROM jobs")]

    def cache(self, source: str) -> tuple[float, list[Job]] | None:
        value = self.get("feed:" + source)
        if not value:
            return None
        return value["at"], [Job.from_dict(job) for job in value["jobs"]]

    def cache_jobs(self, source: str, jobs: list[Job]):
        self.set("feed:" + source, {"at": datetime.now(timezone.utc).timestamp(),
                                  "jobs": [job.to_dict() for job in jobs]})
