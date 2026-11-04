"""Experimental transport shim, imported only by the read-only benchmark."""
from types import SimpleNamespace
from urllib.parse import urlsplit


class Response:
    def __init__(self, value):
        self.value = value
        self.status = value.status
        self.headers = value.headers

    async def body(self):
        return self.value.body

    async def text(self):
        return self.value.body.decode('utf-8')


class HybridPage:
    def __init__(self, http_get, browser_fetch, error_type, counters):
        self.http_get, self.browser_fetch = http_get, browser_fetch
        self.error_type, self.counters = error_type, counters
        self.browser_hosts = set()
        self.url = ''
        self.response = None

    async def route(self, *args):
        # The real browser receives the identical resource filter in page_setup.
        pass

    def count(self, method, response):
        self.counters[method+'_responses_'+str(response.status)] += 1

    async def goto(self, url, **kwargs):
        target = urlsplit(url)
        if target.scheme != 'https' or target.netloc not in {'afisha.yandex.ru','kudago.com'}:
            raise self.error_type('benchmark_origin_rejected')
        result = None
        if target.netloc not in self.browser_hosts:
            self.counters['http_requests'] += 1
            try:
                result = await self.http_get(url)
                self.count('http',result)
            except Exception:
                self.counters['http_network_errors'] += 1
            if result is not None:
                # Do not evade a rate limit or multiply the collector's retries.
                if result.status == 429 or result.status >= 500:
                    self.url, self.response = result.url, result
                    return Response(result)
                invalid_encoding = False
                if result.status == 200:
                    try: result.body.decode('utf-8')
                    except UnicodeDecodeError: invalid_encoding = True
                fallback = (result.status in {301,302,303,307,308,403} or invalid_encoding
                            or 'captcha' in urlsplit(result.url).path.lower())
                if not fallback:
                    self.url, self.response = result.url, result
                    return Response(result)
                self.counters['fallback_encoding' if invalid_encoding else 'fallback_http'] += 1
                if result.status == 403:
                    # One HTTP probe, not one forbidden request per card.
                    self.browser_hosts.add(target.netloc)
        self.counters['browser_requests'] += 1
        try:
            result = await self.browser_fetch(url)
        except Exception:
            self.counters['browser_errors'] += 1
            raise self.error_type('benchmark_browser_navigation_failed') from None
        self.count('browser',result)
        self.url, self.response = result.url, result
        return Response(result)

    async def content(self):
        return self.response.body.decode('utf-8')

    async def wait_for_selector(self, selector, **kwargs):
        if not self.response.css(selector):
            raise self.error_type('benchmark_selector_missing')


class ScraplingBrowser:
    def __init__(self, error_type, counters, executable_path=None):
        self.error_type, self.counters = error_type, counters
        self.executable_path = executable_path
        self.http_manager = self.http = self.browser_manager = self.browser = None

    async def open(self):
        from scrapling.fetchers import FetcherSession
        self.http_manager = FetcherSession(timeout=25,retries=1,follow_redirects=False)
        self.http = await self.http_manager.__aenter__()
        self.page = HybridPage(self.http.get,self.fetch_browser,self.error_type,self.counters)
        return self

    async def fetch_browser(self, url):
        if self.browser is None:
            from scrapling.fetchers import AsyncStealthySession
            options = dict(headless=True,solve_cloudflare=False,timeout=25000,retries=1,google_search=False)
            if self.executable_path: options['executable_path'] = self.executable_path
            self.browser_manager = AsyncStealthySession(**options)
            self.browser = await self.browser_manager.__aenter__()
        async def setup(page):
            await page.route('**/*',lambda route: route.abort() if route.request.resource_type in {'image','media','font'} else route.continue_())
        options = dict(page_setup=setup,wait=0,load_dom=True,network_idle=False)
        if url == 'https://kudago.com/msk/':
            options.update(wait_selector='a.post-title-link',wait_selector_state='attached')
        return await self.browser.fetch(url,**options)

    async def new_context(self): return self
    async def new_page(self): return self.page
    async def route(self,*args): pass

    async def close(self):
        if self.browser_manager:
            await self.browser_manager.__aexit__(None,None,None)
            self.browser_manager = self.browser = None
        if self.http_manager:
            await self.http_manager.__aexit__(None,None,None)
            self.http_manager = self.http = None


def playwright_factory(error_type, counters, executable_path=None):
    class Context:
        async def __aenter__(self):
            self.browser = ScraplingBrowser(error_type,counters,executable_path)
            async def launch(**kwargs): return await self.browser.open()
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        async def __aexit__(self,*args):
            await self.browser.close()
    return Context


def fingerprints(records):
    """Stable event/venue and schedule identity; no source HTML or browser state."""
    events, sessions, periods = set(), set(), set()
    for record in records:
        eid = record['source_event_id']
        events.add(eid)
        for occurrence in record.get('occurrences',[]):
            sessions.add((eid,occurrence['starts_at']))
        if record.get('period'):
            p = record['period']
            periods.add((eid,p['date_from'],p['date_to']))
    return {'events':sorted(events),'sessions':sorted(sessions),'periods':sorted(periods)}


def compare(left,right):
    result = {}
    for field in ('events','sessions','periods'):
        a = {tuple(v) if isinstance(v,list) else v for v in left[field]}
        b = {tuple(v) if isinstance(v,list) else v for v in right[field]}
        result[field] = dict(common=len(a & b),baseline_only=len(a-b),scrapling_only=len(b-a),
                             baseline_retained=round(len(a & b)/len(a),4) if a else None,
                             baseline_only_sample=sorted(a-b)[:10],scrapling_only_sample=sorted(b-a)[:10])
    return result
