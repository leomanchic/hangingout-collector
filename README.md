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

Late recovery jobs skip collection only when evidence matching that incident
already exists. Data recovery requires a complete confirmed publication. A missing
primary slot requires a completed primary, backup or recovery run; a confirmed
empty queue plan (`idle`) also covers the scheduled scan, without refreshing data.
A plain manual run does not cover a missing primary slot. The monitor checks the
same rule before creating an incident, before accepting a late recovery start,
and when resolving it. Stale snapshots cannot create instant open/close loops.
GitHub accepting a dispatch or a green job alone does not suffice.
Short network/429/5xx failures have bounded request retries. Batch retries reuse
the same ID and bytes, so a lost response cannot import a batch twice. CAPTCHA,
permission, schema and unknown failures stop additional automatic retries.
The monitor optionally queues one private owner alert per incident and one recovery
message through the existing Telegram outbox. Collector jobs never receive Telegram
credentials. Normal event expiration and duplicate checks remain server-side.

Recovery consistency was verified on 2026-11-04 after reproducing duplicate
incidents for one missed KudaGO slot. The server passed 276 application tests.
The monitor reserved one recovery attempt, [run 41902020860](https://github.com/leomanchic/hangingout-collector/actions/runs/41902020860),
which processed 25 queued cards and published 12 existing event/venue records.
Two subsequent monitor checks retained exactly one incident and one attempt,
resolved by that run. Both application and collector health returned HTTP 200;
the overall collector status became `ok`. Historical incident rows were preserved.

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

## Persistent Yandex detail queue

Production Yandex runs retain their detail frontier in the REG.RU SQLite database,
through signed requests to `/api/internal/collector/queue/yandex`. It is independent
of the runner filesystem and GitHub cache. No additional credentials are needed.
Dry runs, the benchmark and local collection without an active CI run do not use it.

Each run discovers the usual six lanes and leases up to 120 pending details for
20 minutes. Half the available budget is reserved for due refreshes; the remaining
half goes to never-checked cards, with unused slots transferred to the other group.
Each group rotates categories and prioritizes older tasks. First-check counts refer
to the queue, not necessarily newly added catalogue events. Queued URLs can be
visited even when the card is absent from the current page band.

Only event ID, public Moscow URL, category and scheduling metadata are persisted.
Every detail is fetched and validated again; old listing content is never imported.
Successful detail checks become due after two hours; valid pages with no detailed
schedule after six hours. Isolated identity/encoding errors back off from one hour
to a day; 404/410 cards wait a day. The existing shared detail-error budget remains.

Completion is acknowledged only after all publication batches are confirmed.
A failed publication leaves healthy tasks unfinished. Error cooldowns are saved
even when the error budget stops a run. Leases from failed runs or expired leases
are reclaimed; request retries are idempotent. The queue is capped at 5,000 IDs;
unseen-for-seven-days entries expire from the queue without deleting site events.

`/status` and `/api/collector-status` expose pending, due and leased counts. CI logs
also show selected first checks, refreshes and completed task counts. Deploy the
backend including migration `010_collector_queue.sql` before this collector version.
The prior collector still works against the migrated backend for rollback.

Live verification on 2026-11-04, collector commit `d82920f3b3a3be828648c6c828967c3753aae621`:
[first run](https://github.com/leomanchic/hangingout-collector/actions/runs/41885151485)
completed 120 details and retained 105 pending;
[second run](https://github.com/leomanchic/hangingout-collector/actions/runs/41887296139)
completed those 105, leaving zero pending or leased tasks. Server-side plan IDs
confirmed zero overlap and that all second-run tasks belonged to the first-run
backlog. Both runs passed 54 collector tests and their publication receipts were
confirmed (291 and 299 event/venue records, not distinct new site cards). The
application suite passed 249 tests. This covers the discovered queue at that time,
not every event in Moscow. Setup of Chromium took eight minutes in the second
run, but collection itself completed normally in under six minutes.

## Persistent KudaGO detail queue

KudaGO now uses the same durable protocol, with a separate source namespace and
`/api/internal/collector/queue/kudago`. Its normal budget remains 300 details
(up to 600 explicitly configured). Discovery saves all eligible URLs before the
budget is applied. Old pending URLs remain eligible when page bands rotate;
half the budget is reserved for due refreshes, with unused slots transferred.
The ID is a stable hash of the canonical Moscow event URL; URL redirects still
pass the existing parser's destination checks and catalogue deduplication.
HTML, browser state and old schedules are not persisted in the queue.

Tasks are completed only after every publication batch is confirmed. Removed
pages and valid cards without an accepted schedule are checked again after six
hours; accepted cards after two. A fatal HTTP 403, CAPTCHA or parsing error stops
the run and leaves its tasks unfinished. Logs show the phase (`home`, `listings`,
`details`) and bounded counters, without response bodies or challenge URLs.
The queue does not make a source that refuses access available.

For either source, a fully discovered but empty due-task plan may report `idle`.
The server requires a finished empty plan and no publication receipts. It counts
as an executed scheduled scan, while **last publication freshness is unchanged**.
A nonempty plan whose cards all fail validation still reports `empty_preserved`
and fails the job. This avoids treating a failed extraction as a healthy idle run.

Deploy the backend first, then update the collector. The existing migration 010
already has source keys; no schema migration, credentials or extra CRON job are
needed. Status shows both independent queues. Offline validation: 263 application
tests and 62 standalone collector tests, including continuation with missing
current listings, source isolation, rejected publication and empty-run reporting.

Live verification: [run 41897504634](https://github.com/leomanchic/hangingout-collector/actions/runs/41897504634),
commit `cbfbb1acf07dc08b889cc7454ccfa9d5bf2998c0`, succeeded on 2026-11-04.
All 226 queued IDs were distinct and acknowledged; no pending or leased tasks
remained. The server confirmed 91 accepted event/venue records (156 sessions,
21 periods), all updates with `added=0`. The collector skipped 95 cards without a supported
upcoming schedule, 39 without one unambiguous venue, and one removed card.
Page band 1/5/6/7 produced fewer than the 300-task budget in this run. Continuation
above the budget is covered by offline 300+195 and 600+195 restart tests; this live
run alone is not evidence of multi-run continuation.

The preceding hourly KudaGO run returned HTTP 403; this verification could access
the source. Both source statuses became `ok`, while the monitor still reported
the earlier missed primary slot (`schedule_overdue`). A successful manual run
does not conceal a failed scheduled run.

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

### Measured decision, 2026-11-04

[Full paired run 41851636054](https://github.com/leomanchic/hangingout-collector/actions/runs/41851636054)
used commit `17a57b5d943d9a9d3f1e9eafe75cff5d363a9acc`. All 47 offline
tests passed. The measured candidate is the hybrid adapter in `benchmark_loader.py`,
not every possible configuration of Scrapling. Production remains on Playwright.

| Source / page band | Playwright seconds; accepted records | Hybrid Scrapling seconds; accepted records |
| --- | --- | --- |
| KudaGO 1/2/3/4 | 835.3; 169 | 900.4; timeout, no complete result |
| KudaGO 1/5/6/7 | 620.4; 91 | 762.5; 88 |
| Yandex 1/2/3/4 | 295.7; 304 | 310.4; 307 |
| Yandex 1/5/6/7 | 289.9; 253 | 287.8; 252 |

Records identify an event and venue; these are not newly added site cards.
All benchmark runs were unpublished. The workflow is red because the first
KudaGO candidate timed out; the separate production jobs succeeded.

The completed KudaGO candidate was 22.9% slower. Its 21 estimated periods
matched the baseline. Yandex used only four browser navigations instead of
144-145, but had no consistent elapsed-time gain. One candidate detail had an
event identity mismatch: the existing quarantine rejected its 56 sessions and
allowed the rest of the collection to complete. A later local two-loader check
returned the correct identity and 56 sessions; that check used a different IP/time
and does not explain the transient CI response.

Live listings changed during the paired runs, so differences in accepted IDs are
not proof of parser loss. Yandex event-ID overlap was 97.7% and 98.0%; the second
pair retained every baseline session among IDs common to both runs. The candidate
does not yet justify replacing the production loader.

A concrete coverage limit was observed: Yandex inspected 120 details with 105-115
additional candidates still pending; KudaGO's first baseline inspected 300 of
496 discovered cards. The persistent Yandex queue above now continues remaining
details across runs, retaining freshness checks and bounded request rates. Pending candidates
still need date and venue validation before they can become accepted events.

## References

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Playwright Python CI](https://playwright.dev/python/docs/ci)
- [KudaGO API documentation](https://docs.kudago.com/api/)
