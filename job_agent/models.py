from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PERIODS = {"year": 1, "month": 12, "hour": 2080}


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalized(value: str) -> str:
    return " ".join(re.sub(r"[^\w+#.]+", " ", value.casefold()).split())


def public_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTPS posting URL without embedded credentials.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise ValueError("Local/private posting URLs are not supported.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Local/private posting URLs are not supported.")
    if parsed.port not in (None, 443):
        raise ValueError("Only the standard HTTPS port is supported.")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Posting URLs cannot contain control characters.")
    return value


def canonical_url(value: str) -> str:
    parsed = urlsplit(public_url(value))
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "gh_src"}]
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


@dataclass
class Salary:
    minimum: float | None = None
    maximum: float | None = None
    currency: str = ""
    period: str = "year"
    evidence: str = ""

    def validate(self) -> None:
        if self.period not in PERIODS:
            raise ValueError("Compensation period must be year, month, or hour.")
        values = [value for value in (self.minimum, self.maximum) if value is not None]
        if any(isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("Compensation must be a finite non-negative number.")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum compensation cannot exceed maximum.")
        if values and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Specify a three-letter currency such as USD, EUR, or INR.")


@dataclass
class Profile:
    name: str = ""
    email: str = ""
    titles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: str = ""
    locations: list[str] = field(default_factory=list)
    remote: str = "any"
    minimum_salary: float | None = None
    currency: str = "USD"
    period: str = "year"
    resume: dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.remote not in {"any", "remote", "onsite"}:
            raise ValueError("Choose any, remote, or onsite for the work preference.")
        Salary(self.minimum_salary, currency=self.currency, period=self.period).validate()
        if self.email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.email):
            raise ValueError("Enter a valid email address.")


@dataclass
class Job:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    remote: bool | None = None
    skills: list[str] = field(default_factory=list)
    salary: Salary = field(default_factory=Salary)
    sources: list[str] = field(default_factory=list)
    fetched_at: str = ""
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        public_url(self.url)
        if not self.title.strip() or not self.company.strip():
            raise ValueError("A job must include a title and company.")
        self.salary.validate()
        if not self.sources:
            self.sources = [self.source]

    @property
    def id(self) -> str:
        return digest([normalized(self.company), normalized(self.title), normalized(self.location)])

    @property
    def keys(self) -> list[str]:
        return list(dict.fromkeys(["identity:" + self.id, "url:" + canonical_url(self.url),
                                   "source:" + self.source + ":" + self.source_id, *self.aliases]))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        values = dict(data)
        values["salary"] = Salary(**values.get("salary", {}))
        return cls(**values)


def deduplicate(jobs: list[Job]) -> list[Job]:
    result: list[Job] = []
    aliases: dict[str, Job] = {}
    for job in jobs:
        matches = []
        for key in job.keys:
            if key in aliases and all(aliases[key] is not match for match in matches):
                matches.append(aliases[key])
        primary = matches[0] if matches else job
        if not matches:
            result.append(primary)
        combined = [*matches, job]
        all_keys = list(dict.fromkeys(key for member in combined for key in member.keys))
        primary.aliases = all_keys
        primary.sources = sorted({source for member in combined for source in member.sources})
        for redundant in matches[1:]:
            result = [member for member in result if member is not redundant]
        for key in all_keys:
            aliases[key] = primary
    return result
