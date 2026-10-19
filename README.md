# Hangingout event collector

Public Moscow event collection for [hangingout.ru](https://hangingout.ru).
This repository contains only the collectors, their data contract and workflow.
It does not contain the application, user database, Telegram credentials or SSH keys.

[First verified cloud run, 19 October 2026](https://github.com/leomanchic/hangingout-collector/actions/runs/39253320842):
both jobs succeeded; KudaGO published 24 events / 46 sessions, Yandex published
13 events. The server confirmed ingestion without adding duplicate cards.

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

## Collection limits

- KudaGO: up to 120 public cards from four Moscow listings, with one second between
  navigations. Explicit upcoming showtimes and one identifiable venue are required.
- Yandex Afisha: five pages of a Moscow concert selection; only exact single sessions
  with a venue are imported.
- Both cover the next 30 days, not the complete Moscow catalogue. Missing cards are
  not treated as cancellations. Date ranges and venue opening hours are not showtimes.
- Browser sessions are isolated. No saved personal cookies, CAPTCHA solving, stealth
  patches or proxy rotation. If a source blocks a runner, the job fails and the
  existing server catalogue is preserved.
- Source attribution links are retained. Articles and photographs are not republished.
- Server-side ingestion updates source IDs and checks cross-source duplicates while
  retaining existing user plans, links, prices and distinct showtimes.

For local development: Python 3.12, `pip install -r requirements.txt`, then
`python -m playwright install chromium`. Collect without publishing using
`python -m event_collector.kudago_collect --channel chromium` or the equivalent
`event_collector.yandex_collect`. Generated data is ignored by Git.

## References

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Playwright Python CI](https://playwright.dev/python/docs/ci)
- [KudaGO API documentation](https://docs.kudago.com/api/)
