"""Read-only runner experiment. No ingestion imports, keys or publication path."""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

from scrapling.fetchers import Fetcher, StealthySession
from event_collector.kudago_html import listing_links, normalize as kudago_normalize
from event_collector.yandex_afisha import parse_page, detail_items, normalize as yandex_normalize
from event_collector.source_http import SyncError


URLS = (
    ('kudago_api', 'https://kudago.com/public-api/v1.4/events/?location=msk&page_size=1'),
    ('kudago', 'https://kudago.com/msk/exhibitions/'),
    ('yandex', 'https://afisha.yandex.ru/moscow/selections/all-events-concert'),
)


def run_probe():
    # Libraries can log redirect/challenge URLs. Emit only our bounded counters.
    logging.disable(logging.CRITICAL)
    rows = []
    since = datetime.now(timezone.utc)
    until = since + timedelta(days=60)

    def fetch(source, method, url, request, *, event_id=None):
        row = {'source': source, 'method': method, 'phase': 'detail' if event_id is not None else 'listing'}
        started = time.monotonic()
        parsed = None
        try:
            response = request(url)
            row.update(status=response.status, bytes=len(response.body),
                       captcha='captcha' in urlsplit(response.url).path.lower())
            if row['captcha']:
                row['error'] = 'captcha'
            elif response.status != 200:
                row['error'] = 'http_' + str(response.status)
            elif response.url != url:
                row['error'] = 'unexpected_redirect'
            elif len(response.body) > 8_000_000:
                row['error'] = 'page_too_large'
            else:
                html = response.body.decode('utf-8')
                if source == 'kudago_api':
                    row['items'] = len(json.loads(html)['results'])
                elif event_id is not None:
                    if source == 'kudago':
                        record, reason = kudago_normalize(html, url, since, until)
                        records = [record] if record else []
                        row['rejected_reason'] = reason
                    else:
                        records = [r for item in detail_items(html, event_id)
                                   if (r := yandex_normalize(item, since, until)[0])]
                    row['accepted_cards'] = len({r['source_event_id'] for r in records})
                    row['sessions'] = sum(len(r.get('occurrences', [])) for r in records)
                    row['periods'] = sum(len(r.get('periods', [])) for r in records)
                elif source == 'kudago':
                    parsed = listing_links(html)
                    row['items'] = len(parsed)
                else:
                    parsed = parse_page(html, path=urlsplit(url).path)[0]
                    row['items'] = len(parsed)
        except SyncError as exc:
            row['error'] = str(exc)  # Only collector-defined, safe error codes.
        except Exception as exc:
            row['error'] = type(exc).__name__  # Never raw browser exception text.
        row['seconds'] = round(time.monotonic() - started, 3)
        rows.append(row)
        print(json.dumps(row), flush=True)
        time.sleep(1)
        return parsed

    def sample(method, request, sources):
        for source, url in sources:
            listing = fetch(source, method, url, request)
            if not listing or source == 'kudago_api':
                continue
            if source == 'kudago':
                destination, eid = listing[0], 'sample'
            else:
                destination = urljoin(url, listing[0]['event'].get('url', ''))
                eid = listing[0]['event']['id']
            target = urlsplit(destination)
            expected = 'kudago.com' if source == 'kudago' else 'afisha.yandex.ru'
            prefix = '/msk/event/' if source == 'kudago' else '/moscow/'
            if (target.scheme == 'https' and target.netloc == expected and target.path.startswith(prefix)
                    and not target.query and not target.fragment):
                fetch(source, method, destination, request, event_id=eid)

    sample('scrapling_http', lambda url: Fetcher.get(url, timeout=25, retries=1, follow_redirects=False), URLS)
    try:
        with StealthySession(headless=True, solve_cloudflare=False, timeout=25000, retries=1,
                             google_search=False) as browser:
            sample('scrapling_browser', lambda url: browser.fetch(url, wait=1500), URLS[1:])
    except Exception as exc:
        rows.append({'method': 'scrapling_browser', 'error': type(exc).__name__})
        print(json.dumps(rows[-1]), flush=True)

    report = {'published': False, 'scrapling_version': '0.5', 'results': rows}
    target = Path('artifacts/scrapling-probe.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('Scrapling public-source probe. No publication. Counts are a small sample, not catalogue coverage.\n\n```json\n')
            stream.write(json.dumps(report, indent=2))
            stream.write('\n```\n')
    # A completed diagnostic is distinct from a usable replacement loader.
    usable = {r['source'] for r in rows if r.get('items', 0) > 0 and not r.get('error')}
    print(json.dumps({'probe_completed': True, 'usable_sources': sorted(usable), 'published': False}), flush=True)
    return 0 if {'kudago', 'yandex'} <= usable else 2


if __name__ == '__main__':
    raise SystemExit(run_probe())
