"""Read precise Moscow sessions from public Yandex Afisha HTML selections.

This is a bounded, partial catalogue, never an authoritative cancellation feed.
No private API, JavaScript evaluation, credentials or CAPTCHA solver is used.
"""
import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .models import EventImport
from .source_http import SyncError, clean

ORIGIN = "https://afisha.yandex.ru"
PATH = "/moscow/selections/all-events-concert"
ZONE = ZoneInfo("Europe/Moscow")
SOURCE = "Яндекс Афиша"
CATEGORIES = {"concert": "music", "theatre": "theatre", "art": "art",
              "excursions": "walk", "cinema": "cinema", "education": "learning", "masterclass": "learning"}
LISTINGS = {"music": PATH, "theatre": "/moscow/selections/all-events-theatre",
            "art": "/moscow/selections/all-events-art", "walk": "/moscow/selections/all-events-excursions",
            "learning": "/moscow/masterclass", "cinema": "/moscow/selections/all-events-cinema"}
ASSIGNMENT = re.compile(r"\s*window\[['\"]__APOLLO_STATE__['\"]\]\s*=\s*(.*?)\s*;?\s*", re.S)
LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|new Date\("(?:[^"\\]|\\.)*"\)|\bundefined\b')


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.links, self.parts = [], [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.parts = []
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.parts is not None:
            self.scripts.append("".join(self.parts))
            self.parts = None


def resolve(state, value):
    if isinstance(value, dict) and "__ref" in value:
        value = state.get(value["__ref"])
        if not isinstance(value, dict) or "__ref" in value:
            raise SyncError("yandex_invalid_reference")
    return value


def read_state(html):
    """Extract JSON-like state without executing any page-provided code."""
    if len(html.encode("utf-8")) > 8_000_000:
        raise SyncError("page_too_large")
    parser = Page()
    parser.feed(html)
    matches = [match for script in parser.scripts if (match := ASSIGNMENT.fullmatch(script))]
    if len(matches) != 1:
        raise SyncError("yandex_state_missing_or_changed")
    # The serializer uses these two literals in otherwise valid JSON. Matching
    # quoted strings first ensures words inside titles/descriptions are untouched.
    data = LITERAL.sub(lambda m: "null" if m[0] == "undefined" else
                       m[0][9:-1] if m[0].startswith("new Date(") else m[0], matches[0][1])
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        raise SyncError("yandex_state_invalid_json") from None
    if not isinstance(state, dict) or not isinstance(state.get("ROOT_QUERY"), dict):
        raise SyncError("yandex_state_invalid_schema")
    return state, parser


def parse_page(html, *, page=1, path=PATH):
    state, parser = read_state(html)
    items, stack, nodes = [], [(state, 0)], 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 50:
            raise SyncError("yandex_state_too_complex")
        if isinstance(value, dict):
            if value.get("__typename") == "ActualEvent":
                event = resolve(state, value.get("event"))
                schedule = resolve(state, value.get("scheduleInfo"))
                if not isinstance(event, dict) or not isinstance(schedule, dict):
                    raise SyncError("yandex_invalid_event_schema")
                place = resolve(state, schedule.get("onlyPlace")) or {}
                city = resolve(state, place.get("city")) or {}
                items.append({"event": event, "schedule": schedule, "place": place, "city": city})
            else:
                stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    if not items:
        raise SyncError("yandex_no_event_schedules")
    next_page = False
    for href in parser.links:
        parsed = urlsplit(urljoin(ORIGIN + path, href))
        if (parsed.scheme == "https" and parsed.netloc == "afisha.yandex.ru"
                and parsed.path.rstrip("/") == path
                and parse_qs(parsed.query) == {"page": [str(page + 1)]}):
            next_page = True
    return items, next_page


def detail_items(html, expected_event_id):
    """Only dated sessions in the requested event's public schedule, never recommendations.

    Handle both performance schedules and dated cinema/masterclass slots. A
    regularity/opening-hours summary alone cannot generate a single occurrence.
    """
    state, _ = read_state(html)
    root = state["ROOT_QUERY"]
    event = resolve(state, root.get('event(' + json.dumps({"id": expected_event_id}, separators=(",", ":")) + ')'))
    if not isinstance(event, dict) or event.get("id") != expected_event_id:
        raise SyncError("yandex_detail_event_mismatch")
    items, seen = [], set()

    def add(place_ref, session, day):
        if not isinstance(day, str) or not isinstance(session, dict) or session.get("date") != day:
            return
        value = session.get("datetime")
        if not isinstance(value, str) or not value.startswith(day + "T"):
            return
        place = resolve(state, place_ref) or {}
        city = resolve(state, place.get("city")) or {}
        key = (place.get("id"), value)
        if key in seen:
            return
        seen.add(key)
        ticket = resolve(state, session.get("ticket")) or {}
        if ticket.get("saleStatus") in {"cancelled", "canceled"}:
            return
        price = ticket.get("price") or {}
        prices = [{"currency": price.get("currency"), "value": price.get("min")}]
        # This exact datetime is attached to this venue and date by the source.
        schedule = {"dates": [day], "placesTotal": 1, "prices": prices,
                    "regularity": {"singleShowtime": value}}
        items.append({"event": event, "schedule": schedule, "place": place, "city": city, "dated_session": True})

    for key, value in root.items():
        if not isinstance(value, dict):
            continue
        if key.startswith("eventScheduleOther("):
            try:
                params = json.loads(key[len("eventScheduleOther("):-1])
            except ValueError:
                continue
            if params.get("id") != expected_event_id:
                continue
            for group in value.get("byDate") or []:
                for slot in group.get("sessions") or []:
                    add(slot.get("place"), slot.get("session"), group.get("date"))
        elif key.startswith("eventScheduleCinema:"):
            try:
                params = json.loads(key[len("eventScheduleCinema:"):])
            except ValueError:
                continue
            if params.get("id") != expected_event_id:
                continue
            for group in value.get("items") or []:
                for schedule in group.get("schedule") or []:
                    for session in schedule.get("sessions") or []:
                        add(group.get("place"), session, group.get("date"))
    return items


def normalize(item, since, until):
    event, schedule, place, city = (item[key] for key in ("event", "schedule", "place", "city"))
    eid = event.get("id")
    if not isinstance(eid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", eid):
        raise SyncError("yandex_invalid_event_id")
    if city.get("id") != "moscow":
        return None, "other_or_unknown_city"
    category = CATEGORIES.get((event.get("type") or {}).get("code"))
    if not category:
        tags = {tag.get('code') for key, value in event.items() if key.startswith('tags(') and isinstance(value, list)
                for tag in value if isinstance(tag, dict)}
        category = next((CATEGORIES[tag] for tag in ('masterclass', 'education', 'theatre', 'excursions', 'art', 'cinema', 'concert')
                         if tag in tags), None)
    if not category:
        return None, "other_category"
    venue, address = clean(place.get("title"), 200), clean(place.get("address"), 300)
    pid = place.get("id")
    if (schedule.get("placesTotal") != 1 or not venue or not address
            or not isinstance(pid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", pid)):
        return None, "no_single_known_venue"
    regularity = schedule.get("regularity") or {}
    value = regularity.get("singleShowtime")
    if ((event.get("permanent") and not item.get("dated_session")) or schedule.get("permanent") or schedule.get("multiSession")
            or regularity.get("daily") or regularity.get("weekly")
            or not isinstance(value, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", value)):
        return None, "no_precise_single_session"
    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
        start = start.replace(tzinfo=ZONE) if start.tzinfo is None else start.astimezone(ZONE)
    except ValueError:
        raise SyncError("yandex_invalid_source_time") from None
    # A single timestamp must agree with the source's date list. Do not spread
    # one time over every date of a multi-session event.
    if schedule.get("dates") != [start.date().isoformat()]:
        return None, "ambiguous_session_dates"
    if not since < start < until:
        return None, "outside_date_window"
    title = clean(event.get("title"), 200)
    if not title:
        raise SyncError("yandex_missing_title")
    url = urljoin(ORIGIN, event.get("url", ""))
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "afisha.yandex.ru"
            or not parsed.path.startswith("/moscow/") or parsed.query or parsed.fragment):
        raise SyncError("yandex_invalid_source_url")
    prices = schedule.get("prices") or []
    if not isinstance(prices, list):
        raise SyncError("yandex_invalid_prices")
    # Money.value in the observed HTML is in kopecks, not roubles.
    amounts = [money["value"] for money in prices if isinstance(money, dict)
               and money.get("currency") == "rub" and type(money.get("value")) is int
               and 0 < money["value"] <= 10_000_000_000]
    price, price_text = None, "Цену уточняйте на Яндекс Афише"
    if amounts and len(amounts) == len(prices):
        minimum = min(amounts)
        price = minimum // 100 if minimum % 100 == 0 else None
        amount_text = f"{minimum // 100:,}".replace(",", " ")
        if minimum % 100:
            amount_text += f",{minimum % 100:02}"
        price_text = f"от {amount_text} ₽"
    record = dict(title=title, description="Расписание, подробности и билеты — на Яндекс Афише.",
                  city="Москва", category=category, venue=venue, address=address,
                  price=price, price_text=price_text, source_name=SOURCE, source_url=url,
                  source_event_id=f"{eid}_{pid}", source_updated_at=None,
                  occurrences=[dict(id=f"t{int(start.timestamp())}",
                                    starts_at=start.astimezone(timezone.utc).isoformat(), timezone="Europe/Moscow")])
    return EventImport.model_validate(record).model_dump(), None


async def get_html(client, url):
    # One attempt. Blocked pages and rate limits require operator attention,
    # not rapid retries, proxies, copied browser cookies or CAPTCHA bypasses.
    async with client.stream("GET", url, headers={"Accept": "text/html"}, follow_redirects=False) as response:
        if "captcha" in response.headers.get("Location", "").lower():
            raise SyncError("yandex_captcha_required")
        if response.status_code != 200:
            raise SyncError(f"http_{response.status_code}")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 8_000_000:
                raise SyncError("page_too_large")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise SyncError("yandex_invalid_encoding") from None


class YandexAfisha:
    name, slug, complete_snapshot = SOURCE, "yandex", False
    normalize = staticmethod(normalize)

    def __init__(self, *, pages=5, load_page=None):
        if not 1 <= pages <= 10:
            raise ValueError("Yandex pages must be between 1 and 10")
        self.pages, self.load_page = pages, load_page

    async def fetch(self, client, since, until):
        items, seen = [], {}
        for page in range(1, self.pages + 1):
            url = ORIGIN + PATH + (f"?page={page}" if page > 1 else "")
            html = await self.load_page(url) if self.load_page else await get_html(client, url)
            batch, has_next = parse_page(html, page=page)
            for item in batch:
                key = (item["event"].get("id"), item["place"].get("id"),
                       (item["schedule"].get("regularity") or {}).get("singleShowtime"))
                # Recommendations may repeat on several pages. Count each schedule once.
                if key not in seen:
                    seen[key] = item
                    items.append(item)
            if not has_next or page == self.pages:
                return items, page
            await asyncio.sleep(2)
        return items, self.pages
