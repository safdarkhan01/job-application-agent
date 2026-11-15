from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
import re

from .connectors import Arbeitnow, Jobicy, Remotive, utc_now
from .matching import LEVEL_WORDS, location_matches, seniority
from .models import Job, PERIODS, canonical_url, deduplicate, digest, normalized
from .service import discover
from .storage import Store


@dataclass
class MarketQuery:
    role: str
    location: str
    level: str = ""
    currency: str = "USD"

    def __post_init__(self):
        if not self.level:
            self.level = seniority(self.role)

    @property
    def key(self):
        return "market:" + digest(asdict(self))

    def validate(self):
        if not self.role.strip() or not self.location.strip():
            raise ValueError("Enter a generic role and location for salary research.")
        if len(self.role) > 120 or len(self.location) > 120:
            raise ValueError("Keep market research to a generic role/location, not resume or personal information.")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Choose a three-letter currency for salary research.")


@dataclass
class MarketReport:
    query: dict
    researched_at: str
    samples: list[dict] = field(default_factory=list)
    source_results: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def source_count(self) -> int:
        return len({sample["source"].split(":")[0] for sample in self.samples})

    @property
    def sufficient(self) -> bool:
        samples = self.aggregate_samples
        return len(samples) >= 3 and len({sample["source"].split(":")[0] for sample in samples}) >= 2

    @property
    def aggregate_samples(self) -> list[dict]:
        groups = [[sample for sample in self.samples if sample["basis"] == basis]
                  for basis in ("base", "total", "unspecified")]
        return max(groups, key=len)

    @property
    def summary(self) -> str:
        if not self.sufficient:
            return f"Insufficient comparable evidence: {len(self.samples)} posting(s), {self.source_count} website(s)."
        samples = self.aggregate_samples
        lows = [sample["annual_minimum"] for sample in samples]
        highs = [sample["annual_maximum"] for sample in samples]
        return (f"{self.query['currency']} {median(lows):,.0f}-{median(highs):,.0f}/year: "
                f"median advertised bounds ({samples[0]['basis']} basis), {len(samples)} postings across "
                f"{len({sample['source'].split(':')[0] for sample in samples})} websites. Low confidence.")

    def context(self, job: Job) -> str:
        if not self.sufficient or not comparable_samples([job], MarketQuery(**self.query)):
            return "Market evidence does not establish this employer's offered pay or satisfy a minimum."
        samples = self.aggregate_samples
        if job.salary.basis != samples[0]["basis"]:
            return "Base and total/unspecified compensation are not compared. No equivalent market rate is established."
        floor = job.salary.minimum * PERIODS[job.salary.period]
        reference = median(sample["annual_minimum"] for sample in samples)
        relative = "at/above" if floor >= reference else "below"
        return (f"Advertised lower bound is {relative} this small sample's median lower bound. "
                "Role/location/level and base-vs-total caveats apply; this is context, not a guaranteed market rate.")


def comparable_samples(jobs: list[Job], query: MarketQuery) -> list[dict]:
    words = [word for word in normalized(query.role).split() if word not in LEVEL_WORDS | {"and", "of", "the"}]
    samples, seen = [], set()
    for job in deduplicate(jobs):
        if not words:
            continue
        title_words = set(normalized(job.title).split())
        if sum(word in title_words for word in words) / len(words) < 0.5:
            continue
        if not location_matches(query.location, job.location):
            continue
        posting_level = seniority(job.title)
        if query.level and posting_level and posting_level != query.level:
            continue
        pay = job.salary
        if pay.currency != query.currency or pay.minimum is None or pay.maximum is None or pay.minimum <= 0:
            continue
        if job.hiring_status == "closed":
            continue
        key = canonical_url(job.url)
        if key in seen:
            continue
        seen.add(key)
        samples.append({
            "source": job.source, "url": job.url, "title": job.title, "location": job.location,
            "level": posting_level or "unspecified", "level_comparability": "same explicit level" if posting_level and query.level
            else "level not aligned/verified",
            "currency": pay.currency, "period": pay.period, "minimum": pay.minimum, "maximum": pay.maximum,
            "annual_minimum": pay.minimum * PERIODS[pay.period], "annual_maximum": pay.maximum * PERIODS[pay.period],
            "basis": pay.basis, "salary_evidence": pay.evidence,
            "published_at": job.published_at or "unknown", "retrieved_at": job.fetched_at or "unknown",
            "sample_size": "one advertised posting; employer population/sample size unavailable",
        })
    return samples[:100]


def build_report(store: Store, query: MarketQuery, jobs: list[Job], messages: list[str]) -> MarketReport:
    query.validate()
    report = MarketReport(
        asdict(query), utc_now(), comparable_samples(jobs, query), messages,
        [
            "Observed advertised pay, not an independent salary survey or a guaranteed offer; confidence is low.",
            "Non-random, remote-heavy, small sample. Duplicate postings are collapsed, but cross-site independence is not guaranteed.",
            "Source publication and retrieval dates are recorded separately. Missing dates/seniority remain unspecified.",
            "Aggregates keep currency and base/total/unspecified basis separate. Annualization uses 12 months / 2,080 hours.",
            "City-specific research does not silently substitute national/Worldwide jobs. Missing comparable data stays insufficient.",
            "Glassdoor is not integrated: access was restricted; no page/anti-bot bypass. Jobicy's separate salary benchmark API needs a paid server-side key and is not connected.",
            "Market evidence never fills employer salary fields, satisfies your pay minimum, or invents qualifications.",
        ],
    )
    store.set(query.key, asdict(report))
    return report


def research(store: Store, query: MarketQuery) -> MarketReport:
    query.validate()
    # Only public GET feeds are requested. No profile, CV, identity, or query text is transmitted.
    sources = {source.id: source for source in (Remotive(), Arbeitnow(), Jobicy())}
    result = discover(store, sources)
    return build_report(store, query, result.jobs, result.messages)


def saved_report(store: Store, query: MarketQuery) -> MarketReport | None:
    value = store.get(query.key)
    return MarketReport(**value) if value else None
