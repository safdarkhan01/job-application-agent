from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

from .models import Job, PERIODS, Profile, normalized
from .resume import contains_term, extract_skills


LEVELS = ("intern", "entry", "junior", "mid", "senior", "lead", "staff", "principal")
LEVEL_WORDS = set(LEVELS) | {"manager", "director", "sr", "jr", "level"}


def seniority(title: str) -> str:
    for level in ("director", "manager", "principal", "staff", "lead", "senior", "junior", "entry", "intern", "mid"):
        if contains_term(title, level):
            return level
    if re.search(r"\bsr\.?\b", title, re.I):
        return "senior"
    if re.search(r"\bjr\.?\b", title, re.I):
        return "junior"
    return ""


def publication_age(value: str, now: datetime | None = None) -> int | None:
    if not value:
        return None
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        published = published.replace(tzinfo=timezone.utc) if published.tzinfo is None else published
    except (ValueError, TypeError):
        return None
    today = (now or datetime.now(timezone.utc)).date()
    age = (today - published.date()).days
    return age if age >= 0 else None


@dataclass
class Match:
    score: int = 0
    role: str = "unknown"
    location: str = "unknown"
    compensation: str = "unknown"
    skills: str = "unknown"
    skill_ratio: float = 0
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    pay_floor: float | None = None
    hiring: str = "unknown"
    level: str = "unknown"
    age_days: int | None = None
    recent: bool | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return (self.skills == "match" and self.location not in {"mismatch", "unknown"}
                and self.compensation in {"meets", "not requested"} and self.hiring != "closed")

    @property
    def order(self) -> tuple:
        return (self.eligible, self.score, self.skill_ratio,
                self.pay_floor if self.pay_floor is not None else -1,
                {"active": 2, "open": 1, "unknown": 0, "closed": -1}[self.hiring],
                self.level == "preferred", self.recent is True,
                -(self.age_days if self.age_days is not None else 100000) if self.recent is not None else 0)


def location_matches(preference: str, location: str) -> bool:
    aliases = {"us": "united states", "usa": "united states", "uk": "united kingdom"}
    wanted = aliases.get(normalized(preference), normalized(preference))
    actual = normalized(location)
    actual = re.sub(r"\b(?:usa|us)\b", "united states", actual)
    actual = re.sub(r"\buk\b", "united kingdom", actual)
    return bool(wanted) and contains_term(actual, wanted)


def rank(job: Job, profile: Profile, *, hiring: str | None = None, now: datetime | None = None) -> Match:
    result = Match(hiring=hiring or job.hiring_status)
    title = normalized(job.title)
    matches = []
    for desired in profile.titles:
        words = [word for word in normalized(desired).split() if word not in LEVEL_WORDS | {"and", "of", "the"}]
        if words and all(contains_term(title, word) for word in words):
            matches.append(desired)
    if matches:
        result.role = "match"
        result.score += 5
        result.reasons.append("Title: similar to " + ", ".join(matches) + " (secondary signal).")
    elif profile.titles:
        result.role = "mismatch"
        result.reasons.append("Title: differs from your preferred titles; relevant skills/pay can still qualify.")
    else:
        result.reasons.append("Title: no preferred titles configured.")
    text = job.title + "\n" + job.description + "\n" + " ".join(job.skills)
    posting_skills = list(dict.fromkeys([*job.skills, *extract_skills(text)]))
    matching = [skill for skill in profile.skills if contains_term(text, skill)]
    for skill in matching:
        if not any(normalized(skill) == normalized(known) for known in posting_skills):
            posting_skills.append(skill)
    covered = [skill for skill in posting_skills if any(normalized(skill) == normalized(known) for known in profile.skills)]
    result.matched_skills = covered
    result.missing_skills = [skill for skill in posting_skills if skill not in covered]
    if posting_skills and profile.skills:
        result.skill_ratio = len(covered) / len(posting_skills)
        result.skills = "match" if result.skill_ratio * 100 >= profile.minimum_skill_percent else "partial"
        result.score += round(50 * result.skill_ratio)
        result.reasons.append(
            f"Skills: {len(covered)}/{len(posting_skills)} known posting skills overlap "
            f"({result.skill_ratio:.0%}; minimum {profile.minimum_skill_percent}%). "
            + ("Matched: " + ", ".join(covered) + ". " if covered else "")
            + ("Not in your profile: " + ", ".join(result.missing_skills) + ". " if result.missing_skills else "")
            + "Mentions are evidence, not proof of qualifications or mandatory requirements.")
    else:
        result.reasons.append("Skills: insufficient profile/posting skill evidence; review rather than assume a match.")
    if profile.remote == "remote" and job.remote is False:
        result.location = "mismatch"
        result.reasons.append("Location: the source marks this as non-remote/hybrid.")
    elif profile.remote == "onsite" and job.remote is True:
        result.location = "mismatch"
        result.reasons.append("Location: remote job conflicts with your onsite/hybrid preference.")
    elif profile.remote != "any" and job.remote is None:
        result.reasons.append("Location: work arrangement is unknown; review eligibility.")
    elif profile.locations:
        if any(location_matches(place, job.location) for place in profile.locations):
            result.location = "match"
            result.reasons.append("Location: requested place appears in '" + job.location + "'. Verify work authorization.")
        elif normalized(job.location) in {"anywhere", "worldwide", "global"} and job.remote is True:
            result.location = "match"
            result.reasons.append("Location: source lists worldwide remote; verify legal eligibility.")
        elif normalized(job.location) in {"", "remote"}:
            result.reasons.append("Location: remote alone does not establish country eligibility.")
        else:
            result.location = "mismatch"
            result.reasons.append("Location: no requested place appears in '" + job.location + "'.")
    else:
        result.location = "not requested"
        result.reasons.append("Location: no geographic restriction configured.")
    if result.location in {"match", "not requested"}:
        result.score += 15
    salary = job.salary
    if salary.currency == profile.currency and salary.minimum is not None:
        result.pay_floor = salary.minimum * PERIODS[salary.period]
    if profile.minimum_salary is None:
        result.compensation = "not requested"
        if result.pay_floor is not None:
            result.score += 30
        result.reasons.append("Pay: no minimum configured. Unknown pay is not verified compensation.")
    elif salary.minimum is None and salary.maximum is None:
        result.reasons.append("Pay: unknown; market estimates never satisfy your offered-pay minimum.")
    elif not salary.currency or salary.currency != profile.currency:
        result.reasons.append("Pay: currency unknown or different; no FX conversion.")
    else:
        wanted = profile.minimum_salary * PERIODS[profile.period]
        low = salary.minimum * PERIODS[salary.period] if salary.minimum is not None else None
        high = salary.maximum * PERIODS[salary.period] if salary.maximum is not None else None
        if low is not None and low >= wanted:
            result.compensation = "meets"
            result.score += 30
        elif high is not None and high < wanted:
            result.compensation = "below"
        else:
            result.compensation = "possible"
        result.reasons.append(f"Pay: {result.compensation}; advertised evidence: {salary.evidence or 'explicit range'}. "
                              f"Basis: {salary.basis}. Annualization: 12 months / 2,080 hours; no FX/tax adjustment.")
    posting_level = seniority(job.title)
    if profile.current_level and posting_level:
        if posting_level == profile.current_level:
            result.level = "preferred"
        elif profile.include_higher and posting_level in LEVELS and profile.current_level in LEVELS:
            result.level = "preferred" if LEVELS.index(posting_level) > LEVELS.index(profile.current_level) else "other"
        else:
            result.level = "other"
        result.reasons.append(f"Level: posting explicitly says {posting_level}; your stated level is "
                              f"{profile.current_level}. Same/higher preference is a tie-break, not proof of eligibility.")
    else:
        result.reasons.append("Level: not stated or no current level configured; no seniority facts inferred.")
    result.age_days = publication_age(job.published_at, now)
    if result.age_days is not None:
        result.recent = result.age_days <= profile.recent_days if profile.prefer_recent else None
        result.reasons.append(f"Freshness: source publication date is {result.age_days} days old; retrieval date is not used.")
    else:
        result.reasons.append("Freshness: source publication date unknown; not inferred from retrieval time.")
    result.reasons.append("Hiring: " + result.hiring + ". "
                          + ("Closed listings cannot be ready to apply." if result.hiring == "closed" else
                             "An open/recent listing is not an explicit employer actively-hiring claim."))
    return result
