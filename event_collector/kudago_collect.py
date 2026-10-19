"""Hourly public-card collector, using an ordinary isolated local browser."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
import json
from pathlib import Path
import sys
import time

from .common import ROOT
from .kudago_html import LISTINGS, ORIGIN, listing_links, normalize
from .source_http import SyncError
from .yandex_collect import publish


async def collect(*, limit=120, days=30, channel="msedge"):
    from playwright.async_api import async_playwright, Error as BrowserError
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
                groups = []
                for path in LISTINGS:
                    await asyncio.sleep(1)
                    groups.append(listing_links(await load(ORIGIN + path)))
                urls = list(dict.fromkeys(url for row in zip_longest(*groups) for url in row if url))[:limit]
                for url in urls:
                    await asyncio.sleep(1)
                    html = await load(url)
                    visited += 1
                    record, reason = normalize(html, url, since, until)
                    if record:
                        old = records.get(record["source_event_id"])
                        if old and old != record:
                            raise SyncError("kudago_conflicting_card")
                        records[record["source_event_id"]] = record
                    else:
                        skipped[reason] += 1
                return list(records.values()), dict(source="kudago", fetched=visited, discovered=len(set(u for g in groups for u in g)),
                                                   imported=len(records), sessions=sum(len(r["occurrences"]) for r in records.values()),
                                                   skipped=dict(skipped), coverage="partial", days=days)
            finally:
                await browser.close()
    except BrowserError:
        raise SyncError("kudago_browser_navigation_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"], default="msedge" if sys.platform == "win32" else "chromium")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/kudago-events.json")
    parser.add_argument("--key-file", type=Path, default=ROOT / "data/collector.key")
    args = parser.parse_args()
    if not 1 <= args.limit <= 120 or not 1 <= args.days <= 90:
        parser.error("--limit must be 1..120; --days must be 1..90")
    try:
        if args.publish and not args.key_file.is_file():
            raise SyncError("collector_key_file_missing")
        async def run():
            async with asyncio.timeout(480):
                return await collect(limit=args.limit, days=args.days, channel=args.channel)
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
        parser.exit(1, f"Collector stopped: {exc}. No successful publication confirmed.\n")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, TimeoutError):
        parser.exit(1, "Collection stopped: file, data or timeout error. No successful publication confirmed.\n")


if __name__ == "__main__":
    main()
