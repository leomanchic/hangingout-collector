"""Hourly public-card collector, using an ordinary isolated local browser."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

from .common import ROOT
from .kudago_html import LISTINGS, ORIGIN, listing_links, normalize
from .source_http import SyncError
from .yandex_collect import publish
from .collector_frontier import page_window, interleave


async def collect(*, limit=300, days=60, pages=4, channel="msedge"):
    from playwright.async_api import async_playwright, Error as BrowserError, TimeoutError as BrowserTimeout
    since = datetime.now(timezone.utc)
    until = since + timedelta(days=days)
    records, skipped, visited = {}, Counter(), 0
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel=channel, headless=True)
            try:
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
                page = await context.new_page()

                async def load(url, *, home=False):
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    # The public site may run its own JS on first visit. No CAPTCHA
                    # solving, cookie extraction, proxy rotation or stealth patches.
                    if home:
                        await page.wait_for_selector("a.post-title-link", timeout=15_000)
                    elif response is None or response.status != 200:
                        raise SyncError(f"kudago_http_{response.status if response else 'missing'}")
                    if page.url.rstrip("/") != url.rstrip("/"):
                        raise SyncError("kudago_unexpected_redirect")
                    html = await page.content()
                    if len(html.encode("utf-8")) > 8_000_000:
                        raise SyncError("page_too_large")
                    return html

                await load(ORIGIN + "/msk/", home=True)
                groups, lanes, pagination_stops = [], {}, {}
                window = page_window(pages)
                for path in LISTINGS:
                    await asyncio.sleep(1)
                    html = await load(ORIGIN + path)
                    batches = [listing_links(html)]
                    previous = batches[0]
                    for number in range(2, max(window) + 1):
                        button = page.locator(f'.feed-pagination__button-{number}')
                        try:
                            # Pagination is hydrated after the document loads.
                            # Immediate count()/is_visible() races on CI runners.
                            await button.wait_for(state='visible', timeout=8_000)
                        except BrowserTimeout:
                            pagination_stops[path] = number
                            break
                        await asyncio.sleep(1)
                        await button.click()
                        await page.wait_for_function('old => { const a=document.querySelector("a.post-title-link"); return a && a.href !== old; }',
                                                     arg=previous[0] if previous else '', timeout=15_000)
                        html = await page.content()
                        links = listing_links(html)
                        if not links or links == previous:
                            break
                        previous = links
                        if number in window:
                            batches.append(links)
                    group = interleave(batches)
                    groups.append(group)
                    lanes[path] = len(group)
                    print(json.dumps({'progress':'discovery', 'lane':path, 'cards':len(group)}), flush=True)
                urls = interleave(groups, limit=limit)
                for url in urls:
                    await asyncio.sleep(1)
                    html = await load(url)
                    visited += 1
                    if visited % 50 == 0:
                        print(json.dumps({'progress':'details', 'pages':visited, 'events':len(records)}), flush=True)
                    record, reason = normalize(html, url, since, until)
                    if record:
                        old = records.get(record["source_event_id"])
                        if old and old != record:
                            raise SyncError("kudago_conflicting_card")
                        records[record["source_event_id"]] = record
                    else:
                        skipped[reason] += 1
                # A deep crawl takes minutes. A slot that started during it must
                # not invalidate the whole signed batch at the receiver.
                cutoff = datetime.now(timezone.utc) + timedelta(seconds=30)
                for key, record in list(records.items()):
                    record['occurrences'] = [o for o in record['occurrences'] if datetime.fromisoformat(o['starts_at']) > cutoff][:100]
                    if not record['occurrences']:
                        del records[key]
                        skipped['started_during_collection'] += 1
                return list(records.values()), dict(source="kudago", fetched=visited, discovered=len(set(u for g in groups for u in g)),
                                                   imported=len(records), sessions=sum(len(r["occurrences"]) for r in records.values()),
                                                   skipped=dict(skipped), categories=dict(Counter(r['category'] for r in records.values())),
                                                   lanes=lanes, pagination_stops=pagination_stops,
                                                   page_window=window, coverage="partial", days=days)
            finally:
                await browser.close()
    except BrowserError:
        raise SyncError("kudago_browser_navigation_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"], default="msedge" if sys.platform == "win32" else "chromium")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/kudago-events.json")
    parser.add_argument("--key-file", type=Path, default=ROOT / "data/collector.key")
    args = parser.parse_args()
    if not 1 <= args.limit <= 600 or not 1 <= args.days <= 90 or not 1 <= args.pages <= 10:
        parser.error("--limit must be 1..600; --days must be 1..90; --pages must be 1..10")
    try:
        if args.publish and not args.key_file.is_file():
            raise SyncError("collector_key_file_missing")
        async def run():
            async with asyncio.timeout(900):
                return await collect(limit=args.limit, days=args.days, pages=args.pages, channel=args.channel)
        records, stats = asyncio.run(run())
        if not records:
            print(json.dumps({**stats, "status": "empty_preserved"}))
            raise SystemExit(2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
        if args.publish:
            stats["published"] = publish(records, args.key_file, source="kudago")
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
