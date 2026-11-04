import json
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

from benchmark_loader import HybridPage, fingerprints, compare


class NavigationError(Exception):
    pass


def response(status=200, body=b'<html></html>', url='https://kudago.com/msk/'):
    return SimpleNamespace(status=status, body=body, url=url,
                           headers={'retry-after':'7'}, css=lambda selector: [1])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def make_page(self, http_response, browser_response=None):
        http=AsyncMock(return_value=http_response)
        browser=AsyncMock(return_value=browser_response or response())
        counters=Counter()
        return HybridPage(http,browser,NavigationError,counters),http,browser,counters

    async def test_http_success_does_not_launch_browser(self):
        page,http,browser,counters=self.make_page(response())
        result=await page.goto('https://kudago.com/msk/')
        self.assertEqual(await result.text(),'<html></html>')
        browser.assert_not_awaited()
        self.assertEqual(counters['http_requests'],1)

    async def test_forbidden_latches_browser_without_repeating_http_probe(self):
        page,http,browser,counters=self.make_page(response(403))
        await page.goto('https://kudago.com/msk/')
        await page.goto('https://kudago.com/msk/concerts/')
        self.assertEqual(http.await_count,1)
        self.assertEqual(browser.await_count,2)
        self.assertEqual(counters['fallback_http'],1)

    async def test_rate_limits_and_server_errors_remain_for_outer_retry_policy(self):
        for status in (429,500,503):
            page,http,browser,counters=self.make_page(response(status))
            result=await page.goto('https://kudago.com/msk/')
            self.assertEqual(result.status,status)
            self.assertEqual(result.headers['retry-after'],'7')
            browser.assert_not_awaited()

    async def test_invalid_utf8_falls_back_without_accepting_corrupt_text(self):
        page,http,browser,counters=self.make_page(response(body=b'\xff'))
        result=await page.goto('https://kudago.com/msk/')
        self.assertEqual(await result.text(),'<html></html>')
        self.assertEqual(counters['fallback_encoding'],1)

    async def test_final_browser_url_remains_visible_to_identity_checks(self):
        destination='https://afisha.yandex.ru/showcaptcha'
        page,http,browser,counters=self.make_page(response(302),response(url=destination))
        await page.goto('https://afisha.yandex.ru/moscow')
        self.assertEqual(page.url,destination)

    async def test_network_failure_has_one_browser_fallback(self):
        page,http,browser,counters=self.make_page(response())
        http.side_effect=TimeoutError()
        await page.goto('https://kudago.com/msk/')
        self.assertEqual(counters['http_network_errors'],1)
        self.assertEqual(browser.await_count,1)

    async def test_removed_page_is_not_retried_by_transport(self):
        page,http,browser,counters=self.make_page(response(410))
        self.assertEqual((await page.goto('https://kudago.com/msk/event/gone/')).status,410)
        browser.assert_not_awaited()

    async def test_foreign_origin_is_rejected_before_network(self):
        page,http,browser,counters=self.make_page(response())
        with self.assertRaises(NavigationError):
            await page.goto('https://other.example/')
        http.assert_not_awaited()
        browser.assert_not_awaited()


class IdentityTests(unittest.TestCase):
    def test_deduplicates_same_record_but_preserves_venues_and_sessions(self):
        record={'source_event_id':'film:venue1','occurrences':[{'starts_at':'2026-11-04T19:00:00+03:00'}],
                'period':{'date_from':'2026-11-04','date_to':'2026-12-04'}}
        other={**record,'source_event_id':'film:venue2'}
        result=fingerprints([record,record,other])
        self.assertEqual(len(result['events']),2)
        self.assertEqual(len(result['sessions']),2)
        self.assertEqual(len(result['periods']),2)

    def test_roundtrip_comparison_reports_losses_and_gains_separately(self):
        left=fingerprints([{'source_event_id':'one'},{'source_event_id':'two'}])
        right=fingerprints([{'source_event_id':'two'},{'source_event_id':'three'}])
        result=compare(json.loads(json.dumps(left)),json.loads(json.dumps(right)))
        self.assertEqual(result['events']['common'],1)
        self.assertEqual(result['events']['baseline_only'],1)
        self.assertEqual(result['events']['scrapling_only'],1)
        self.assertEqual(result['events']['baseline_retained'],0.5)
        self.assertIsNone(result['sessions']['baseline_retained'])

    def test_publication_guard_rejects_accidental_call(self):
        from benchmark_collector import no_publication
        with self.assertRaisesRegex(RuntimeError,'publication_forbidden'):
            no_publication([], 'unused')
