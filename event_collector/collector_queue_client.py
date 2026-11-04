"""Signed durable queue protocol; never used by local dry runs or benchmarks."""
import asyncio
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
import uuid

import httpx

from .protocol import ENDPOINT, MAX_BYTES, signature
from .source_http import SyncError, retry_delay

QUEUE_ENDPOINT = '/api/internal/collector/queue/yandex'
CATEGORIES = ('music','theatre','art','walk','learning','cinema')
ERRORS = {'yandex_detail_event_mismatch','yandex_invalid_encoding','http_404','http_410'}


def valid_task(task):
    if not isinstance(task,dict) or set(task) != {'id','url','category'}:
        return False
    if not isinstance(task['id'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',task['id']):
        return False
    url=task['url']
    if not isinstance(url,str) or not 1 <= len(url) <= 600 or any(ord(c)<=32 for c in url):
        return False
    try:parsed=urlsplit(url)
    except ValueError:return False
    return (parsed.scheme=='https' and parsed.netloc=='afisha.yandex.ru'
            and parsed.path.startswith('/moscow/') and not parsed.query and not parsed.fragment
            and not any(p in {'.','..'} for p in parsed.path.split('/')) and task['category'] in CATEGORIES)


def describe(item):
    event=item.get('event') or {}
    task={'id':event.get('id'),'url':urljoin('https://afisha.yandex.ru',event.get('url') or ''),
          'category':item.get('_queue_category')}
    return task if valid_task(task) else None


class DetailQueue:
    def __init__(self,key_file,run_key,*,client=None):
        if not isinstance(run_key,str) or not re.fullmatch(r'[1-9][0-9]{0,19}\.[1-9][0-9]{0,3}',run_key):
            raise SyncError('queue_invalid_run_key')
        self.secret=Path(key_file).read_text(encoding='ascii').strip()
        if not re.fullmatch(r'[a-f0-9]{64}',self.secret):
            raise SyncError('invalid_collector_key_file')
        self.run_key,self.client=run_key,client
        self.tasks=[]
        self.outcomes={}
        self.summary={}

    def request(self,action,**data):
        payload={'operation':'collector_queue_v1','action':action,'source':'yandex',
                 'run_key':self.run_key,'request_id':uuid.uuid4().hex,**data}
        body=json.dumps(payload,separators=(',',':'),ensure_ascii=True).encode()
        if len(body)>MAX_BYTES:raise SyncError('queue_request_too_large')
        client=self.client or httpx.Client(timeout=25,follow_redirects=False)
        try:
            for attempt in range(3):
                stamp=str(int(time.time()))
                delay=2**attempt
                try:
                    r=client.post('https://hangingout.ru'+QUEUE_ENDPOINT,content=body,
                                  headers={'Content-Type':'application/json','X-Collector-Time':stamp,
                                           'X-Collector-Signature':signature(self.secret,stamp,body)},follow_redirects=False)
                    if r.status_code==200:
                        receipt=r.json()
                        if (receipt.get('status')!='ok' or receipt.get('run_key')!=self.run_key
                                or receipt.get('request_id')!=payload['request_id'] or receipt.get('action')!=action):
                            raise SyncError('queue_invalid_receipt')
                        return receipt
                    if r.status_code not in {429} and r.status_code<500:
                        raise SyncError(f'queue_http_{r.status_code}')
                    delay=retry_delay(r.headers,attempt)
                    if delay is None:raise SyncError('queue_retry_later')
                except httpx.TransportError:
                    pass
                except (ValueError,AttributeError):
                    raise SyncError('queue_invalid_receipt') from None
                if attempt<2:time.sleep(delay)
            raise SyncError('queue_unavailable')
        finally:
            if self.client is None:client.close()

    async def plan(self,candidates,budget):
        receipt=await asyncio.to_thread(self.request,'plan',candidates=candidates,budget=budget)
        tasks=receipt.get('tasks')
        if (not isinstance(tasks,list) or len(tasks)>budget or any(not valid_task(t) for t in tasks)
                or len({t['id'] for t in tasks})!=len(tasks)
                or not isinstance(receipt.get('lease_until'),int) or receipt['lease_until']<=time.time()):
            raise SyncError('queue_invalid_plan')
        self.tasks=tasks
        self.summary=receipt.get('queue',{})
        self.summary.update(selected_new=receipt['selected_new'],selected_refresh=receipt['selected_refresh'])
        return tasks

    def record(self,event_id,status,reason=None):
        self.outcomes[event_id]={'id':event_id,'status':status,**({'reason':reason} if reason else {})}

    async def flush_errors(self):
        errors=[o for o in self.outcomes.values() if o['status']=='error']
        if errors:await asyncio.to_thread(self.request,'errors',outcomes=errors)

    def finish(self,published_records):
        if set(self.outcomes)!={t['id'] for t in self.tasks}:
            raise SyncError('queue_incomplete_plan')
        receipt=self.request('finish',outcomes=list(self.outcomes.values()),published_records=published_records)
        self.summary.update(receipt['queue'],processed=len(self.outcomes))
        return self.summary
