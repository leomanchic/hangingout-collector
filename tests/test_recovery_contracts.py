import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from event_collector.source_http import SyncError, retry_delay
from event_collector.yandex_collect import publish
from event_collector.collector_report import report_run
import run_collector
import asyncio
from types import SimpleNamespace
from event_collector.kudago_collect import collect


class RecoveryContracts(unittest.TestCase):
    def test_kudago_home_can_finish_its_normal_script_before_http_classification(self):
        class BrowserError(Exception): pass
        class Page:
            url=''
            waits=0
            async def goto(self,url,**kwargs):
                self.url=url
                return SimpleNamespace(status=403 if url.endswith('/msk/') else 200)
            async def wait_for_selector(self,*args,**kwargs): self.waits+=1
            async def content(self): return '<html></html>'
        page=Page()
        class Context:
            async def route(self,*args): pass
            async def new_page(self): return page
        class Browser:
            async def new_context(self): return Context()
            async def close(self): pass
        class Chromium:
            async def launch(self,**kwargs): return Browser()
        class Playwright:
            async def __aenter__(self): return SimpleNamespace(chromium=Chromium())
            async def __aexit__(self,*args): pass
        module=SimpleNamespace(async_playwright=Playwright,Error=BrowserError)
        async def pause(*args): pass
        with patch.dict('sys.modules',{'playwright.async_api':module}), patch('asyncio.sleep',side_effect=pause):
            records,stats=asyncio.run(collect(pages=1,channel='chromium'))
        self.assertEqual(page.waits,1)
        self.assertEqual(records,[])

    def test_genuine_kudago_home_forbidden_is_not_a_transient_browser_error(self):
        class BrowserError(Exception): pass
        class Page:
            url=''
            async def goto(self,url,**kwargs):
                self.url=url
                return SimpleNamespace(status=403)
            async def wait_for_selector(self,*args,**kwargs): raise BrowserError('no public cards')
        class Context:
            async def route(self,*args): pass
            async def new_page(self): return Page()
        class Browser:
            async def new_context(self): return Context()
            async def close(self): pass
        class Chromium:
            async def launch(self,**kwargs): return Browser()
        class Playwright:
            async def __aenter__(self): return SimpleNamespace(chromium=Chromium())
            async def __aexit__(self,*args): pass
        module=SimpleNamespace(async_playwright=Playwright,Error=BrowserError)
        with patch.dict('sys.modules',{'playwright.async_api':module}):
            with self.assertRaisesRegex(SyncError,'kudago_http_403'):
                asyncio.run(collect(pages=1,channel='chromium'))

    def test_lost_response_reuses_batch_bytes(self):
        calls=[]
        def transport(request):
            calls.append(request)
            if len(calls)==1:
                raise httpx.ReadTimeout('synthetic',request=request)
            return httpx.Response(200,json={'status':'ok','source':'yandex','imported':1,'added':1})
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*64,encoding='ascii')
            with httpx.Client(transport=httpx.MockTransport(transport)) as client, patch('time.sleep'):
                self.assertEqual(publish([{}],key,client=client)['imported'],1)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0].content,calls[1].content)

    def test_permanent_error_and_long_retry_after_stop(self):
        for code,headers in ((403,{}),(429,{'Retry-After':'3600'})):
            calls=[]
            def transport(request):
                calls.append(request)
                return httpx.Response(code,headers=headers)
            with tempfile.TemporaryDirectory() as folder:
                key=Path(folder)/'key';key.write_text('a'*64,encoding='ascii')
                with httpx.Client(transport=httpx.MockTransport(transport)) as client, patch('time.sleep') as sleep:
                    with self.assertRaises(SyncError): publish([{}],key,client=client)
                    sleep.assert_not_called()
            self.assertEqual(len(calls),1)

    def test_transient_errors_have_a_hard_cap(self):
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(503,headers={'Retry-After':'4'})
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*64,encoding='ascii')
            with httpx.Client(transport=httpx.MockTransport(transport)) as client, patch('time.sleep') as sleep:
                with self.assertRaisesRegex(SyncError,'publish_http_503'): publish([{}],key,client=client)
                self.assertEqual([c.args for c in sleep.call_args_list],[(4,),(4,)])
        self.assertEqual(len(calls),3)

    def test_retry_after_http_dates_and_invalid_values(self):
        self.assertIsNone(retry_delay({'Retry-After':'Wed, 01 Jan 2098 00:00:00 GMT'},0))
        self.assertIsNone(retry_delay({'Retry-After':'invalid'},0))
        self.assertEqual(retry_delay({'Retry-After':'Wed, 01 Jan 2020 00:00:00 GMT'},0),0)

    def test_late_recovery_does_not_start_browser(self):
        with patch.dict('os.environ',{'COLLECTOR_SOURCE':'kudago','COLLECTOR_SECRET':'a'*64,'GITHUB_RUN_ID':'12345','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'workflow_dispatch','COLLECTOR_RECOVERY_KEY':'b'*32}), patch('run_collector.report_run',return_value='skip'), patch('subprocess.run') as child, patch('builtins.print'):
            self.assertEqual(run_collector.main(),0)
            child.assert_not_called()

    def test_recovery_identifier_sent_in_signed_run_receipt(self):
        captured=[]
        def post(url,**kwargs):
            captured.append(json.loads(kwargs['content']))
            return httpx.Response(200,json={'status':'skip','run_key':'12345.1','source':'yandex'})
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*64,encoding='ascii')
            with patch.dict('os.environ',{'COLLECTOR_RECOVERY_KEY':'b'*32}), patch('httpx.post',side_effect=post):
                self.assertEqual(report_run(key,'12345.1','yandex','workflow_dispatch','running'),'skip')
        self.assertEqual(captured[0]['recovery_key'],'b'*32)
        self.assertNotIn('a'*64,json.dumps(captured))

    def test_primary_slot_identifier_is_signed_and_conflicting_keys_are_rejected(self):
        captured=[]
        def post(url,**kwargs):
            captured.append(json.loads(kwargs['content']))
            return httpx.Response(200,json={'status':'ok','run_key':'12345.1','source':'yandex'})
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*64,encoding='ascii')
            with patch.dict('os.environ',{'COLLECTOR_SCHEDULE_KEY':'c'*32,'COLLECTOR_RECOVERY_KEY':''}), patch('httpx.post',side_effect=post):
                self.assertEqual(report_run(key,'12345.1','yandex','workflow_dispatch','running'),'ok')
                with self.assertRaises(RuntimeError):
                    report_run(key,'12345.1','yandex','schedule','running')
                with patch.dict('os.environ',{'COLLECTOR_RECOVERY_KEY':'b'*32}):
                    with self.assertRaises(RuntimeError):
                        report_run(key,'12345.1','yandex','workflow_dispatch','running')
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0]['schedule_key'],'c'*32)
        self.assertNotIn('a'*64,json.dumps(captured))


if __name__=='__main__':
    unittest.main()
