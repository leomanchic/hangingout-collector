"""Hourly public-card collector, using an ordinary isolated local browser."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

from .common import ROOT
from .kudago_html import LISTINGS, ORIGIN, listing_links, normalize, event_url, card_destination
from .source_http import SyncError, retry_delay
from .yandex_collect import publish
from .collector_frontier import page_window, interleave
from .collector_queue_client import DetailQueue, describe_kudago

LANE_CATEGORIES = dict(zip(LISTINGS, ('music','theatre','learning','art','walk','cinema')))
SKIP_REASONS = frozenset({'removed_card', 'no_event_metadata', 'different_canonical_url',
    'not_scheduled', 'no_stable_event_id', 'other_category', 'no_unambiguous_venue',
    'no_schedule_year_bounds', 'no_precise_upcoming_sessions', 'started_during_collection'})


def write_skip_report(path, rows, *, complete, fetched, accepted):
    """Public, bounded extraction diagnostics; never a publication receipt."""
    if path is None:
        return
    safe = []
    for row in rows:
        if (set(row) != {'url', 'category', 'reason'} or len(row['url']) > 600 or event_url(row['url']) != row['url']
                or row['category'] not in LANE_CATEGORIES.values() or row['reason'] not in SKIP_REASONS):
            raise SyncError('invalid_skip_diagnostic')
        safe.append(row)
    if len(safe) > 600:
        raise SyncError('skip_diagnostic_limit')
    report = dict(version=1, source='kudago', scope='card_extraction',
                  generated_at=datetime.now(timezone.utc).isoformat(), collection_complete=complete,
                  fetched=fetched, accepted_records=accepted, skipped_cards=safe,
                  reasons=dict(Counter(row['reason'] for row in safe)))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


async def collect(*, limit=300, days=60, pages=4, channel="msedge", queue=None, diagnostics_path=None):
    from playwright.async_api import async_playwright, Error as BrowserError
    since = datetime.now(timezone.utc)
    until = since + timedelta(days=days)
    records, skipped, visited = {}, Counter(), 0
    rejected, accepted_tasks = {}, {}
    def report(complete=False):
        write_skip_report(diagnostics_path, rejected.values(), complete=complete, fetched=visited, accepted=len(records))
    def reject(task, reason):
        rejected[task['url']] = dict(url=task['url'], category=task['category'], reason=reason)
        if queue is not None:
            queue.record(task['id'], 'empty')
        report()
    report()
    phase='home'
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel=channel, headless=True)
            try:
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
                page = await context.new_page()

                async def load(url, *, home=False, raw=False, card=False):
                    for attempt in range(3):
                        try:
                            response = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                            if 'captcha' in page.url.lower():
                                raise SyncError('kudago_captcha_required')
                            if response is not None and (response.status == 429 or response.status >= 500):
                                delay = retry_delay(response.headers, attempt)
                                if attempt == 2 or delay is None:
                                    raise SyncError(f'kudago_http_{response.status}' + ('_retry_later' if delay is None else ''))
                                await asyncio.sleep(delay)
                                continue
                            break
                        except BrowserError:
                            if 'captcha' in page.url.lower():
                                raise SyncError('kudago_captcha_required') from None
                            if attempt == 2:
                                raise SyncError('kudago_browser_navigation_failed') from None
                            await asyncio.sleep(2 ** attempt)
                    # The public site may run its own JS on first visit. No CAPTCHA
                    # solving, cookie extraction, proxy rotation or stealth patches.
                    if home:
                        try:
                            await page.wait_for_selector("a.post-title-link", timeout=15_000)
                        except BrowserError:
                            if response is not None and response.status in {401,403}:
                                raise SyncError(f'kudago_http_{response.status}') from None
                            raise
                        if 'captcha' in page.url.lower():
                            raise SyncError('kudago_captcha_required')
                    elif card and response is not None and response.status in {404, 410}:
                        return None
                    elif response is None or response.status != 200:
                        raise SyncError(f"kudago_http_{response.status if response else 'missing'}")
                    if page.url.rstrip("/") != url.rstrip("/"):
                        if not card:
                            raise SyncError("kudago_unexpected_redirect")
                        if card_destination(page.url) is None:
                            return None
                    html = await response.text() if raw and response is not None else await page.content()
                    if len(html.encode("utf-8")) > 8_000_000:
                        raise SyncError("page_too_large")
                    return html

                await load(ORIGIN + "/msk/", home=True)
                groups, lanes, pagination_stops, categories = [], {}, {}, {}
                window = page_window(pages)
                phase='listings'
                for path in LISTINGS:
                    await asyncio.sleep(1)
                    html = await load(ORIGIN + path)
                    batches = [listing_links(html)]
                    known = set(batches[0])
                    for number in window[1:]:
                        await asyncio.sleep(1)
                        # The public Next-page button requests this very URL.
                        # Read its HTML directly so slow client hydration cannot
                        # silently restrict cloud runs to page 1.
                        html = await load(ORIGIN + path + f'?get_facets=1&page={number}', raw=True)
                        links = listing_links(html)
                        if not links or not set(links) - known:
                            pagination_stops[path] = number
                            break
                        known.update(links)
                        batches.append(links)
                    group = interleave(batches)
                    groups.append(group)
                    for url in group:categories.setdefault(url,LANE_CATEGORIES[path])
                    lanes[path] = len(group)
                    print(json.dumps({'progress':'discovery', 'lane':path, 'cards':len(group)}), flush=True)
                discovered=interleave(groups)
                work=[{'url':url, 'category':categories[url]} for url in discovered[:limit]]
                if queue is not None:
                    candidates=[describe_kudago(url,categories[url]) for url in discovered]
                    if any(t is None for t in candidates):raise SyncError('queue_invalid_kudago_candidate')
                    work=await queue.plan(candidates,limit)
                phase='details'
                for task in work:
                    url=task['url']
                    await asyncio.sleep(1)
                    html = await load(url, card=True)
                    visited += 1
                    if visited % 50 == 0:
                        print(json.dumps({'progress':'details', 'pages':visited, 'events':len(records)}), flush=True)
                    if html is None:
                        skipped['removed_card'] += 1
                        reject(task, 'removed_card')
                        continue
                    record, reason = normalize(html, event_url(page.url), since, until)
                    if record:
                        old = records.get(record["source_event_id"])
                        if old and old != record:
                            raise SyncError("kudago_conflicting_card")
                        records[record["source_event_id"]] = record
                        accepted_tasks.setdefault(record['source_event_id'], []).append(task)
                        if queue is not None:queue.record(task['id'], 'checked')
                    else:
                        skipped[reason] += 1
                        reject(task, reason)
                # A deep crawl takes minutes. A slot that started during it must
                # not invalidate the whole signed batch at the receiver.
                cutoff = datetime.now(timezone.utc) + timedelta(seconds=30)
                for key, record in list(records.items()):
                    record['occurrences'] = [o for o in record['occurrences'] if datetime.fromisoformat(o['starts_at']) > cutoff][:100]
                    if not record['occurrences'] and not record.get('period'):
                        del records[key]
                        skipped['started_during_collection'] += 1
                        for task in accepted_tasks[key]:
                            reject(task, 'started_during_collection')
                report(complete=True)
                return list(records.values()), dict(source="kudago", fetched=visited, discovered=len(discovered),
                                                   imported=len(records), sessions=sum(len(r["occurrences"]) for r in records.values()),
                                                   periods=sum(bool(r.get('period')) for r in records.values()),
                                                   skipped=dict(skipped), categories=dict(Counter(r['category'] for r in records.values())),
                                                   lanes=lanes, pagination_stops=pagination_stops,
                                                   page_window=window, coverage="partial", days=days,
                                                   **({'queue':queue.summary} if queue is not None else {}))
            finally:
                await browser.close()
    except SyncError:
        report()
        # Only bounded phase/counters: never log page HTML, cookies or challenge URLs.
        print(json.dumps({'progress':'collection_stopped','source':'kudago','phase':phase,
                          'fetched':visited,'accepted':len(records)}),flush=True)
        raise
    except BrowserError:
        report()
        raise SyncError("kudago_browser_navigation_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"], default="msedge" if sys.platform == "win32" else "chromium")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/kudago-events.json")
    parser.add_argument("--diagnostics", type=Path, default=ROOT / "artifacts/kudago-skips.json")
    parser.add_argument("--key-file", type=Path, default=ROOT / "data/collector.key")
    args = parser.parse_args()
    if not 1 <= args.limit <= 600 or not 1 <= args.days <= 90 or not 1 <= args.pages <= 10:
        parser.error("--limit must be 1..600; --days must be 1..90; --pages must be 1..10")
    try:
        if args.publish and not args.key_file.is_file():
            raise SyncError("collector_key_file_missing")
        queue=DetailQueue(args.key_file,os.environ['COLLECTOR_RUN_KEY'],source='kudago') if args.publish and os.environ.get('COLLECTOR_RUN_KEY') else None
        async def run():
            async with asyncio.timeout(900):
                return await collect(limit=args.limit, days=args.days, pages=args.pages, channel=args.channel,queue=queue,
                                     diagnostics_path=args.diagnostics)
        records, stats = asyncio.run(run())
        if not records:
            if queue is not None:stats['queue']=queue.finish(0)
            if queue is not None and not queue.tasks:
                print(json.dumps({**stats,'status':'idle'}))
                return
            print(json.dumps({**stats, "status": "empty_preserved"}))
            raise SystemExit(2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
        if args.publish:
            stats["published"] = publish(records, args.key_file, source="kudago")
            if queue is not None:stats['queue']=queue.finish(len(records))
            receipt = ROOT / "data/kudago-collector-last-success.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(dict(at=time.time(), host="hangingout.ru", imported=len(records))), encoding="utf-8")
        print(json.dumps({**stats, "status": "ok"}, ensure_ascii=True))
    except SyncError as exc:
        parser.exit(1, f"Collector stopped: {exc}. Check committed_batches if publication was partial.\n")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, TimeoutError):
        parser.exit(1, "Collection stopped: file, data or timeout error. No successful publication confirmed.\n")


if __name__ == "__main__":
    main()
