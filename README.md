# Job Application Agent

A local-first desktop assistant: **CV -> Connections -> Jobs -> Apply or discard -> Tracker**.

**Implemented:** local CV/profile import, six public discovery integrations, background matching/draft preparation, salary evidence, hiring badges, a cross-portal tracker, and **after-approval email sending for employers explicitly inviting email applications**.

**Still unavailable:** signed-in LinkedIn/Indeed/Naukri/Glassdoor/Wellfound accounts, automatic portal-form applications, inbox access, and automatic employer application-stage synchronization. Email delivery is a separate channel, not a way to bypass a portal's requirements. The full requested signed-in, cross-portal auto-apply workflow is **not complete**.

## Run

Python 3.11+ with Tkinter is required (included with standard python.org Windows/macOS installers). On Debian/Ubuntu, install the matching `python3-tk` OS package first.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m job_agent
```

On macOS/Linux, use `python3 -m venv .venv`, `.venv/bin/python -m pip install -e .`, then `.venv/bin/python -m job_agent`. The installed `job-application-agent` command is another entry point. `tests/fixtures/synthetic_resume.txt` is a fabricated development CV; no real user data is included.

## Four short steps

1. **Profile:** import PDF/DOCX/TXT/Markdown, review skills/experience, and set location, work style, minimum pay, currency and period. **Matching preferences** adds skill overlap, your stated level, same/higher opportunities, and recent-posting preferences. **Next: Connections** saves the profile.
2. **Connections:** choose public discovery feeds or **Employer boards**. These do not sign in to a candidate account. Restricted portals are explicitly unavailable. An optional **Mail account** configures your own SMTP sender for the session. **Find matches** runs in the background; hourly refresh is opt-in while the app is open.
3. **Jobs:** recommendations prioritize skills and employer-advertised pay, not title prestige. Open **Details** for evidence and source links; **Pay research** retrieves actual public data. Matching jobs receive local drafts (up to 20 new drafts per refresh). **Review & apply** opens the draft; **Discard** never contacts an employer.
4. **Applications:** review the message, **Answers**, resume, and **Change delivery**. **Apply** opens the final concrete review. For email, **Approve & send** starts the send without another intervention. For unsupported forms, approval is local only; **Open website** is manual. **History** and **Update stage** keep progress in one place. **Add past application** records applications made outside this agent.

Help, source limitations, matching reasons, refresh logs, CV preview, salary sources, full JSON and audit history are on demand rather than permanent screen text. Errors and capability states remain visible. Background work never replaces existing approved payloads or unsaved application-editor content.

### CV handling

Extraction is local, conservative keyword matching, not AI verification. Name, seniority, employers, years of experience and required answers are not invented. Edit missing/unrecognized skills and titles yourself. The short default application message makes no claims about qualifications.

Files must be <=15 MB. PDFs need extractable text and <=100 pages; scanned/empty PDFs require external OCR. Password-protected PDFs, legacy DOC, binary/unsupported text and malformed files fail explicitly. Imported CVs are copied to hash-named local assets; existing applications keep their own snapshot. **Use profile** explicitly refreshes a draft's saved profile/CV and clears approval.

## Discovery and account access

| Source | Actual connection | Limits |
| --- | --- | --- |
| [Remotive](https://remotive.com/) | Public remote-jobs API; no applicant account | 6-hour cache, source-delayed listings, original Remotive attribution/links |
| [Arbeitnow](https://www.arbeitnow.com/) | Public job-board API; no applicant account | First response page only (feed-controlled size); 1-hour cache |
| [Jobicy](https://jobicy.com/jobs-rss-feed) | Public Jobs API; no applicant account | First 100 recent jobs, source links retained; 1-hour cache; no paid links/key |
| Greenhouse | Public employer board by token | Employer-specific, not global search; 1-hour cache |
| Lever | Public employer postings; global/EU site slugs | First 100 jobs per board; 1-hour cache |
| Ashby | Public employer board by name | Explicitly unlisted jobs excluded; 1-hour cache |
| LinkedIn, Indeed, Naukri, Glassdoor, Wellfound | Manual posting import only | No account sign-in, profile/resume extraction, scraping, autofill or automatic portal application |

**Employer boards** accepts comma-separated identifiers, not full URLs (10 boards total). For `https://jobs.lever.co/COMPANY`, enter `COMPANY` in Lever; for `https://jobs.ashbyhq.com/BOARD`, enter `BOARD` in Ashby. Greenhouse uses the employer's public board token. These identifiers are not employer API secrets. Greenhouse/Lever/Ashby board slugs are shown as company labels when the feed supplies no company display name.

Requests respect persistent cache/failure cooldowns and do not send your CV/profile. Failed feeds show an error and may use explicitly stale cached jobs. Saved jobs remain visible after refresh/deselection; removal from search does not prove closure. Deduplication groups source IDs, canonical URLs and normalized company/title/location, retaining aliases. Identical role/location requisitions may be conservatively grouped.

## Real automatic sending: email only

This is a normal, single-recipient SMTP mail client for an **explicit email-application invitation**. It does not automate a portal browser or infer that a contact/privacy/support email accepts applications.

1. In **Connections -> Mail account**, provide your own provider's SMTP hostname, username, sender and provider-approved SMTP/app password. TLS is mandatory: SSL/465 or STARTTLS/587. Passwords are memory-only and must be re-entered after restart. Server/username/sender settings are saved locally. Saving settings is **not** a verified login; authentication happens on send.
2. In an application, choose **Change delivery -> Email**, enter the advertised recipient and subject, paste the employer's email-application instructions including that address, and confirm the posting permits it and all required materials/answers are included. No recipients or answers are guessed. Only the single reviewed resume attachment is supported; if other required files/steps cannot be provided, use the employer's manual process instead.
3. **Apply** shows the actual recipient, sender, SMTP account/target, subject, message/answers, job URL, attachment filename and SHA-256. **Approve & send** transmits exactly that content. Candidate email must match the sender. Changing recipient, sender/account settings, message, answers, job payload or CV requires renewed review; a changed mail account blocks an old snapshot until delivery is refreshed.

The message, answers and resume leave the device **only on that send approval**, via your selected provider to the reviewed employer recipient. SMTP settings/provider policy may block login. For Gmail, see [app passwords](https://support.google.com/accounts/answer/185833) and Google's [SMTP setup](https://support.google.com/a/answer/176600); 2-Step Verification/provider policy applies, and not all accounts support this route. OAuth, corporate SMTP policy setup and inbox authorization are not implemented. Do not enter credentials into issues, chats, the public repository, or fixtures.

Each attempt is durably recorded before network use. **Email sent** means the outgoing server replied SMTP 250 after the message body, **not** employer delivery, receipt, a portal submission, or progression/rejection. The Message-ID and acceptance provenance are stored. Authentication/TLS failures before sending become **Needs attention**; explicit refusals are not sent. A timeout/disconnect during DATA, unexpected acknowledgement, or restart during an attempt becomes **Uncertain** and is locked against blind retry. SMTP itself is not idempotent: the durable local gate prevents accidental app retries, not every possible duplicate outside the app. **Resolve uncertain** requires employer/provider verification and fresh approval before any permitted retry.

No live email/application was sent during development. Email MIME, TLS, authentication failure, timeout, exact-content approval, concurrent send protection and actual Tk final-approval flow are tested with a synthetic SMTP transport. Real provider sign-in and end-to-end employer delivery remain unverified until an individual configures and approves an actual use.

### Why portal auto-apply is still blocked

Public jobs pages and a user's job-seeker login do not grant employer integration privileges. Official documentation checked during implementation:

| Platform | Documented submission access |
| --- | --- |
| [Greenhouse](https://docs.greenhouse.io/job-board.html#submit-an-application) | Secret employer Job Board API key via Basic Auth; application requests must be handled server-side |
| [Lever](https://github.com/lever/postings-api#apply-to-a-job-posting) | Key generated by an employer-account Super Admin; employer-required fields/custom-question limitations |
| [Ashby](https://developers.ashbyhq.com/reference/applicationformsubmit) | Organization API key with `candidatesWrite` and [Basic Auth](https://developers.ashbyhq.com/reference/authentication) |
| [SmartRecruiters](https://developers.smartrecruiters.com/docs/post-an-application) | `X-SmartToken`; [customer/partner credentials](https://developers.smartrecruiters.com/docs/authentication), not a public applicant token |
| [Recruitee](https://docs.recruitee.com/reference/authentication-1.md) | Company-account Careers API token, including candidate POST; documented mandatory token enforcement by 10 February 2027, not permission to exploit a legacy unauthenticated route |
| [LinkedIn](https://learn.microsoft.com/en-us/linkedin/talent/) | Approved Talent/ATS integrations, not an authorized personal-profile/job-search connection for this app |
| Remotive / Arbeitnow / Jobicy | Discovery/links/status APIs; no applicant submission endpoint documented in the reviewed public contracts |

No employer keys, partner agreements or permitted browser-form automation connection exist here. No undocumented/private form APIs, access-control/CAPTCHA bypass, restricted logged-in scraping or fake connected states are used. The generic `SubmittingConnector` test interface remains scaffolding, **not** another working apply feature. An actual supported platform authorization/permission is needed before implementing its signed-in route; email does not close that gap.

## Matching, freshness and hiring

The fit score is a heuristic, not a hiring probability: **known posting-skill overlap 50, advertised pay 30, location/work style 15, title 5**. A different title or promotion is not a hard exclusion. Poor skill overlap cannot become a recommendation just because pay/title is high.

Recommended jobs need sufficient known skill evidence (default 50% of recognized posting skills, editable), matching/unrestricted geography/work style, no known closure, and advertised compensation meeting a configured minimum. This measures skill mentions, **not verified mandatory requirements**. No recognized skill evidence means review. Required qualifications/work authorization/answers must still be checked by the user.

Unknown pay, a different/unknown currency or a range merely overlapping the minimum is **Needs review**, never a verified pay match. No FX conversion. Month/hour comparisons annualize using 12 months / 2,080 hours; base/total/unspecified basis remains visible. Remotive ambiguous `$` pay stays unknown; structured source fields are used when unambiguous. Greenhouse/Arbeitnow pay is not guessed.

Higher advertised lower bounds and explicit hiring evidence help ordering; same/higher stated seniority and real source publication dates are secondary preferences. Unspecified seniority stays unspecified, and no eligibility is inferred. Missing publication dates stay unknown: retrieval/last-updated dates are not substituted.

Every job row includes a hiring badge, with evidence, provenance, source URL and last-checked time in **Details**:

| Badge | Meaning |
| --- | --- |
| Active claim | Explicit employer/source actively-hiring claim; manual claims are labeled `(you)` |
| Open | A published/open listing, **not** an actively-hiring claim |
| Unverified | No reliable evidence, failed check, or open/active evidence older than 48 hours |
| Closed | Explicit closure evidence; not ready to apply |

Recent/visible does not imply active hiring. Jobicy's documented `active` listing status is normalized to **Open**, not an employer actively-hiring claim. Its [batch status endpoint](https://jobicy.com/jobs-rss-feed#japi-status-check) is supported for a selected Jobicy listing with a 60-second cooldown and is rechecked before an email send; unknown/failed/closed checks pause that send. Other sources do not have a supported live availability check here. A failed check never proves closure; a previously confirmed closure stays blocked until explicitly revalidated.

## Salary research

**Pay research** retrieves real public Remotive, Arbeitnow and Jobicy feeds and filters them locally for a **generic role, location, level and currency**. Background discovery also builds a report from its selected sources, without contacting unselected feeds. No name, email, CV, credentials or profile contents are sent in queries.

This is **observed advertised compensation**, not a Glassdoor survey or an independent population benchmark. Each sample records the posting/source URL, publication and retrieval dates, role/location, explicit/unknown seniority, currency, original period/range, compensation basis, and sample/comparability caveats. Duplicate postings are collapsed; independent underlying data across websites cannot be guaranteed.

A summary needs >=3 comparable postings across >=2 websites with a common currency and base/total/unspecified basis. It reports **median advertised lower/upper bounds**, not a guaranteed salary; confidence remains low due to a small, non-random, remote-heavy sample. Insufficient sources, missing salaries, location mismatch and unavailable sources stay explicit. A city query does not silently substitute national/Worldwide jobs. Unknown level/basis is recorded, not invented. Different bases and currencies are not pooled; no FX conversion.

Saved market context can explain how an advertised lower bound compares with that sample. It **never fills employer compensation, satisfies a minimum, invents qualifications, or guarantees the best opportunity**.

Glassdoor access was restricted and is not integrated; the BLS page checked was also inaccessible (HTTP 403). [Jobicy's separate salary benchmark API](https://jobicy.com/salary-api) requires a paid server-side key and is not connected. No blocked page is scraped or fabricated into the report. Source-feed references: [Remotive contract/attribution](https://github.com/remotive-com/remote-jobs-api), [Arbeitnow API](https://www.arbeitnow.com/blog/job-board-api), [Jobicy public API](https://jobicy.com/jobs-rss-feed). Cached results/failure cooldowns apply to research too.

## Unified application tracker

**Add past application** records a portal, original job URL, company/role/location, actual application date and current stage for jobs applied to outside this agent. URL/source/business-identity aliases conservatively link duplicates to an existing record rather than applying again. The import is user-reported, needs no CV/account access, and never submits anything.

**Update stage** supports prepared, sent-unconfirmed, submitted, screening, interview, offer, rejected, withdrawn, closed and discarded, with dated evidence/history. Manual entries are explicitly user-reported. Marking withdrawn/closed records your report; it does not change anything on a portal. Sender acceptance is `sent_unconfirmed`, not employer-confirmed submission. Already-progressed tracking records cannot accidentally be approved/sent again.

Three different facts remain separate:

| Fact | Examples and provenance |
| --- | --- |
| Technical send state | Draft, approved/not sent, sending, manual/not sent, email sent/unverified, user-reported submitted, needs attention, refused, uncertain |
| Recruitment stage | Screening/interview/offer/rejected/withdrawn/closed; user-reported unless a genuine provider receipt establishes the initial submission |
| Listing availability | Open/closed/unknown/explicit active claim, with separate source/evidence/check time |

No current connector can automatically synchronize a candidate's employer stage across portals. The UI says **Portal status sync unavailable**. Sending authorization does not imply inbox access. No reply, a vanished listing, a failed fetch, SMTP acceptance or a listing closure is interpreted as application rejection or employer receipt. Old records and immutable approved payloads are preserved when local tables are added.

## Privacy and durable state

| OS | Local app data |
| --- | --- |
| Windows | `%LOCALAPPDATA%\JobApplicationAgent` |
| macOS | `~/Library/Application Support/JobApplicationAgent` |
| Linux | `$XDG_DATA_HOME/job-application-agent` or `~/.local/share/job-application-agent` |

Profiles, CV copies, saved jobs, generic salary evidence, snapshots and audit events stay in that directory. There is no external AI or telemetry. CV/profile data leaves only in the concrete user-approved email; opening a website or contacting feeds reveals normal network metadata to that service. Source links are preserved, not re-published to third-party boards.

Local data is **not encrypted**. Use OS account security/full-disk encryption; backups contain private information. SMTP passwords are not stored; non-secret account settings are local. Close the app and delete this app-specific directory to erase its data; removing the executable alone does not erase it. Do not commit app data, mail secrets, real CVs, virtualenvs or built executables to this public repo.

SQLite transactions record approval digests, attempt IDs and event history. Only one desktop instance may use a data directory. Approval binds the target, recipient, actual message/answers, sender/SMTP configuration and resume hash; attachment integrity is checked again. Sent/uncertain records are locked against accidental edits/retries. A new profile does not silently change approved applications.

## Windows package

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[packaging]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\windows.spec
.\dist\JobApplicationAgent.exe
```

The portable x64 build bundles Python/Tk/PDF support and license notices, so recipients do not need Python. It is **unsigned** and platform-specific, with no installer, auto-updater, macOS notarization or Linux package. SmartScreen/antivirus/managed-device policies may block it.

**Development-host blocker:** builds succeed, but Windows Application Control refuses the extracted `python313.dll` before Python starts. Packaged runtime verification on this host remains blocked; source execution and actual Tk widget workflows pass. No bypass is attempted. Managed devices may require administrator-approved signing/deployment.

`JobApplicationAgent.exe --smoke-test` opens the actual UI with temporary data and no network, then exits. The GitHub Actions Windows workflow requires unit/widget tests, build and packaged smoke launch before artifact upload/tagged release publication. Source publication alone does not publish a public download. Existing download artifacts are retained; revised builds are separate and labeled with limitations.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q job_agent tests run_desktop.py
```

Tests use temporary databases, synthetic text/DOCX/PDF CVs and an offline SMTP transport. They exercise final approval, byte-correct MIME/attachments, TLS-before-login, session-only secrets, duplicate/concurrent sends, uncertain outcomes, discovery, skill/pay/location/recency gates, salary evidence, hiring claims, external application imports, tracker separation and responsive actual Tk widget flows. No live application/mail send or user-account modification is part of development. Linux UI tests need a display (e.g. `xvfb-run`).

Optional bounded live **read-only** checks (respect source limits):

```powershell
$env:JOB_AGENT_LIVE_FEEDS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_live_feeds -v
Remove-Item Env:\JOB_AGENT_LIVE_FEEDS
```

Architecture: `resume.py` local extraction; `connectors.py` public discovery/status and capability contracts; `matching.py` evidence-based ranking; `market.py` observed salary reports; `email_apply.py` real SMTP channel; `service.py` approval/durable attempts; `tracking.py` provenance/stage/availability; `storage.py` SQLite/assets; `desktop.py` Tk UI/background work.

MIT licensed. Feed content and external services remain subject to their own terms.
