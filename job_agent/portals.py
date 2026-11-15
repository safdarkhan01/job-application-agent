from dataclasses import dataclass


@dataclass(frozen=True)
class Portal:
    name: str
    access: str
    setup: str
    evidence_url: str


PORTALS = (
    Portal("Remotive", "Working public discovery", "No credentials; 6-hour cache, remote jobs, original links.",
           "https://github.com/remotive-com/remote-jobs-api"),
    Portal("Arbeitnow", "Working public discovery", "No credentials; first feed page, 1-hour cache.",
           "https://www.arbeitnow.com/blog/job-board-api"),
    Portal("Jobicy", "Working public discovery", "No credentials; first 100 recent remote jobs. Public listing links, not paid ATS links.",
           "https://jobicy.com/jobs-rss-feed"),
    Portal("Greenhouse", "Working configurable discovery", "Add employer board token(s). Not a global job search; submission needs employer authorization.",
           "https://developers.greenhouse.io/job-board.html"),
    Portal("Lever", "Working configurable discovery", "Add company site slug(s); global and EU supported. Employer API key is required for application POST; not implemented.",
           "https://github.com/lever/postings-api"),
    Portal("Ashby", "Working configurable discovery", "Add employer board name(s). Public listed jobs only; submission not integrated.",
           "https://developers.ashbyhq.com/docs/public-job-posting-api"),
    Portal("LinkedIn", "Manual only / partner access", "Paste posting details and URL. No profile/resume access, OAuth connection, scraping or automatic application. Talent APIs require approved integration access.",
           "https://learn.microsoft.com/en-us/linkedin/talent/"),
    Portal("Indeed", "Manual only / not connected", "Official partner documentation is separate from public job search. No authorized API configured; documentation access could not be verified here (HTTP 403).",
           "https://docs.indeed.com/"),
    Portal("Naukri", "Manual only / not connected", "No verified public discovery integration in this app; terms page could not be retrieved during implementation. Paste details/open in your browser.",
           "https://www.naukri.com/termsconditions"),
    Portal("Glassdoor", "Manual only / not connected", "No verified public discovery integration in this app; terms page returned HTTP 403. Paste details/open in your browser.",
           "https://www.glassdoor.com/about/terms/"),
    Portal("Wellfound", "Manual only / not connected", "Account-based candidate/employer service. No authorized public discovery/submission API configured; paste details/open in your browser.",
           "https://wellfound.com/terms"),
)
