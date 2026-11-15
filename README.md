# Job Application Agent

A local-first desktop assistant for finding jobs using **your uploaded resume**, location, titles, skills, and compensation preferences. Import a resume, discover real postings, inspect match evidence, and prepare and approve a concrete application.

**This is a working initial desktop app, not an unattended browser bot. No built-in connector automatically submits applications.** Current sources provide discovery but not authorized candidate submission access. Approved applications go to an explicit manual browser handoff; the app never calls that a submission unless you confirm completing it yourself.

## Run on your computer

Requires Python 3.11+ with Tkinter (included in standard python.org Windows/macOS installers). On Debian/Ubuntu, install the matching `python3-tk` OS package first. No accounts, cloud setup, or AI API keys are needed.

Windows PowerShell, from the repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m job_agent
```

macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m job_agent
```

The installed `job-application-agent` command is another entry point. For development without personal information, import `tests/fixtures/synthetic_resume.txt`; all of its contents are fabricated.

## Use

1. **Resume & preferences:** import PDF, DOCX, TXT, or Markdown. Review editable keyword-derived titles, skills, email, and experience evidence. Enter your name, desired titles, locations, remote/onsite preference, and optional minimum compensation with currency and period. Save. No facts, years of experience, or employers are invented.
2. **Discover & match:** select public sources and refresh. **Configure Greenhouse / Lever / Ashby boards** accepts comma-separated employer board identifiers (up to 10 total, including Lever global/EU). **Portal catalog & capabilities** separates working discovery, configured employer boards, and manual-only portals, with official reference links. Public-feed requests reveal your IP/User-Agent, but contain no resume, email, or profile fields. Select a posting for source attribution, original URL, retrieval time, salary evidence, and individual match reasons.
3. **Review & applications:** prepare a posting, edit the candidate name/email, cover letter, and question answers, then inspect the full application JSON and local resume attachment. **Review & approve** opens the exact payload for explicit consent. Editing it, changing answers, or refreshing its profile/resume invalidates approval.
4. **Continue approved application:** current connectors open the original posting with an actionable **manual handoff**, not a claimed submission. Attach the displayed local resume, copy reviewed content, complete mandatory questions and any login/CAPTCHA yourself. Use **I completed the manual application** only after actually submitting. The audit trail distinguishes this user report from an API-confirmed receipt.

Existing applications retain their own profile and content-addressed resume snapshot. A later profile edit does not silently change approved applications; use **Use current profile / resume** to explicitly replace that snapshot and invalidate approval. This refresh uses the last saved profile.

PDFs must contain extractable text. Scanned/empty PDFs require external OCR; password-protected PDFs, legacy DOC, binary text, malformed documents, files over 15 MB, and PDFs over 100 pages produce explicit errors. There is no OCR or LLM interpretation. The small built-in title/skill vocabulary is intentionally conservative: manually add missing skills and desired titles.

## Supported sources and honest limitations

| Source | Discovery | Application |
| --- | --- | --- |
| [Remotive](https://remotive.com/) | Public remote-jobs API; original Remotive links and attribution preserved; 6-hour cache, listings delayed by the source | Manual handoff |
| [Arbeitnow](https://www.arbeitnow.com/) | Public job-board API, first page only (feed-controlled page size), 1-hour cache | Manual handoff |
| [Jobicy](https://jobicy.com/jobs-rss-feed) | Public API, first 100 recent remote jobs; Jobicy attribution/links retained, 1-hour cache; no paid API or credentials | Manual handoff |
| Greenhouse | Public employer board by token (e.g. the board segment in an employer's Greenhouse URL); 1-hour cache; board token shown as company label | Manual handoff; public discovery does not grant application API access |
| Lever | Public employer postings API; configure site slug(s), global or EU; first 100 postings per board; 1-hour cache; board slug shown as company label | Manual handoff; application POST requires an employer-generated API key, not implemented |
| Ashby | Public employer board API; configure board name(s); listed jobs only; 1-hour cache; board name shown as company label | Manual handoff |
| LinkedIn / other posting URLs | Paste the HTTPS URL, job details, description, and optional explicit salary in **Import posting / LinkedIn URL**; no page fetch | Manual handoff; no LinkedIn API, profile access, logged-in scraping, autofill, or automatic application |

This does not search every job site. **LinkedIn, Indeed, Naukri, Glassdoor, and Wellfound are not API-integrated.** They are separately labeled manual-only in the portal catalog, not presented as working search integrations. No credentials, partner agreements, or paid aggregator accounts are configured. No authentication or access-control bypass is attempted. API limits and failure cooldowns persist across restarts. Failed sources show explicit errors and use stale cached listings when available. Saved jobs remain locally visible across refreshes and source deselection: check retrieval time and original posting for availability. Feed data and user-entered posting facts are not independently verified.

Integration references assessed during implementation:

| Platform | Official/public reference and access boundary |
| --- | --- |
| Remotive | [API documentation](https://github.com/remotive-com/remote-jobs-api): preserve source links/attribution, respect cache limits; do not syndicate into third-party job boards |
| Arbeitnow | [Public job-board API](https://www.arbeitnow.com/blog/job-board-api) |
| Jobicy | [API documentation](https://jobicy.com/jobs-rss-feed): public listing links need no key; paid direct ATS links are a different optional product, not used here |
| Greenhouse | [Job Board API](https://developers.greenhouse.io/job-board.html): public board discovery does not confer application POST authorization |
| Lever | [Official Postings API](https://github.com/lever/postings-api): global/EU public discovery; POST requires a Super Admin-generated key and custom required questions are not exposed |
| Ashby | [Public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api): `isRemote` can include hybrid; this app uses `workplaceType` and excludes explicitly unlisted jobs |
| LinkedIn | [Talent Solutions](https://learn.microsoft.com/en-us/linkedin/talent/): ATS/customer recruiting integrations, not an authorized personal profile/job-search connection for this app |
| Indeed | [Partner documentation](https://docs.indeed.com/): HTTP 403 during verification; no authorized connection configured |
| Naukri | [Terms](https://www.naukri.com/termsconditions): verification timed out; no public integration established |
| Glassdoor | [Terms](https://www.glassdoor.com/about/terms/): HTTP 403 during verification; no public integration established |
| Wellfound | [Terms](https://wellfound.com/terms): account-based candidate/employer service; no authorized discovery/submission connection established |

These are implementation capabilities, not a claim that restricted portals have no possible commercial partner integration. Terms and access can change. Resume files stored within a LinkedIn account cannot be retrieved by this app: download your own resume from your account and import it locally. Testing manual LinkedIn URLs with synthetic input does **not** establish actual LinkedIn account/profile integration.

### Matching

- Score is a heuristic, **not** a probability of employment: title-token match 40 points, proportion of profile skills mentioned 30, location/work-arrangement match 15, and known compensation meeting the minimum 15. Mismatches remain visible and are labeled outside preferences.
- All requested title tokens must occur in the posting title. Skill matches use word boundaries against description/title/tags. Experience evidence is editable but is not converted into invented seniority or used to assert eligibility.
- Locations use boundary-aware text/limited country aliases, not geocoding. A generic "remote" is **not** proof that a job accepts applicants from your country. Work authorization and onsite/hybrid details need review.
- Unknown salary never counts as meeting a minimum. An overlapping range is only **possible**, not **meets**. Currencies must match exactly; no FX conversion. Hour/month comparisons use 2,080 hours/12 months per year, not tax or benefits adjustments.
- Only explicit currency and period strings are parsed from Remotive salary text. Ambiguous `$` or free-form pay stays unknown. Lever, Ashby and Jobicy use explicit structured salary data when unambiguous (base salary, not equity/bonuses). Greenhouse/Arbeitnow pay is unknown unless supplied through a manual import.
- Discovery deduplicates normalized company/title/location, canonical posting URL, and source ID, retaining source aliases. Identical company/title/location postings are conservatively grouped, even if they could represent distinct requisitions. Different locations with distinct source IDs/URLs remain separate.

## Privacy and durable state

All profile, resume, saved postings, application snapshots, cached feeds, and audit events live in:

| OS | Directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\JobApplicationAgent` |
| macOS | `~/Library/Application Support/JobApplicationAgent` |
| Linux | `$XDG_DATA_HOME/job-application-agent` or `~/.local/share/job-application-agent` |

There is **no external AI, telemetry, credential storage, or automatic profile upload**. Local data is **not encrypted**; use OS account security/full-disk encryption on shared devices. The app copies imported resumes to immutable hash-named local assets; it does not modify originals. Old attachments remain available for application snapshots. Close the app and delete this app-specific data directory to remove all local data (uninstalling an executable alone does not remove it). Backups contain personal information.

SQLite transactions record approval hashes and state changes durably. A second desktop instance is refused for the same data directory. Duplicate applications are prevented across source IDs, normalized identity, and canonical URLs. Approval covers exact candidate data, answers, letter, target job, and resume SHA-256; attachment integrity is checked again before proceeding. Submitted/uncertain applications are locked against editing/retry.

The submission boundary supports future **authorized** adapters, but none is enabled in this release. An adapter must declare its capability, validate mandatory answers, accept the durable idempotency key, and return a confirmed provider receipt. Exceptions, timeouts, missing receipts, or a restart during an in-flight attempt become **unknown**, never automatically retried. **Resolve uncertain outcome** requires an explicit employer-verified outcome/reference; a confirmed rejection requires fresh approval before another attempt. Offline tests exercise this contract with a synthetic adapter that never uses the network.

Manual handoffs cannot guarantee which content a user ultimately submits on another website. The app records the approved intended payload and the user's completion report, not independent verification of external browser actions.

## Standalone Windows download/build

A portable Windows executable bundles Python, Tk, PDF support, and the app. End users do not need Python. The initial packaging target is **Windows x64**, with source execution available on macOS/Linux.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[packaging]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\windows.spec
.\dist\JobApplicationAgent.exe
```

Builds are platform-specific; build on Windows for Windows. The executable is **unsigned**: Windows SmartScreen/antivirus warnings may appear, and first launch unpacks bundled files into a temporary directory. There is no installer, code-signing certificate, auto-updater, notarized macOS bundle, or Linux package yet.

`JobApplicationAgent.exe --smoke-test` opens the actual packaged UI with isolated temporary data, closes after one second, and returns a failing exit code on a startup/callback error. It makes no network requests and never uses your normal app data.

**Validation limitation on the development machine:** the standalone executable builds, but Windows Application Control blocks loading its extracted `python313.dll` before Python starts. Packaged runtime behavior could not be verified on that machine; the unfrozen application and actual Tk widget workflows run successfully. No policy bypass is attempted. Managed devices may require administrator-approved signing/deployment. The CI packaged smoke test must pass before automated release publication. The build spec bundles Python and pypdf license notices; Tcl/Tk notices are included with the Tk runtime.

The `Desktop checks and Windows package` GitHub Actions workflow runs offline tests (including actual Tk widgets), compiles modules, builds the executable, and uploads a portable ZIP plus SHA-256 checksum. A maintainer can run it manually from Actions, download the build artifact, or publish a `v*` tag to create a GitHub Release. **A downloadable public release exists only after a successful workflow/tag publication; committing source alone does not publish binaries.** Do not include your app data directory in a release.

## Development and verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q job_agent tests run_desktop.py
```

Tests use temporary databases and synthetic text/DOCX/PDF fixtures. They cover real desktop widget flow, imports, ranking, explicit unknown compensation, country/skill boundaries, deduplication, persistence, approval invalidation, mandatory answers, duplicate dispatch, timeout ambiguity, and crash recovery. No real applications are submitted. Linux GUI smoke tests need a display (`xvfb-run` is suitable).

Optional live smoke check (GET requests to public feeds and example employer boards, not LinkedIn; respect source request limits):

```powershell
$env:JOB_AGENT_LIVE_FEEDS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_live_feeds -v
Remove-Item Env:\JOB_AGENT_LIVE_FEEDS
```

### Extension boundary

`job_agent/connectors.py` defines discovery capabilities, normalized `Job`/`Salary` results, and the future `SubmittingConnector` protocol. Register permitted connectors with `connectors()` and preserve their rate limits/attribution. Do not mark submission supported simply because a public jobs API exists. Credentials, consent for transmitting resumes, dynamic employer-required questions, provider-specific idempotency/reconciliation, and access agreements must be implemented and reviewed before shipping any real submission adapter.

Core logic has no GUI dependency: `resume.py` parses local files, `matching.py` explains scores, `storage.py` owns SQLite/assets, `service.py` owns feed caching and the approval/submission state machine, and `desktop.py` is the Tkinter UI.

MIT licensed. Source feed content remains subject to each source's terms.
