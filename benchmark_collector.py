"""Paired full-budget dry runs. Production collectors and publication are unchanged."""
import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from benchmark_loader import playwright_factory, fingerprints, compare
from event_collector import kudago_collect, yandex_collect
from event_collector.collector_frontier import page_window
from event_collector.source_http import SyncError


def no_publication(*args,**kwargs):
    raise RuntimeError('benchmark_publication_forbidden')


async def collect_one(args, counters):
    from playwright import async_api
    as_of = datetime.fromisoformat(args.as_of)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return as_of.astimezone(tz) if tz else as_of.replace(tzinfo=None)
    module = kudago_collect if args.source=='kudago' else yandex_collect
    window = page_window(4,hour=args.band)
    with ExitStack() as stack:
        stack.enter_context(patch.object(module,'datetime',FixedDateTime))
        stack.enter_context(patch.object(module,'page_window',lambda pages: window))
        stack.enter_context(patch.object(yandex_collect,'publish',no_publication))
        stack.enter_context(patch.object(kudago_collect,'publish',no_publication))
        if args.mode=='scrapling':
            # All substitutions live in this isolated child process only.
            stack.enter_context(patch.object(async_api,'async_playwright',playwright_factory(async_api.Error,counters,args.executable_path)))
        else:
            original = async_api.Page.goto
            async def tracked(page,url,**kwargs):
                counters['browser_requests'] += 1
                try:
                    response = await original(page,url,**kwargs)
                    counters['browser_responses_'+str(response.status if response else 'missing')] += 1
                    return response
                except Exception:
                    counters['browser_errors'] += 1
                    raise
            stack.enter_context(patch.object(async_api.Page,'goto',tracked))
        async with asyncio.timeout(900):
            if args.source=='kudago':
                records, stats = await kudago_collect.collect(limit=300,days=60,pages=4,channel=args.channel)
            else:
                items, diagnostics = await yandex_collect.browser_fetch(pages=4,channel=args.channel,details=120,days=60)
                destination = Path(args.output).with_suffix('.events.json')
                stats = yandex_collect.export(items,destination,now=as_of,days=60)
                records = json.loads(destination.read_text(encoding='utf-8')) if stats['status']=='ok' else []
                stats['discovery'] = diagnostics
            return records, stats


def child(args):
    if os.environ.get('COLLECTOR_SECRET') or os.environ.get('COLLECTOR_KEY_FILE'):
        raise SystemExit('Benchmark must not receive ingestion credentials')
    logging.disable(logging.CRITICAL)
    counters = Counter()
    report = dict(source=args.source,mode=args.mode,band=args.band,as_of=args.as_of,published=False,
                  page_window=page_window(4,hour=args.band),limits={'kudago':300,'yandex_details':120,'days':60,'seconds':900},
                  versions={name:importlib.metadata.version(name) for name in ('playwright','scrapling','patchright')})
    started = time.monotonic()
    try:
        records, stats = asyncio.run(collect_one(args,counters))
        report.update(status='ok' if records else 'empty',stats=stats,fingerprints=fingerprints(records))
    except SyncError as exc:
        report.update(status='failed',error=str(exc))
    except TimeoutError:
        report.update(status='failed',error='benchmark_timeout')
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__)
    report.update(seconds=round(time.monotonic()-started,3),transport=dict(counters))
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=True,indent=2),encoding='utf-8')
    print('BENCHMARK_RUN:'+json.dumps({k:v for k,v in report.items() if k!='fingerprints'}),flush=True)
    return 0 if report['status']=='ok' else 2


def paired(args):
    if os.environ.get('COLLECTOR_SECRET') or os.environ.get('COLLECTOR_KEY_FILE'):
        raise SystemExit('Benchmark must not receive ingestion credentials')
    folder=Path('artifacts/full-benchmark')/args.source
    folder.mkdir(parents=True,exist_ok=True)
    pairs=[]
    for band in range(args.rounds):
        as_of=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order=['baseline','scrapling'] if band % 2==0 else ['scrapling','baseline']
        reports={}
        for mode in order:
            output=folder/f'band-{band}-{mode}.json'
            command=[sys.executable,__file__,'--source',args.source,'--mode',mode,'--band',str(band),
                     '--as-of',as_of,'--output',str(output),'--channel',args.channel]
            if args.executable_path:command+=['--executable-path',args.executable_path]
            print(json.dumps({'progress':'benchmark_start','source':args.source,'mode':mode,'band':band}),flush=True)
            try:
                subprocess.run(command,timeout=960,check=False)
                reports[mode]=json.loads(output.read_text(encoding='utf-8'))
            except (subprocess.TimeoutExpired,OSError,ValueError):
                reports[mode]={'source':args.source,'mode':mode,'status':'failed','error':'child_timeout_or_missing_report'}
        pair={'source':args.source,'band':band,'order':order,'as_of':as_of,'published':False,
              'runs':{m:{k:v for k,v in r.items() if k!='fingerprints'} for m,r in reports.items()}}
        if all(r['status']=='ok' for r in reports.values()):
            pair['comparison']=compare(reports['baseline']['fingerprints'],reports['scrapling']['fingerprints'])
            pair['scrapling_time_ratio']=round(reports['scrapling']['seconds']/reports['baseline']['seconds'],3)
        pairs.append(pair)
        (folder/'comparison.json').write_text(json.dumps(pairs,indent=2),encoding='utf-8')
        print('BENCHMARK_PAIR:'+json.dumps(pair),flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as stream:
            stream.write('Full-budget paired collection; no publication. Same runner/IP within each pair; live listings may change.\n\n```json\n')
            stream.write(json.dumps(pairs,indent=2));stream.write('\n```\n')
    return 0 if all(r['status']=='ok' for p in pairs for r in p['runs'].values()) else 2


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',choices=['kudago','yandex'],required=True)
    p.add_argument('--mode',choices=['baseline','scrapling'])
    p.add_argument('--rounds',type=int,choices=[1,2],default=2)
    p.add_argument('--band',type=int,choices=[0,1,2],default=0)
    p.add_argument('--as-of')
    p.add_argument('--output')
    p.add_argument('--channel',default='chromium')
    p.add_argument('--executable-path')
    args=p.parse_args()
    if args.mode and (not args.as_of or not args.output):p.error('Child requires --as-of and --output')
    return child(args) if args.mode else paired(args)


if __name__=='__main__':
    raise SystemExit(main())
