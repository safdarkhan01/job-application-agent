from __future__ import annotations

from dataclasses import dataclass, field
import re

from .models import Job, PERIODS, Profile, normalized
from .resume import contains_term


@dataclass
class Match:
    score: int = 0
    role: str = "unknown"
    location: str = "unknown"
    compensation: str = "unknown"
    reasons: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.role == "match" and self.location not in {"mismatch", "unknown"} and self.compensation in {
            "meets", "not requested"}


def location_matches(preference: str, location: str) -> bool:
    aliases = {"us": "united states", "usa": "united states", "uk": "united kingdom"}
    wanted = aliases.get(normalized(preference), normalized(preference))
    actual = normalized(location)
    actual = re.sub(r"\b(?:usa|us)\b", "united states", actual)
    actual = re.sub(r"\buk\b", "united kingdom", actual)
    return contains_term(actual, wanted)


def rank(job: Job, profile: Profile) -> Match:
    result = Match()
    title = normalized(job.title)
    matches = []
    for desired in profile.titles:
        words = [word for word in normalized(desired).split() if word not in {"and", "of", "the"}]
        if words and all(contains_term(title, word) for word in words):
            matches.append(desired)
    if matches:
        result.role = "match"
        result.score += 40
        result.reasons.append("Role: title matches " + ", ".join(matches) + ".")
    elif profile.titles:
        result.role = "mismatch"
        result.reasons.append("Role: no requested title matches the posting title.")
    else:
        result.reasons.append("Role: add desired job titles to your profile.")
    text = job.title + "\n" + job.description + "\n" + " ".join(job.skills)
    skills = [skill for skill in profile.skills if contains_term(text, skill)]
    if profile.skills:
        result.score += round(30 * len(skills) / len(profile.skills))
        result.reasons.append(f"Skills: {len(skills)}/{len(profile.skills)} mentioned"
                              + (": " + ", ".join(skills) if skills else "") + ". Not proof of qualification.")
    else:
        result.reasons.append("Skills: no profile skills set.")
    if profile.remote == "remote" and job.remote is False:
        result.location = "mismatch"
        result.reasons.append("Location: the source marks this as non-remote.")
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
            result.reasons.append("Location: source explicitly lists worldwide remote; verify legal eligibility.")
        elif normalized(job.location) in {"", "remote"}:
            result.reasons.append("Location: remote does not establish country eligibility; review posting.")
        else:
            result.location = "mismatch"
            result.reasons.append("Location: no requested place appears in '" + job.location + "'.")
    else:
        result.location = "not requested"
        result.reasons.append("Location: no geographic restriction configured.")
    if result.location in {"match", "not requested"}:
        result.score += 15
    salary = job.salary
    if profile.minimum_salary is None:
        result.compensation = "not requested"
        result.reasons.append("Compensation: no minimum configured.")
    elif salary.minimum is None and salary.maximum is None:
        result.reasons.append("Compensation: unknown; not counted as meeting your minimum.")
    elif not salary.currency or salary.currency != profile.currency:
        result.reasons.append("Compensation: currency unknown or different; no FX conversion is performed.")
    else:
        wanted = profile.minimum_salary * PERIODS[profile.period]
        low = salary.minimum * PERIODS[salary.period] if salary.minimum is not None else None
        high = salary.maximum * PERIODS[salary.period] if salary.maximum is not None else None
        if low is not None and low >= wanted:
            result.compensation = "meets"
        elif high is not None and high < wanted:
            result.compensation = "below"
        else:
            result.compensation = "possible"
        result.reasons.append(f"Compensation: {result.compensation}; source evidence: {salary.evidence or 'explicit range'}. "
                              "Annualization uses 12 months / 2,080 hours; no tax or FX adjustment.")
    if result.compensation == "meets":
        result.score += 15
    return result
