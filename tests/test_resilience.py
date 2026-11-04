"""Offline failure injection, also exported to the public collector CI."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from event_collector import yandex_collect as collector
from event_collector.source_http import SyncError


class BrowserError(Exception):
    pass


class Page:
    def __init__(self, responses=(), destination=None):
        self.responses = iter(responses)
        self.destination = destination
        self.url = ''
        self.calls = 0

    async def route(self, *args):
        pass

    async def goto(self, url, **kwargs):
        self.calls += 1
        self.url = self.destination or url
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def response(body=b'<html>OK</html>', status=200, headers=None):
    return SimpleNamespace(status=status, headers=headers or {}, body=AsyncMock(return_value=body))


class ResilienceContracts(unittest.TestCase):
    def test_decode_retry_recovers_without_changing_text_or_logging_sensitive_content(self):
        page = Page([response(b'\xffbad-body-private', headers={'content-type': 'text/html; charset=utf-8',
                                                             'set-cookie': 'private-cookie'}),
                     response('Точный текст'.encode('utf-8'))])
        output = io.StringIO()
        with patch('asyncio.sleep', new_callable=AsyncMock) as sleep, redirect_stdout(output):
            result = asyncio.run(collector.load_browser_page(page, 'https://afisha.yandex.ru/moscow/test?token=private-query', BrowserError))
        self.assertEqual(result, 'Точный текст')
        self.assertEqual(page.calls, 2)
        sleep.assert_awaited_once_with(1)
        diagnostic = json.loads(output.getvalue())
        self.assertEqual(diagnostic['charset'], 'utf-8')
        self.assertEqual(diagnostic['invalid_byte_offset'], 0)
        self.assertNotIn('private', output.getvalue())

    def test_repeated_decode_failure_stays_strict_and_bounded(self):
        page = Page([response(b'\xff')] * 3)
        with patch('asyncio.sleep', new_callable=AsyncMock), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SyncError, '^yandex_invalid_encoding$'):
                asyncio.run(collector.load_browser_page(page, 'https://afisha.yandex.ru/moscow/test', BrowserError))
        self.assertEqual(page.calls, 2)

    def test_navigation_and_body_network_errors_share_three_attempt_cap(self):
        broken_body = response()
        broken_body.body.side_effect = BrowserError('private browser error')
        page = Page([BrowserError('network'), broken_body, BrowserError('network')])
        with patch('asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(SyncError, '^yandex_navigation_timeout_or_network$'):
                asyncio.run(collector.load_browser_page(page, 'https://afisha.yandex.ru/moscow/test', BrowserError))
        self.assertEqual(page.calls, 3)

    def test_captcha_redirect_forbidden_and_long_retry_after_are_never_ignored(self):
        for page, reason in (
            (Page([response()], destination='https://afisha.yandex.ru/showcaptcha?private'), 'yandex_captcha_required'),
            (Page([response()], destination='https://evil.test/'), 'yandex_unexpected_redirect'),
            (Page([response(status=403)]), 'http_403'),
            (Page([response(status=429, headers={'retry-after': '3600'})]), 'http_429_retry_later'),
        ):
            with self.subTest(reason=reason), patch('asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(SyncError, '^' + reason + '$'):
                    asyncio.run(collector.load_browser_page(page, 'https://afisha.yandex.ru/moscow/test', BrowserError))
                sleep.assert_not_awaited()
                with self.assertRaises(SyncError):
                    collector.quarantine_detail(SyncError(reason), 'event1', {}, 120)
            self.assertEqual(page.calls, 1)

    def test_identity_and_encoding_errors_use_one_budget(self):
        diagnostic = {}
        with redirect_stdout(io.StringIO()):
            for n in range(4):
                reason = 'yandex_invalid_encoding' if n % 2 else 'yandex_detail_event_mismatch'
                collector.quarantine_detail(SyncError(reason), str(n), diagnostic, 120)
            with self.assertRaisesRegex(SyncError, 'http_404'):
                collector.quarantine_detail(SyncError('http_404'), 'fifth', diagnostic, 120)
        self.assertEqual(diagnostic['detail_failures'], 5)
        self.assertEqual(diagnostic['detail_mismatches'], 2)
        self.assertEqual(diagnostic['detail_failure_reasons']['yandex_invalid_encoding'], 2)

    def test_small_sample_and_schema_failure_are_fatal(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SyncError, 'yandex_invalid_encoding'):
                collector.quarantine_detail(SyncError('yandex_invalid_encoding'), 'e', {}, 9)
            with self.assertRaises(SyncError):
                collector.checked_detail_items('<html>Changed schema</html>', 'e', {}, 120)

    def collect_with_failure(self, reason, *, malformed_urls=0, listing_failure=False):
        page = Page()
        browser = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
        class Playwright:
            async def __aenter__(self): return SimpleNamespace(chromium=chromium)
            async def __aexit__(self, *args): pass
        module = SimpleNamespace(async_playwright=Playwright, Error=BrowserError)
        candidates = [{'event': {'id': str(n), 'url': '/moscow/concert/' + str(n)}} for n in range(20)]
        for item in candidates[-malformed_urls:] if malformed_urls else []:
            item['event']['url'] = 'https://evil.test/'
        async def load(page, url, browser_error):
            if listing_failure or url.endswith('/0'):
                raise SyncError(reason)
            return 'valid'
        with patch.dict('sys.modules', {'playwright.async_api': module}), \
                patch.dict(collector.LISTINGS, {'music': '/moscow/listing'}, clear=True), \
                patch.object(collector, 'load_browser_page', side_effect=load), \
                patch.object(collector, 'parse_page', return_value=(candidates, False)), \
                patch.object(collector, 'normalize', return_value=(None, 'no_precise_single_session')), \
                patch.object(collector, 'detail_items', side_effect=lambda html, eid: [{'confirmed': eid}]), \
                patch('asyncio.sleep', new_callable=AsyncMock), redirect_stdout(io.StringIO()):
            return asyncio.run(collector.browser_fetch(pages=1, channel='chromium', details=20))

    def test_one_bad_detail_does_not_drop_good_cards_or_import_bad_fallback(self):
        for reason in ('yandex_invalid_encoding', 'http_404', 'http_410'):
            with self.subTest(reason=reason):
                items, diagnostics = self.collect_with_failure(reason)
                self.assertEqual(items, [{'confirmed': str(n)} for n in range(1, 20)])
                self.assertEqual(diagnostics['details'], 20)
                self.assertTrue(diagnostics['detail_partial'])
                self.assertEqual(diagnostics['skipped_detail_ids'], ['0'])

    def test_listing_failure_and_insufficient_actual_sample_stop_before_export(self):
        with self.assertRaisesRegex(SyncError, 'yandex_invalid_encoding'):
            self.collect_with_failure('yandex_invalid_encoding', listing_failure=True)
        with self.assertRaisesRegex(SyncError, 'yandex_detail_error_budget_exceeded'):
            self.collect_with_failure('yandex_invalid_encoding', malformed_urls=15)
        with self.assertRaisesRegex(SyncError, 'http_403'):
            self.collect_with_failure('http_403')


if __name__ == '__main__':
    unittest.main()
