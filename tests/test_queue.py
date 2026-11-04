import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

from event_collector.collector_queue_client import DetailQueue,describe
from event_collector import yandex_collect as collector
from event_collector.source_http import SyncError


class QueueContracts(unittest.TestCase):
    def test_transport_retries_identical_operation_after_ambiguous_response(self):
        seen=[]
        def transport(request):
            p=json.loads(request.content);seen.append(request.content)
            self.assertNotIn(b'a'*64,request.content)
            if len(seen)==1:raise httpx.ReadTimeout('lost receipt')
            return httpx.Response(200,json={'status':'ok','run_key':p['run_key'],'request_id':p['request_id'],'action':p['action']})
        with tempfile.TemporaryDirectory() as folder,httpx.Client(transport=httpx.MockTransport(transport)) as c:
            key=Path(folder)/'key';key.write_text('a'*64)
            queue=DetailQueue(key,'123.1',client=c)
            with patch('time.sleep'):queue.request('errors',outcomes=[])
        self.assertEqual(len(seen),2)
        self.assertEqual(seen[0],seen[1])

    def test_invalid_receipt_and_redirect_are_rejected(self):
        for response in [httpx.Response(302,headers={'location':'https://evil.test/'}),httpx.Response(200,json={'status':'ok'})]:
            with tempfile.TemporaryDirectory() as folder,httpx.Client(transport=httpx.MockTransport(lambda r:response)) as c:
                key=Path(folder)/'key';key.write_text('a'*64)
                with self.assertRaises(SyncError):DetailQueue(key,'123.1',client=c).request('plan',candidates=[],budget=1)

    def test_descriptor_does_not_persist_listing_content(self):
        item={'event':{'id':'e','url':'/moscow/concert/e','title':'not persisted'},'_queue_category':'music','schedule':{'private':'not needed'}}
        self.assertEqual(describe(item),{'id':'e','url':'https://afisha.yandex.ru/moscow/concert/e','category':'music'})
        self.assertIsNone(describe({**item,'event':{'id':'e','url':'https://other.test/'}}))

    def collect(self,*,failures=0,empty=False):
        class BrowserError(Exception):pass
        page=SimpleNamespace(route=AsyncMock(),url='https://afisha.yandex.ru/moscow/listing')
        browser=SimpleNamespace(new_page=AsyncMock(return_value=page),close=AsyncMock())
        class Playwright:
            async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
            async def __aexit__(self,*args):pass
        tasks=[{'id':str(n),'url':f'https://afisha.yandex.ru/moscow/concert/{n}','category':'music'} for n in range(20)]
        queue=SimpleNamespace(plan=AsyncMock(return_value=tasks),summary={},record=Mock(),flush_errors=AsyncMock(),finish=Mock())
        def detail(html,eid):
            if int(eid)<failures:raise SyncError('yandex_detail_event_mismatch')
            return [] if empty else [{'confirmed':eid}]
        self.queue,self.browser=queue,browser
        with patch.dict('sys.modules',{'playwright.async_api':SimpleNamespace(async_playwright=Playwright,Error=BrowserError)}), \
             patch.dict(collector.LISTINGS,{'music':'/moscow/listing'},clear=True), \
             patch.object(collector,'load_browser_page',new=AsyncMock(return_value='html')), \
             patch.object(collector,'parse_page',return_value=([],False)), \
             patch.object(collector,'detail_items',side_effect=detail), \
             patch('asyncio.sleep',new_callable=AsyncMock),redirect_stdout(io.StringIO()):
            return asyncio.run(collector.browser_fetch(pages=1,channel='chromium',details=20,queue=queue))

    def test_backlog_is_processed_without_reappearing_in_current_listing(self):
        items,stats=self.collect()
        self.assertEqual(items,[{'confirmed':str(n)} for n in range(20)])
        self.assertEqual(stats['details'],20)
        self.queue.plan.assert_awaited_once_with([],20)
        self.queue.finish.assert_not_called()

    def test_empty_old_detail_never_uses_stale_listing_fallback(self):
        items,stats=self.collect(empty=True)
        self.assertEqual(items,[])
        self.queue.record.assert_any_call('0','empty')

    def test_error_backoff_is_reported_even_when_error_budget_stops_collection(self):
        with self.assertRaisesRegex(SyncError,'yandex_detail_event_mismatch'):self.collect(failures=5)
        self.queue.flush_errors.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.queue.finish.assert_not_called()

    def test_publication_failure_never_completes_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'events.json';out.write_text('[{"source_event_id":"event_venue"}]')
            key=Path(folder)/'key';key.write_text('a'*64)
            queue=Mock()
            with patch('sys.argv',['collector','--publish','--key-file',str(key),'--output',str(out)]), \
                 patch.dict('os.environ',{'COLLECTOR_RUN_KEY':'123.1'}), \
                 patch.object(collector,'DetailQueue',return_value=queue), \
                 patch.object(collector,'browser_fetch',new=AsyncMock(return_value=([],{}))), \
                 patch.object(collector,'export',return_value={'status':'ok'}), \
                 patch.object(collector,'publish',side_effect=SyncError('publish_http_503')),redirect_stdout(io.StringIO()),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit):collector.main()
            queue.finish.assert_not_called()
