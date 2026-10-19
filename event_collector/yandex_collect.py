"""Export verified Yandex sessions from a local browser to an import JSON file.

The browser uses an isolated temporary profile, never the user's saved cookies.
This command does not publish to the live site or modify its database.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

import httpx

from .source_http import SyncError
from .yandex_afisha import YandexAfisha, normalize, parse_page
from .common import ROOT
from .protocol import ENDPOINT, MAX_BYTES, signature


async def browser_fetch(*, pages, channel):
    try:
        from playwright.async_api import async_playwright, Error as BrowserError
    except ImportError:
        raise SyncError("install_requirements_collector_first") from None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel=channel, headless=True)
            try:
                page = await browser.new_page()

                async def load_page(url):
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    if "captcha" in page.url.lower():
                        raise SyncError("yandex_captcha_required")
                    if page.url != url:
                        raise SyncError("yandex_unexpected_redirect")
                    if response is None or response.status != 200:
                        raise SyncError(f"http_{response.status if response else 'missing'}")
                    body = await response.body()
                    if len(body) > 8_000_000:
                        raise SyncError("page_too_large")
                    try:
                        return body.decode("utf-8")
                    except UnicodeError:
                        raise SyncError("yandex_invalid_encoding") from None

                provider = YandexAfisha(pages=pages, load_page=load_page)
                return await provider.fetch(None, None, None)
            finally:
                await browser.close()
    except BrowserError:
        # Browser exceptions can include a CAPTCHA URL with challenge tokens.
        raise SyncError("browser_navigation_failed") from None


def export(items, destination, *, now=None, days=30):
    since = now or datetime.now(timezone.utc)
    until = since + timedelta(days=days)
    records, skipped, seen = [], Counter(), {}
    for item in items:
        record, reason = normalize(item, since, until)
        if not record:
            skipped[reason] += 1
            continue
        key = record["source_event_id"]
        if key in seen and seen[key] != record:
            raise SyncError("yandex_conflicting_single_sessions")
        if key not in seen:
            seen[key] = record
            records.append(record)
    stats = dict(source="yandex", fetched=len(items), imported=len(records), skipped=dict(skipped),
                 coverage="partial", days=days)
    if not records:
        return {**stats, "status": "empty_preserved"}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=destination.name + ".", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp.replace(destination)
    finally:
        if temp:
            temp.unlink(missing_ok=True)
    return {**stats, "status": "ok", "output": str(destination)}


def publish(records, key_file, *, client=None, source="yandex"):
    if source not in {"yandex", "kudago"}:
        raise SyncError("invalid_collector_source")
    secret = Path(key_file).read_text(encoding="ascii").strip()
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", secret):
        raise SyncError("invalid_collector_key_file")
    payload = dict(version=1, batch_id=uuid.uuid4().hex, collected_at=datetime.now(timezone.utc).isoformat(), events=records)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise SyncError("batch_too_large")
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Collector-Time": stamp,
               "X-Collector-Signature": signature(secret, stamp, body)}
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        # A fixed HTTPS destination, TLS verification enabled, no redirected POSTs.
        endpoint = ENDPOINT if source == "yandex" else "/api/internal/collector/kudago"
        response = client.post("https://hangingout.ru" + endpoint, content=body, headers=headers, follow_redirects=False)
        if response.status_code != 200:
            raise SyncError(f"publish_http_{response.status_code}")
        result = response.json()
        if (result.get("status") != "ok" or result.get("source") != source
                or result.get("imported") != len(records)):
            raise SyncError("publish_invalid_receipt")
        return result
    except (httpx.HTTPError, ValueError, AttributeError):
        raise SyncError("publish_network_or_response_error") from None
    finally:
        if owned:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/yandex-events.json")
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"],
                        default="msedge" if sys.platform == "win32" else "chromium")
    parser.add_argument("--html", type=Path, help="Parse a saved HTML page without network or browser")
    parser.add_argument("--publish", action="store_true", help="Upload fresh events to hangingout.ru over signed HTTPS")
    parser.add_argument("--key-file", type=Path, default=ROOT / "data/collector.key")
    args = parser.parse_args()
    if not 1 <= args.pages <= 10 or not 1 <= args.days <= 90:
        parser.error("--pages must be 1..10 and --days must be 1..90")
    if args.publish and args.html:
        parser.error("--publish requires a fresh browser collection, not a saved HTML file")
    try:
        if args.publish and not args.key_file.is_file():
            raise SyncError("collector_key_file_missing")
        if args.html:
            if args.html.stat().st_size > 8_000_000:
                raise SyncError("page_too_large")
            items, _ = parse_page(args.html.read_text(encoding="utf-8"))
            pages = 1
        else:
            async def fetch():
                async with asyncio.timeout(180):
                    return await browser_fetch(pages=args.pages, channel=args.channel)
            items, pages = asyncio.run(fetch())
        stats = export(items, args.output, days=args.days)
        if args.publish and stats["status"] == "ok":
            records = json.loads(Path(args.output).read_text(encoding="utf-8"))
            stats["published"] = publish(records, args.key_file)
            receipt = ROOT / "data/collector-last-success.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(dict(at=time.time(), host="hangingout.ru", imported=len(records))), encoding="utf-8")
        print(json.dumps({**stats, "pages": pages}, ensure_ascii=True))
        if stats["status"] != "ok":
            raise SystemExit(2)
    except SyncError as exc:
        parser.exit(1, f"Collector stopped: {exc}. No successful publication confirmed.\n")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, TimeoutError):
        parser.exit(1, "Collection stopped: file, data or timeout error. Nothing published.\n")


if __name__ == "__main__":
    main()
