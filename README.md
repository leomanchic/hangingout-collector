# Hangingout event collector

Public Moscow event collection for [hangingout.ru](https://hangingout.ru).
This repository contains only the collectors, their data contract and workflow.
It does not contain the application, user database, Telegram credentials or SSH keys.

[First verified cloud run, 19 October 2026](https://github.com/leomanchic/hangingout-collector/actions/runs/39253320842):
both jobs succeeded; KudaGO published 24 events / 46 sessions, Yandex published
13 events. The server confirmed ingestion without adding duplicate cards.

[Expanded cloud run, 19 October 2026](https://github.com/leomanchic/hangingout-collector/actions/runs/39257213976)
published 263 Yandex event/venue records with 1,814 exact sessions in all six
categories. Together with the verified initial deep KudaGO import, the catalogue
reached 496 event/venue cards and 2,194 sessions (327 distinct titles). The same
film at different cinemas has distinct venue records; these are not ingestion
duplicates. Counts are a verification snapshot, not a guaranteed hourly yield.

Cinema records also include `source_movie_id`: the provider's film ID, shared by
all its cinemas. The application presents one film card with a cinema/day/session
selector, while keeping venue and occurrence IDs for attendance and plans. This
uses the same collected schedule; it does not add network requests or infer a
film identity from a title. Existing 24-hex Yandex film/venue IDs remain compatible.

The REG.RU monitor is the primary clock: it dispatches each source hourly at :05
(UTC) to standard GitHub Ubuntu runners. The GitHub `:47` timer is a backup.
Explicit manual runs are also available from **Actions → Collect Moscow events → Run workflow**.
KudaGO and Yandex run independently: failure of one source does not stop the other.
GitHub schedules can be delayed. Public repository schedules may be disabled after
60 days without repository activity; re-enable from the Actions page when necessary.

## Configuration

Add one repository Actions secret, `COLLECTOR_SECRET`, containing the 64-character
hex ingestion key matching the server. No Telegram or SSH access is needed.
The workflow signs event batches and sends them only to `https://hangingout.ru`.
TLS verification is enabled and POST redirects are forbidden. The key is passed
through a temporary private file and never included in the command line or artifacts.

Only the default branch schedule or an explicit manual run publishes data.
No pull-request trigger executes untrusted contributor code with secrets.
Source pages and their JavaScript are untrusted inputs; they cannot change the
destination, event schema, signature or workflow configuration.

## Discovery strategy and limits

The primary hourly schedule is `5 * * * *` (UTC), driven by the existing REG.RU
five-minute CRON with `COLLECTOR_PRIMARY_SCHEDULE_ENABLED=true`. The backup GitHub
schedule remains `47 * * * *`. A signed start/end receipt links each
source job to its GitHub run and attempt. The server marks a run complete only
when its totals match every acknowledged publication batch. Partial uploads,
empty results, failures and timeouts never refresh the complete-success clock.
An independent REG.RU CRON checks GitHub and receipts every five minutes:
[/status](https://hangingout.ru/status), [/api/collector-status](https://hangingout.ru/api/collector-status).
The separate `/api/collector-health` probe returns 503 when attention is needed;
application health remains independent. Source freshness allows three hours,
primary scheduler grace 20 minutes (backup 45), and a stuck job 25 minutes. A greater than 70% drop
against at least three prior scheduled runs of the same page band is flagged; manual diagnostic runs
are excluded from that comparison. A watchdog/API failure cannot produce a green status.

Each primary source/hour is reserved in SQLite before dispatch; its `schedule_key`
is echoed in the signed receipt. Restarts and ambiguous POST responses do not repeat
that request. Missed hours are not replayed in a burst. Queued or running collection
and pending recovery suppress additional dispatches. The receiving server rejects
obsolete primary keys and skips backup starts when a full run already covers the
hour or is active. A manual reduced-limit diagnostic does not cover the hour.
GitHub's own queue can still delay execution. The status page reports the primary
clock separately from the backup; a delayed backup alone does not degrade healthy
primary delivery. To revert orchestration, set the primary flag to false and run
the monitor once; the GitHub schedule and bounded recovery remain available.

The REG.RU monitor can recover missing/failed runs using a separate fine-grained
GitHub token scoped to this repository with Actions write permission. The token
stays outside the web root and is never provided to collectors. Recovery dispatches
select only the affected source and include a unique `recovery_key`. Persistent
reservations prevent overlapping monitor processes from launching the same attempt;
ambiguous dispatches are reconciled using the run title and signed start receipt.
Each incident permits at most two recovery attempts with a 20-minute cooldown;
an ordinary transient source failure first waits ten minutes. Active GitHub jobs
are allowed to finish. Disabled workflows are not automatically re-enabled.

Late recovery jobs skip collection when a newer complete publication has already
satisfied the incident. Only verified ingestion receipts resolve a recovery;
GitHub accepting a dispatch or a green job without delivery does not suffice.
Short network/429/5xx failures have bounded request retries. Batch retries reuse
the same ID and bytes, so a lost response cannot import a batch twice. CAPTCHA,
permission, schema and unknown failures stop additional automatic retries.
The monitor optionally queues one private owner alert per incident and one recovery
message through the existing Telegram outbox. Collector jobs never receive Telegram
credentials. Normal event expiration and duplicate checks remain server-side.

Bounded multi-day KudaGO visiting periods have their own date-only schema and
an explicit estimated label. They expire after the final Moscow calendar day,
or after seven days without a fresh source check. They create no attendance or
companion session: visitors follow the attribution link to confirm opening hours.

- Six separate discovery lanes: music, theatre, exhibitions, excursions, learning
  and cinema. Categories and pages take turns so promoted cards cannot exhaust
  the entire request budget. Actual category coverage depends on confirmed times.
- Every hour refreshes page 1 and rotates a deeper band: pages 2–4, then 5–7,
  then 8–10. The cycle covers the first ten pages without re-reading every page
  of every listing in every run. KudaGO uses the public HTML URLs observed in its
  Next-page button requests. Empty or repeated pages end that lane; categories
  with fewer pages naturally have smaller discovery counts.
- KudaGO: up to 300 distinct public event cards per run, with at least one second
  between navigations. One identifiable venue is required. Explicit showtimes are
  retained as sessions; bounded multi-day exhibition/visiting dates become separate
  estimated periods, never artificial midnight sessions.
  Renamed event URLs are followed; removed cards and cards converted into places
  or editorial lists are skipped. Challenges and foreign redirects still stop
  collection. Manual workflow input `kudago_limit` allows a smaller diagnostic
  pass; the hourly default remains 300.
- Yandex Afisha: up to four listing pages per lane and 120 additional event-detail
  pages. Read explicit dated sessions attached to each event and venue from the
  public page, including theatre schedules and dated cinema/masterclass slots.
  Do not extrapolate daily/weekly summaries. Different venues keep separate IDs.
  Bad UTF-8 responses are retried once, within the three-attempt network limit.
  Isolated undecodable, removed (404/410) or identity-mismatched detail pages are
  quarantined; they never contribute sessions or fallback records. All these
  failures share one budget: at most 10% of the actual detail sample, capped at
  four cards. More failures, listing failures, changed schemas and access
  challenges still stop the run. `detail_failure_reasons` and `detail_partial`
  are logged; a partial detail sample produces an Actions warning. `detail_mismatches`
  is included in the signed run metrics and source status.
- Both cover the next 60 days, not the complete Moscow catalogue. At most 100
  nearest confirmed sessions are retained per event/venue per run. Missing cards are
  not treated as cancellations. Date ranges and venue opening hours are not showtimes.
- Production browser sessions are isolated. No saved personal cookies, CAPTCHA solving, stealth
  patches or proxy rotation. If a source blocks a runner, the job fails and the
  existing server catalogue is preserved.
- Source attribution links are retained. Articles and photographs are not republished.
- Server-side ingestion updates source IDs and checks cross-source duplicates while
  retaining existing user plans, links, prices and distinct showtimes.
- Large results are split into signed batches of at most 120 records and 256 KiB.
  Each receipt is checked. A failed later batch reports already committed batches;
  re-running safely updates those records. Partial results never delete older ones.
- Logs report discovered cards by lane, imported events/sessions by category,
  rejected records and remaining detail budget. Discovery is partial, not proof
  that every Moscow event or venue was visited.

For local development: Python 3.12, `pip install -r requirements.txt`, then
`python -m playwright install chromium`. Collect without publishing using
`python -m event_collector.kudago_collect --channel chromium` or the equivalent
`event_collector.yandex_collect`. Generated data is ignored by Git.

## Read-only Scrapling experiment

The separate manual **Scrapling read-only probe** workflow tests Scrapling 0.5
HTTP and browser loaders on one listing and at most one detail per source and
transport. KudaGO API access is also checked. Existing parsers and date/identity
validation are used. Results and failures appear in logs and the job summary.
The experiment has no ingestion secret and never publishes data. Its dependencies
are isolated in `requirements-probe.txt`; production remains on Playwright.
The browser probe uses Scrapling's browser patches, without saved user state,
proxies or CAPTCHA solving. One successful sample is not a reliability benchmark.

## Full-volume loader comparison

The manual **Full collector comparison** workflow runs the existing collectors
with their production budgets: six category lanes, four pages per lane, up to
300 KudaGO cards or 120 Yandex details, and a 60-day date horizon. Two paired
rounds cover page windows 1/2/3/4 and 1/5/6/7. The second round reverses loader
order. Each pair uses the same runner/IP and reference time; live listings can
still change between runs. Browser startup is included in timing; installation
is excluded. Every individual collection is limited to 15 minutes.

The candidate first uses Scrapling HTTP, falling back to an isolated browser
when required. A 403 switches that host to browser mode for the rest of the run.
429 and server errors retain the existing collector retry/backoff policy.
Parsing, source identity, dates, deduplication and request pacing are unchanged.
Original document bytes are retained separately from the hydrated DOM, matching
the existing pagination and strict UTF-8 response parsing.
No cookies, proxies or CAPTCHA solver are supplied. The transport adapter only
exists in an isolated benchmark process; production is not switched by this test.

Comparison artifacts (retained for seven days) contain public event/venue IDs,
schedule fingerprints, coverage losses/gains, validation skips and transport
timings. They contain no raw source HTML or browser state. The workflow receives
no ingestion credentials and publication functions are explicitly disabled.
An unsuccessful collection fails the job while preserving available diagnostics.

## References

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Playwright Python CI](https://playwright.dev/python/docs/ci)
- [KudaGO API documentation](https://docs.kudago.com/api/)
