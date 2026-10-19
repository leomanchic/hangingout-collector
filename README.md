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

The GitHub Actions workflow runs on standard Ubuntu runners every hour at minute 23
(UTC), and can also be started from **Actions → Collect Moscow events → Run workflow**.
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

- Six separate discovery lanes: music, theatre, exhibitions, excursions, learning
  and cinema. Categories and pages take turns so promoted cards cannot exhaust
  the entire request budget. Actual category coverage depends on confirmed times.
- Every hour refreshes page 1 and rotates a deeper band: pages 2–4, then 5–7,
  then 8–10. The cycle covers the first ten pages without re-reading every page
  of every listing in every run. KudaGO uses the public HTML URLs observed in its
  Next-page button requests. Empty or repeated pages end that lane; categories
  with fewer pages naturally have smaller discovery counts.
- KudaGO: up to 300 distinct public event cards per run, with at least one second
  between navigations. Explicit showtimes and one identifiable venue are required.
- Yandex Afisha: up to four listing pages per lane and 120 additional event-detail
  pages. Read explicit dated sessions attached to each event and venue from the
  public page, including theatre schedules and dated cinema/masterclass slots.
  Do not extrapolate daily/weekly summaries. Different venues keep separate IDs.
- Both cover the next 60 days, not the complete Moscow catalogue. At most 100
  nearest confirmed sessions are retained per event/venue per run. Missing cards are
  not treated as cancellations. Date ranges and venue opening hours are not showtimes.
- Browser sessions are isolated. No saved personal cookies, CAPTCHA solving, stealth
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

## References

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Playwright Python CI](https://playwright.dev/python/docs/ci)
- [KudaGO API documentation](https://docs.kudago.com/api/)
