"""Shared offline regression tests exported to the standalone Actions collector."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from event_collector.collector_frontier import interleave, page_window
from event_collector.yandex_afisha import detail_items, normalize
from event_collector.yandex_collect import export, publish
from event_collector.source_http import SyncError

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def state():
    return {'ROOT_QUERY': {'event({"id":"event1"})': {'__ref':'Event:1'},
            'eventScheduleOther({"id":"event1"})': {'byDate': [
                {'date':'2026-10-24', 'sessions': [
                    {'place':{'__ref':'Place:1'}, 'session':{'date':'2026-10-24','datetime':'2026-10-24T19:00:00','ticket':{'__ref':'Ticket:1'}}},
                    {'place':{'__ref':'Place:1'}, 'session':{'date':'2026-10-24','datetime':'2026-10-24T21:00:00','ticket':{'__ref':'Ticket:1'}}}
                ]}]}},
            'Event:1': {'id':'event1','title':'Test theatre','type':{'code':'theatre'},'url':'/moscow/theatre/event1'},
            'Place:1': {'id':'place1','title':'Hall','address':'Street, 1','city':{'id':'moscow'}},
            'Ticket:1': {'price':{'currency':'rub','min':125000},'saleStatus':'available'}}


def html(data):
    return '<script>window["__APOLLO_STATE__"] = ' + json.dumps(data) + ';</script>'


class ExpansionContracts(unittest.TestCase):
    def test_rotation_covers_ten_pages_without_starving_front_page(self):
        windows = [page_window(4,hour=i) for i in range(3)]
        self.assertEqual(windows, [[1,2,3,4],[1,5,6,7],[1,8,9,10]])
        self.assertEqual(set(sum(windows,[])),set(range(1,11)))
        self.assertEqual(interleave([['a','b','c'],['a','x'],['y','z']],limit=5),['a','y','b','x','z'])

    def test_explicit_detail_sessions_are_merged_not_multiplied_by_date(self):
        items = detail_items(html(state()),'event1')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.json'
            stats=export(items+items,path,now=NOW)
            records=json.loads(path.read_text(encoding='utf8'))
        self.assertEqual(stats['imported'],1)
        self.assertEqual(stats['sessions'],2)
        self.assertEqual(records[0]['occurrences'][0]['starts_at'],'2026-10-24T16:00:00+00:00')
        self.assertEqual(records[0]['price'],1250)

    def test_wrong_event_and_date_mismatch_cannot_import_recommendations(self):
        data=state()
        data['ROOT_QUERY']['eventScheduleOther({"id":"another"})'] = data['ROOT_QUERY'].pop('eventScheduleOther({"id":"event1"})')
        self.assertEqual(detail_items(html(data),'event1'),[])
        with self.assertRaises(SyncError):
            detail_items(html(data),'another')
        data=state()
        for slot in data['ROOT_QUERY']['eventScheduleOther({"id":"event1"})']['byDate'][0]['sessions']:
            slot['session']['date']='2026-10-25'
        self.assertEqual(detail_items(html(data),'event1'),[])

    def test_cinema_slots_stay_with_their_venue_and_city(self):
        data=state()
        data['ROOT_QUERY'].pop('eventScheduleOther({"id":"event1"})')
        data['Event:1']['type']['code']='cinema'
        data['Place:2']=dict(data['Place:1'],id='place2',title='Other city',city={'id':'spb'})
        slot={'date':'2026-10-24','datetime':'2026-10-24T19:00:00','ticket':{'__ref':'Ticket:1'}}
        data['ROOT_QUERY']['eventScheduleCinema:{"id":"event1","date":"2026-10-24"}'] = {
            'items':[{'date':'2026-10-24','place':{'__ref':'Place:'+str(n)},'schedule':[{'sessions':[slot]}]} for n in (1,2)]}
        results=[normalize(i,NOW,NOW+timedelta(days=30)) for i in detail_items(html(data),'event1')]
        self.assertEqual(sum(r is not None for r,_ in results),1)
        self.assertEqual([reason for _,reason in results],[None,'other_or_unknown_city'])

    def test_large_publication_splits_by_count_and_bytes(self):
        for records in ([{'n':i} for i in range(250)], [{'n':i,'text':'x'*5000} for i in range(90)]):
            calls=[]
            def transport(request):
                body=json.loads(request.content); calls.append(body)
                self.assertLessEqual(len(request.content),262144)
                self.assertLessEqual(len(body['events']),120)
                return httpx.Response(200,json={'status':'ok','source':'yandex','imported':len(body['events'])})
            with tempfile.TemporaryDirectory() as directory:
                key=Path(directory)/'key';key.write_text('a'*64)
                with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                    result=publish(records,key,client=client)
            self.assertEqual(result['imported'],len(records))
            self.assertGreater(result['batches'],1)
            self.assertEqual([r for b in calls for r in b['events']],records)
            self.assertEqual(len(set(b['batch_id'] for b in calls)),len(calls))

    def test_partial_publication_reports_committed_events(self):
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(200,json={'status':'ok','source':'yandex','imported':120}) if len(calls)==1 else httpx.Response(503)
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/'key';key.write_text('a'*64)
            with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                with self.assertRaisesRegex(SyncError,'committed_batches=1; committed_events=120'):
                    publish([{'n':i} for i in range(121)],key,client=client)


if __name__ == '__main__':
    unittest.main()
