from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Job, Salary
from .resume import extract_skills


MAX_FEED_BYTES = 12 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def listing_fields(source: str, published: object = "") -> dict:
    if isinstance(published, (int, float)) and not isinstance(published, bool):
        try:
            published = datetime.fromtimestamp(published, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            published = ""
    checked = utc_now()
    return {
        "fetched_at": checked, "published_at": published if isinstance(published, str) else "",
        "hiring_status": "open", "hiring_evidence": f"Listed in {source}'s public published-jobs feed; not an actively-hiring claim.",
        "hiring_checked_at": checked,
    }


class FeedError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url: str) -> object:
    request = Request(url, headers={
        "User-Agent": "JobApplicationAgent/0.1 (public job-feed desktop client)",
        "Accept": "application/json",
    })
    try:
        with build_opener(NoRedirect).open(request, timeout=25) as response:
            data = response.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise FeedError("Feed exceeds the 12 MB safety limit.")
            return json.loads(data)
    except HTTPError as exc:
        raise FeedError(f"Feed returned HTTP {exc.code}. Access limits are respected; retry later.") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise FeedError(f"Could not read the public feed: {exc}") from exc


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value[:200_000])
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()[:40_000]


def parse_salary(text: str) -> Salary:
    """Only parse explicitly identified currencies and periods, not ambiguous '$'."""
    currencies = re.findall(r"\b(?:USD|EUR|GBP|INR|CAD|AUD)\b|US\$", text, re.I)
    periods = re.findall(r"\b(year|annual|annually|annum|month|monthly|hour|hourly)\b|/\s*([ymh])\b", text, re.I)
    if not currencies or not periods:
        return Salary(evidence=text)
    codes = {"USD" if currency.upper() == "US$" else currency.upper() for currency in currencies}
    units = {(period[0] or period[1]).lower()[0] for period in periods}
    units = {"year" if unit in {"a", "y"} else "month" if unit == "m" else "hour" for unit in units}
    if len(codes) != 1 or len(units) != 1:
        return Salary(evidence=text)
    code, unit = codes.pop(), units.pop()
    values = re.findall(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kK])?(?!\w)", text)
    numbers = [float(number.replace(",", "")) * (1000 if scale else 1) for number, scale in values]
    if not 1 <= len(numbers) <= 2 or any(number <= 0 for number in numbers):
        return Salary(evidence=text)
    low, high = (numbers[0], numbers[1]) if len(numbers) == 2 else (numbers[0], numbers[0])
    if low > high:
        return Salary(evidence=text)
    # "up to" and "from" are one-sided, never guaranteed fixed compensation.
    if len(numbers) == 1 and re.search(r"\b(up to|maximum|under)\b", text, re.I):
        low = None
    elif len(numbers) == 1 and re.search(r"\b(from|minimum|starting)\b", text, re.I):
        high = None
    return Salary(low, high, code, unit, text)


@dataclass(frozen=True)
class Capabilities:
    discovery: str
    application: str = "Portal auto-apply unavailable. Manual website; invited email is a separate, explicitly approved channel."
    automatic_submission: bool = False


@dataclass
class Discovery:
    jobs: list[Job]
    rejected: int = 0


@dataclass(frozen=True)
class SubmissionResult:
    outcome: str  # accepted, sent (mail-server acceptance only), rejected, needs_input, or unknown
    message: str
    receipt: str = ""


class Connector(Protocol):
    id: str
    label: str
    capabilities: Capabilities
    cache_seconds: int

    def discover(self) -> Discovery: ...


class SubmittingConnector(Connector, Protocol):
    def missing_answers(self, payload: dict) -> list[str]: ...

    def submit(self, payload: dict, idempotency_key: str) -> SubmissionResult: ...


class Remotive:
    id = "remotive"
    label = "Remotive"
    cache_seconds = 6 * 60 * 60
    capabilities = Capabilities("Public remote-job API. Up to 4 refreshes/day; listings may be delayed.")

    def discover(self) -> Discovery:
        data = fetch_json("https://remotive.com/api/remote-jobs")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FeedError("Remotive returned an unexpected feed shape.")
        jobs, rejected = [], 0
        for item in data["jobs"]:
            try:
                description = plain_text(item["description"])
                jobs.append(Job(self.id, str(item["id"]), item["title"], item["company_name"],
                                item.get("candidate_required_location") or "Remote", item["url"],
                                description, True, extract_skills(description),
                                parse_salary(item.get("salary") or ""),
                                **listing_fields(self.label, item.get("publication_date", ""))))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)


class Arbeitnow:
    id = "arbeitnow"
    label = "Arbeitnow"
    cache_seconds = 60 * 60
    capabilities = Capabilities("Public job-board API, first response page only (page size controlled by the feed).")

    def discover(self) -> Discovery:
        data = fetch_json("https://www.arbeitnow.com/api/job-board-api")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise FeedError("Arbeitnow returned an unexpected feed shape.")
        jobs, rejected = [], 0
        for item in data["data"]:
            try:
                description = plain_text(item["description"])
                remote = item.get("remote") if isinstance(item.get("remote"), bool) else None
                jobs.append(Job(self.id, str(item["slug"]), item["title"], item["company_name"],
                                item.get("location") or "Unknown", item["url"], description,
                                remote, extract_skills(description),
                                **listing_fields(self.label, item.get("created_at", ""))))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)


class Greenhouse:
    label = "Greenhouse"
    cache_seconds = 60 * 60
    capabilities = Capabilities("Public employer board API; provide the employer's board token. Not a global search.")

    def __init__(self, board: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", board):
            raise ValueError("Greenhouse board tokens may contain only letters, numbers, underscores, and hyphens.")
        self.board = board
        self.id = "greenhouse:" + board
        self.label = "Greenhouse / " + board

    def discover(self) -> Discovery:
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{self.board}/jobs?content=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FeedError("Greenhouse returned an unexpected feed shape.")
        jobs, rejected = [], 0
        for item in data["jobs"]:
            try:
                description = plain_text(item.get("content") or "")
                jobs.append(Job(self.id, str(item["id"]), item["title"], self.board,
                                item.get("location", {}).get("name") or "Unknown", item["absolute_url"],
                                description, None, extract_skills(description),
                                **listing_fields(self.label, item.get("first_published", ""))))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)


class Manual:
    id = "manual"
    label = "Manual / LinkedIn URL import"
    cache_seconds = 0
    capabilities = Capabilities("Paste posting details and a public HTTPS URL. No scraping or LinkedIn API access.")


def structured_salary(low, high, currency, interval, evidence: str) -> Salary:
    periods = {"year": "year", "yearly": "year", "annual": "year", "annually": "year", "per-year": "year",
               "1 year": "year", "month": "month", "monthly": "month", "per-month": "month", "1 month": "month",
               "hour": "hour", "hourly": "hour", "per-hour": "hour", "1 hour": "hour"}
    period = periods.get(str(interval).casefold())
    if not period or not currency:
        return Salary(evidence=evidence)
    try:
        minimum = float(low) if low is not None and low != "" else None
        maximum = float(high) if high is not None and high != "" else None
        if minimum == 0 and maximum == 0:
            return Salary(evidence=evidence)
        salary = Salary(minimum, maximum, str(currency).upper(), period, evidence)
        salary.validate()
        return salary
    except (ValueError, TypeError):
        return Salary(evidence=evidence + " (unparseable structured pay)")


class Lever:
    cache_seconds = 60 * 60
    capabilities = Capabilities("Public employer postings API; configure company site slug and global/EU region. First 100 jobs.")

    def __init__(self, board: str, *, eu=False):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", board):
            raise ValueError("Lever site slugs may contain only letters, numbers, underscores, and hyphens.")
        self.board, self.eu = board, eu
        self.id = ("lever-eu:" if eu else "lever:") + board
        self.label = ("Lever EU / " if eu else "Lever / ") + board

    def discover(self) -> Discovery:
        host = "api.eu.lever.co" if self.eu else "api.lever.co"
        data = fetch_json(f"https://{host}/v0/postings/{self.board}?mode=json&limit=100")
        if not isinstance(data, list):
            raise FeedError("Lever returned an unexpected feed shape.")
        jobs, rejected = [], 0
        for item in data:
            try:
                details = item.get("descriptionPlain") or plain_text(item.get("description") or "")
                lists = "\n".join(section.get("text", "") + "\n" + plain_text(section.get("content", ""))
                                  for section in item.get("lists", []))
                description = (details + "\n" + lists + "\n" + (item.get("additionalPlain") or ""))[:40_000]
                categories = item.get("categories") or {}
                locations = categories.get("allLocations") or [categories.get("location") or "Unknown"]
                location = "; ".join(locations)
                remote = {"remote": True, "on-site": False, "hybrid": False}.get(item.get("workplaceType"))
                pay = item.get("salaryRange") or {}
                salary = structured_salary(pay.get("min"), pay.get("max"), pay.get("currency"),
                                           pay.get("interval"), "Lever structured salary range")
                jobs.append(Job(self.id, str(item["id"]), item["text"], self.board, location,
                                item["hostedUrl"], description, remote, extract_skills(description),
                                salary, **listing_fields(self.label)))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)


class Ashby:
    cache_seconds = 60 * 60
    capabilities = Capabilities("Public employer job-board API; configure board name. Only publicly listed jobs.")

    def __init__(self, board: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", board):
            raise ValueError("Ashby board names may contain only letters, numbers, underscores, and hyphens.")
        self.board = board
        self.id = "ashby:" + board
        self.label = "Ashby / " + board

    def discover(self) -> Discovery:
        data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.board}?includeCompensation=true")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FeedError("Ashby returned an unexpected feed shape.")
        jobs, rejected = [], 0
        for item in data["jobs"]:
            try:
                if item.get("isListed") is False:
                    continue
                description = item.get("descriptionPlain") or plain_text(item.get("descriptionHtml") or "")
                locations = [item.get("location") or "Unknown"]
                locations.extend(place["location"] for place in item.get("secondaryLocations", []) if place.get("location"))
                # Ashby's isRemote also includes hybrid; only workplaceType establishes full remote.
                remote = {"Remote": True, "Hybrid": False, "OnSite": False}.get(item.get("workplaceType"))
                compensation = item.get("compensation") or {}
                components = [component for component in compensation.get("summaryComponents", [])
                              if component.get("compensationType") == "Salary"]
                salary = Salary(evidence=compensation.get("scrapeableCompensationSalarySummary") or "")
                if len(components) == 1:
                    component = components[0]
                    salary = structured_salary(component.get("minValue"), component.get("maxValue"),
                                               component.get("currencyCode"), component.get("interval"),
                                               salary.evidence or "Ashby structured salary summary (multiple zones may apply)")
                jobs.append(Job(self.id, item["jobUrl"], item["title"], self.board, "; ".join(locations),
                                item["jobUrl"], description[:40_000], remote, extract_skills(description),
                                salary, **listing_fields(self.label, item.get("publishedAt", ""))))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)


class Jobicy:
    id = "jobicy"
    label = "Jobicy"
    cache_seconds = 60 * 60
    capabilities = Capabilities("Public remote-jobs API; first 100 recent listings, source links retained. No paid/direct-link API.")

    def discover(self) -> Discovery:
        data = fetch_json("https://jobicy.com/api/jobs?count=100")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or data.get("success") is False:
            raise FeedError("Jobicy returned an unexpected or unsuccessful feed response.")
        jobs, rejected = [], 0
        for item in data["jobs"]:
            try:
                description = plain_text(item.get("jobDescription") or item.get("jobExcerpt") or "")
                salary = structured_salary(item.get("salaryMin"), item.get("salaryMax"), item.get("salaryCurrency"),
                                           item.get("salaryPeriod"), "Jobicy structured salary")
                jobs.append(Job(self.id, str(item["id"]), item["jobTitle"], item["companyName"],
                                item.get("jobGeo") or "Remote", item["url"], description, True,
                                extract_skills(description), salary,
                                **listing_fields(self.label, item.get("pubDate", ""))))
            except (KeyError, ValueError, TypeError, AttributeError):
                rejected += 1
        return Discovery(jobs, rejected)

    def check_status(self, ids: list[str]) -> dict[str, dict]:
        if not ids or len(ids) > 100 or any(not re.fullmatch(r"[1-9]\d*", value) for value in ids):
            raise ValueError("Check between 1 and 100 numeric Jobicy listing IDs.")
        data = fetch_json("https://jobicy.com/api/jobs/status?ids=" + ",".join(dict.fromkeys(ids)))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or data.get("success") is False:
            raise FeedError("Jobicy returned an unexpected listing-status response.")
        checked_at = data.get("checkedAt") or utc_now()
        try:
            datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise FeedError("Jobicy returned an invalid listing check timestamp.") from exc
        results = {}
        for item in data["jobs"]:
            if not isinstance(item, dict):
                raise FeedError("Jobicy returned a malformed listing-status record.")
            key, status = str(item.get("id", "")), item.get("status")
            if key not in ids or status not in {"active", "closed", "unknown"}:
                raise FeedError("Jobicy returned an unrecognized listing-status value.")
            results[key] = {"status": {"active": "open", "closed": "closed", "unknown": "unknown"}[status],
                            "checked_at": checked_at,
                            "evidence": f"Jobicy status endpoint reports {status}. This is listing availability, not application stage."}
        if set(results) != set(ids):
            raise FeedError("Jobicy omitted requested listing statuses; availability is unverified.")
        return results


def connectors(board: str = "", lever: str = "", ashby: str = "", lever_eu: str = "") -> dict[str, Connector]:
    values = [Remotive(), Arbeitnow(), Jobicy()]
    boards = [(Greenhouse, board), (Lever, lever), (Ashby, ashby),
              (lambda token: Lever(token, eu=True), lever_eu)]
    tokens = [(factory, token.strip()) for factory, text in boards for token in text.split(",") if token.strip()]
    if len(tokens) > 10:
        raise ValueError("Configure at most 10 employer boards per refresh across all ATS platforms.")
    for factory, token in tokens:
        values.append(factory(token))
    return {connector.id: connector for connector in values}
