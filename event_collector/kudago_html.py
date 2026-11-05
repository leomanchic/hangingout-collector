"""Read public KudaGO cards without turning visiting windows into showtimes."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from html import unescape
import json
import re
from urllib.parse import urljoin, urlsplit, parse_qs

from .common import SOURCE, ZONE
from .models import EventImport
from .source_http import SyncError

ORIGIN = "https://kudago.com"
LISTINGS = ("/msk/concerts/", "/msk/theater/", "/msk/knowledge/",
            "/msk/exhibitions/", "/msk/excursions/", "/msk/kino/")
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
VOID = set("area base br col embed hr img input link meta param source track wbr".split())


def location_fields(location):
    if not isinstance(location, dict): return None
    geo = location.get('geo')
    geo = [geo] if isinstance(geo, dict) else geo
    if not isinstance(geo, list) or len(geo) != 1 or not isinstance(geo[0], dict): return None
    try:
        lat, lon = float(geo[0]['latitude']), float(geo[0]['longitude'])
        # Observed KudaGO JSON-LD has transposed labels. Correct only when the
        # same venue's static-map point explicitly corroborates lon,lat order.
        if not (54 < lat < 57 and 35 < lon < 40):
            url = urlsplit(unescape(location.get('hasMap', '')))
            point = parse_qs(url.query).get('pt', [''])[0].split(',')
            if url.hostname != 'static-maps.yandex.ru' or len(point) < 2: return None
            if abs(float(point[0])-lat)>1e-6 or abs(float(point[1])-lon)>1e-6: return None
            lat, lon = lon, lat
        if 54 < lat < 57 and 35 < lon < 40:
            return dict(latitude=lat, longitude=lon, metro=[])
    except (ValueError, TypeError, KeyError):
        pass
    return None


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def has(self, name):
        return name in self.attrs.get("class", "").split()

    def text(self):
        def parts(node):
            for child in node.children:
                if isinstance(child, str):
                    yield child
                elif child.tag not in {"script", "style"}:
                    yield from parts(child)
        return " ".join(" ".join(parts(self)).split())


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        if len(html.encode("utf-8")) > 8_000_000:
            raise SyncError("page_too_large")
        self.root = Node()
        self.stack = [self.root]
        self.count = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100_000 or len(self.stack) > 200:
            raise SyncError("html_structure_limit")
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def event_url(url):
    parsed = urlsplit(urljoin(ORIGIN, url))
    if (parsed.scheme == "https" and parsed.netloc == "kudago.com"
            and re.fullmatch(r"/msk/event/[a-z0-9-]+/", parsed.path)
            and not parsed.query and not parsed.fragment):
        return ORIGIN + parsed.path
    return None


def card_destination(url):
    """A moved event stays importable; a card converted into a place is skipped."""
    canonical = event_url(url)
    if canonical:
        return canonical
    parsed = urlsplit(url)
    if (parsed.scheme == 'https' and parsed.netloc == 'kudago.com' and not parsed.query and not parsed.fragment
            and (parsed.path in {'/msk/', *LISTINGS} or re.fullmatch(r'/msk/(?:place|list)/[a-z0-9-]+/', parsed.path))):
        return None
    raise SyncError('kudago_unexpected_redirect')


def listing_links(html):
    return list(dict.fromkeys(url for node in Document(html).root.nodes()
                             if node.tag == "a" and node.has("post-title-link")
                             and (url := event_url(node.attrs.get("href", "")))))


def overnight_start(day, clock, category):
    """Reduce an explicit one-night event to its start, never expand opening hours.

    Observed on /msk/event/rocklike/: 28–29 ноября, 23:30–3:00.
    Same-month, adjacent calendar days and a clock crossing midnight are required.
    Recurring ranges, weekdays and visiting windows remain unsupported here.
    """
    if category not in {'music', 'theatre', 'cinema'}:
        return None
    dates = re.fullmatch(r'(\d{1,2})\s*[–—-]\s*(\d{1,2}) (' + '|'.join(MONTHS) + r')(?: (20\d{2}))?', day)
    clocks = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)\s*[–—-]\s*([01]?\d|2[0-3]):([0-5]\d)', clock)
    if not dates or not clocks or int(dates[2]) != int(dates[1]) + 1:
        return None
    start_minutes = int(clocks[1]) * 60 + int(clocks[2])
    end_minutes = int(clocks[3]) * 60 + int(clocks[4])
    # A bounded evening-to-morning interval distinguishes a show from daily hours.
    if start_minutes < 18 * 60 or end_minutes > 8 * 60 or start_minutes <= end_minutes:
        return None
    return (f'{int(dates[1])} {dates[3]}' + (f' {dates[4]}' if dates[4] else ''),
            f'{int(clocks[1]):02}:{clocks[2]}', int(dates[2]),
            (24 * 60 - start_minutes + end_minutes))


def normalize(html, url, since, until):
    if not event_url(url):
        raise SyncError("invalid_kudago_url")
    nodes = list(Document(html).root.nodes())
    structured = []
    for node in nodes:
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                value = json.loads("".join(c for c in node.children if isinstance(c, str)))
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict) and value.get("@type") == "Event":
                structured.append(value)
    if len(structured) != 1:
        return None, "no_event_metadata"
    item = structured[0]
    if event_url(item.get("url", "")) != url:
        return None, "different_canonical_url"
    if not str(item.get("eventStatus", "")).endswith("/EventScheduled"):
        return None, "not_scheduled"
    ids = {n.attrs.get("data-id", "") for n in nodes if n.attrs.get("data-type-name") == "event"
           and "post-fav-top" in n.attrs.get("data-classnames", "").split()}
    if len(ids) != 1 or not next(iter(ids)).isdigit():
        return None, "no_stable_event_id"
    crumbs = {urlsplit(n.attrs.get("href", "")).path for n in nodes
              if n.tag == "a" and n.has("crumbs-list-item-link")}
    categories = (("/msk/theater/", "theatre"), ("/msk/cinema/", "cinema"), ("/msk/kino/", "cinema"),
                  ("/msk/concerts/", "music"), ("/msk/exhibitions/", "art"),
                  ("/msk/knowledge/", "learning"), ("/msk/tours/", "walk"),
                  ("/msk/excursions/", "walk"), ("/msk/events/tour/", "walk"))
    category = next((target for path, target in categories if path in crumbs), None)
    if category is None:
        return None, "other_category"
    # Restrict venue extraction to the event details, not advertising/recommendations.
    details = [n for n in nodes if n.has("converting-details")]
    detail_nodes = [n for section in details for n in section.nodes()]
    venues = {n.text() for n in detail_nodes if n.tag == "a" and n.has("post-big-detail-description")
              and urlsplit(n.attrs.get("href", "")).path.startswith("/msk/place/")}
    for block in detail_nodes:
        if block.has("post-big-detail") and any(n.tag == "big" and n.text() == "Место проведения" for n in block.nodes()):
            venues.update(n.text() for n in block.nodes() if n.has("post-big-detail-stub"))
    addresses = {n.text() for n in detail_nodes if n.has("addressItem")}
    location = item.get("location") or {}
    if not venues and isinstance(location, dict) and location.get("@type") == "LocalBusiness":
        venues = {location.get("name", "")}
    if not addresses and isinstance(location, dict):
        postal = location.get("address", [])
        if isinstance(postal, dict):
            postal = [postal]
        if isinstance(postal, list):
            addresses = {a.get("streetAddress", "") for a in postal if isinstance(a, dict)
                         and a.get("addressLocality") == "Москва"}
    venues.discard(""); addresses.discard("")
    if len(venues) != 1 or len(addresses) != 1 or next(iter(venues)).lower() in {"москва", "онлайн"}:
        return None, "no_unambiguous_venue"
    try:
        lower = datetime.fromisoformat(item["startDate"])
        upper = datetime.fromisoformat(item.get("endDate") or item["startDate"])
        lower = lower.replace(tzinfo=ZONE) if lower.tzinfo is None else lower.astimezone(ZONE)
        upper = upper.replace(tzinfo=ZONE) if upper.tzinfo is None else upper.astimezone(ZONE)
    except (ValueError, TypeError, KeyError):
        return None, "no_schedule_year_bounds"
    occurrences = {}
    for table in nodes:
        if not table.has("post-big-details-schedule"):
            continue
        for row in table.nodes():
            if row.tag != "tr":
                continue
            cells = [n.text().lower() for n in row.nodes() if n.tag == "td"]
            if len(cells) != 2:
                continue
            overnight = overnight_start(*cells, category)
            if overnight:
                cells = list(overnight[:2])
            day = re.fullmatch(r"(\d{1,2}) (" + "|".join(MONTHS) + r")(?: (20\d{2}))?", cells[0])
            clock = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", cells[1])
            if not day or not clock:
                continue
            years = [int(day[3])] if day[3] else range(since.astimezone(ZONE).year, until.astimezone(ZONE).year + 1)
            candidates = []
            for year in years:
                try:
                    start = datetime(year, MONTHS.index(day[2]) + 1, int(day[1]), int(clock[1]), int(clock[2]), tzinfo=ZONE)
                except ValueError:
                    continue
                if overnight:
                    end = start + timedelta(minutes=overnight[3])
                    if end.day != overnight[2] or end.month != start.month or end > upper:
                        continue
                if since < start < until and lower <= start <= upper:
                    candidates.append(start)
            if len(candidates) == 1:
                start = candidates[0]
                key = f"t{int(start.timestamp())}"
                occurrences[key] = dict(id=key, starts_at=start.astimezone(timezone.utc).isoformat(), timezone="Europe/Moscow")
    period = None
    if not occurrences:
        # A source date span is a visiting window, never a midnight showtime.
        # Reject unbounded/permanent placeholders instead of inventing an end.
        if (category in {'art','walk','learning'} and 1 <= (upper.date()-lower.date()).days <= 1096
                and upper.date() >= since.astimezone(ZONE).date() and lower.date() <= until.astimezone(ZONE).date()):
            period = dict(date_from=lower.date().isoformat(),date_to=upper.date().isoformat(),
                          timezone='Europe/Moscow',precision='estimated')
        else:
            return None, "no_precise_upcoming_sessions"
    prices = [n.text() for n in nodes if n.attrs.get("id") == "event-price" and n.text()]
    price_text = prices[0] if prices else ""
    price = None
    if re.fullmatch(r"бесплатно[!.]?", price_text.lower()):
        price = 0
    else:
        amount = re.fullmatch(r"(?:от )?([\d ]+)\s*(?:₽|руб\.?|рублей)?", price_text)
        if amount and 0 < int(amount[1].replace(" ", "")) <= 100_000_000:
            price = int(amount[1].replace(" ", ""))
    record = dict(title=item.get("name", ""), description="Расписание, условия посещения и билеты — на странице события КудаGO.",
                  city="Москва", category=category, venue=next(iter(venues)), address=next(iter(addresses)),
                  price=price, price_text=price_text, source_name=SOURCE, source_url=url,
                  source_event_id=next(iter(ids)), source_updated_at=None,
                  source_location=location_fields(location),
                  occurrences=sorted(occurrences.values(), key=lambda o: o["starts_at"])[:100],period=period)
    return EventImport.model_validate(record).model_dump(), None
