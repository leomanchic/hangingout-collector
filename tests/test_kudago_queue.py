"""Offline browser/queue contracts, also run by standalone collector CI."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx

from event_collector import kudago_collect as collector
from event_collector.collector_queue_client import DetailQueue,describe_kudago,valid_task
from event_collector.source_http import SyncError


class KudaQueueContracts(unittest.TestCase):
    def test_descriptor_is_stable_and_source_bound(self):
        task=describe_kudago('https://kudago.com/msk/event/example/','music')
        self.assertTrue(valid_task(task,'kudago'))
        self.assertFalse(valid_task(task,'yandex'))
        for bad in ('https://evil.test/msk/event/example/','https://kudago.com/spb/event/example/',
                    'https://kudago.com/msk/place/example/','https://kudago.com/msk/event/example/?token=x'):
            self.assertIsNone(describe_kudago(bad,'music'))
        self.assertFalse(valid_task({**task,'id':'different'},'kudago'))
        self.assertEqual(task,describe_kudago(task['url'],'music'))

    def test_client_uses_kudago_path_and_refuses_yandex_plan(self):
        def respond(r):
            self.assertEqual(r.url.path,'/api/internal/collector/queue/kudago')
            p=json.loads(r.content);self.assertEqual(p['source'],'kudago')
            return httpx.Response(200,json={**{k:p[k] for k in ('run_key','request_id','action')},'status':'ok',
                'tasks':[{'id':'y','url':'https://afisha.yandex.ru/moscow/event/y','category':'music'}],
                'lease_until':int(datetime.now(timezone.utc).timestamp())+1200})
        with tempfile.TemporaryDirectory() as folder,httpx.Client(transport=httpx.MockTransport(respond)) as client:
            key=Path(folder)/'key';key.write_text('a'*64)
            q=DetailQueue(key,'123.1',client=client,source='kudago')
            with self.assertRaisesRegex(SyncError,'queue_invalid_plan'):asyncio.run(q.plan([],300))

    def collect(self,*,removed=False,blocked=None,diagnostics_path=None,starts_during=False):
        class BrowserError(Exception):pass
        tasks=[describe_kudago(f'https://kudago.com/msk/event/{name}/','music') for name in ('old-a','old-b')]
        page=SimpleNamespace(url='',content=AsyncMock(return_value='html'))
        async def goto(url,**kw):
            page.url=url
            code=403 if (blocked=='home' and url.endswith('/msk/') or blocked=='detail' and url==tasks[1]['url']) else (410 if removed and '/event/' in url else 200)
            page.code=code
            return SimpleNamespace(status=code,headers={},text=AsyncMock(return_value='html'))
        async def selector(*args,**kw):
            if page.code==403:raise BrowserError('private browser details')
        page.goto=AsyncMock(side_effect=goto);page.wait_for_selector=selector
        context=SimpleNamespace(route=AsyncMock(),new_page=AsyncMock(return_value=page))
        browser=SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
        class Playwright:
            async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
            async def __aexit__(self,*args):pass
        queue=SimpleNamespace(plan=AsyncMock(return_value=tasks),summary={},record=Mock(),finish=Mock())
        def normalize(html,url,*args):
            delay = timedelta(seconds=10) if starts_during else timedelta(days=1)
            return {'source_event_id':url,'category':'music','occurrences':[{'id':'1','starts_at':(datetime.now(timezone.utc)+delay).isoformat()}]},None
        self.queue,self.browser,self.page=queue,browser,page
        self.output=io.StringIO()
        with patch.dict('sys.modules',{'playwright.async_api':SimpleNamespace(async_playwright=Playwright,Error=BrowserError)}), \
             patch.object(collector,'LISTINGS',('/msk/concerts/',)),patch.object(collector,'listing_links',return_value=[]), \
             patch.object(collector,'normalize',side_effect=normalize),patch('asyncio.sleep',new_callable=AsyncMock),redirect_stdout(self.output):
            return asyncio.run(collector.collect(pages=1,limit=300,channel='chromium',queue=queue,diagnostics_path=diagnostics_path))

    def test_skip_report_survives_later_failure_and_complete_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'skips.json'
            with self.assertRaisesRegex(SyncError,'kudago_http_403'):
                self.collect(removed=True,blocked='detail',diagnostics_path=path)
            partial=json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(partial['collection_complete'])
            self.assertEqual(partial['skipped_cards'],[{'url':'https://kudago.com/msk/event/old-a/',
                'category':'music','reason':'removed_card'}])
            self.collect(removed=True,diagnostics_path=path)
            final=json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(final['collection_complete'])
            self.assertEqual(final['reasons'],{'removed_card':2})

    def test_started_during_collection_is_empty_in_report_and_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'skips.json'
            records,stats=self.collect(starts_during=True,diagnostics_path=path)
            self.assertEqual(records,[])
            self.assertEqual(stats['skipped'],{'started_during_collection':2})
            final=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(final['accepted_records'],0)
            self.assertEqual(final['reasons'],{'started_during_collection':2})
            self.assertTrue(all(call.args[1]=='empty' for call in self.queue.record.call_args_list[-2:]))

    def test_saved_urls_are_fetched_when_absent_from_current_listing(self):
        records,stats=self.collect()
        self.assertEqual(len(records),2)
        self.assertEqual(stats['fetched'],2)
        self.queue.plan.assert_awaited_once_with([],300)
        self.assertEqual(self.queue.record.call_count,2)
        self.queue.finish.assert_not_called()

    def test_removed_cards_are_acknowledged_as_empty(self):
        records,stats=self.collect(removed=True)
        self.assertEqual(records,[])
        self.assertEqual(stats['skipped'],{'removed_card':2})
        self.assertTrue(all(call.args[1]=='empty' for call in self.queue.record.call_args_list))

    def test_access_refusal_is_fatal_and_diagnostics_do_not_expose_urls(self):
        for stage in ('home','detail'):
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(SyncError,'^kudago_http_403$'):self.collect(blocked=stage)
                self.browser.close.assert_awaited_once()
                self.queue.finish.assert_not_called()
                if stage=='home':self.queue.plan.assert_not_awaited()
                else:self.assertEqual(self.queue.record.call_count,1)
                last=json.loads(self.output.getvalue().splitlines()[-1])
                self.assertEqual(last['phase'],'home' if stage=='home' else 'details')
                self.assertNotIn('https://',self.output.getvalue())

    def test_publication_failure_does_not_advance_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('a'*64)
            queue=Mock()
            with patch('sys.argv',['collector','--publish','--key-file',str(key),'--output',str(Path(folder)/'events.json')]), \
                 patch.dict('os.environ',{'COLLECTOR_RUN_KEY':'123.1'}),patch.object(collector,'DetailQueue',return_value=queue), \
                 patch.object(collector,'collect',new=AsyncMock(return_value=([{'source_event_id':'e'}],{}))), \
                 patch.object(collector,'publish',side_effect=SyncError('publish_http_503')),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit):collector.main()
            queue.finish.assert_not_called()

    def test_no_due_tasks_is_idle_but_checked_empty_cards_are_not(self):
        from event_collector import yandex_collect
        for module in (collector,yandex_collect):
            for tasks in ([],[{'id':'checked-but-empty'}]):
                with self.subTest(source=module.__name__,tasks=tasks),tempfile.TemporaryDirectory() as folder:
                    key=Path(folder)/'key';key.write_text('a'*64)
                    queue=SimpleNamespace(tasks=tasks,finish=Mock(return_value={'pending':0,'processed':len(tasks)}))
                    output=io.StringIO()
                    fetch='collect' if module is collector else 'browser_fetch'
                    with patch('sys.argv',['collector','--publish','--key-file',str(key),'--output',str(Path(folder)/'events.json')]), \
                         patch.dict('os.environ',{'COLLECTOR_RUN_KEY':'123.1'}),patch.object(module,'DetailQueue',return_value=queue), \
                         patch.object(module,fetch,new=AsyncMock(return_value=([],{}))),patch.object(module,'publish') as publish,redirect_stdout(output):
                        if tasks:
                            with self.assertRaises(SystemExit) as error:module.main()
                            self.assertEqual(error.exception.code,2)
                        else:module.main()
                    publish.assert_not_called()
                    queue.finish.assert_called_once_with(0)
                    self.assertEqual(json.loads(output.getvalue())['status'],'empty_preserved' if tasks else 'idle')


if __name__=='__main__':unittest.main()
