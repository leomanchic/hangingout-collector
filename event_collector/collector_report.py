"""Signed CI run receipts. Only non-secret counters are sent to a fixed host."""
import json
import os
import re
import time
from pathlib import Path
import httpx
from .protocol import ENDPOINT, MAX_BYTES, signature
from .source_http import retry_delay


def report_run(key_file, run_key, source, trigger, status, metrics=None):
    secret = Path(key_file).read_text(encoding='ascii').strip()
    payload = dict(operation='collector_run_v1',run_key=run_key,source=source,
                   trigger=trigger,status=status,metrics=metrics or {})
    recovery_key = os.environ.get('COLLECTOR_RECOVERY_KEY', '')
    schedule_key = os.environ.get('COLLECTOR_SCHEDULE_KEY', '')
    if recovery_key and schedule_key:
        raise RuntimeError('Conflicting dispatch identifiers')
    if recovery_key:
        if trigger != 'workflow_dispatch' or not re.fullmatch(r'[a-f0-9]{32}', recovery_key):
            raise RuntimeError('Invalid recovery identifier')
        payload['recovery_key'] = recovery_key
    if schedule_key:
        if trigger != 'workflow_dispatch' or not re.fullmatch(r'[a-f0-9]{32}',schedule_key):
            raise RuntimeError('Invalid schedule identifier')
        payload['schedule_key'] = schedule_key
    body = json.dumps(payload,separators=(',',':')).encode()
    for attempt in range(3):
        stamp = str(int(time.time()))
        delay = 2 ** attempt
        try:
            r = httpx.post('https://hangingout.ru/api/internal/collector/run', content=body,
                           headers={'Content-Type':'application/json','X-Collector-Time':stamp,
                                    'X-Collector-Signature':signature(secret,stamp,body)},timeout=25,follow_redirects=False)
            if r.status_code == 200:
                receipt = r.json()
                if receipt.get('run_key') == run_key and receipt.get('source') == source and receipt.get('status') in {'ok','skip'}:
                    return receipt['status']
            if r.status_code < 500 and r.status_code not in (200, 429):
                break
            if r.status_code == 429 or r.status_code >= 500:
                delay = retry_delay(r.headers, attempt)
                if delay is None:
                    break
        except (httpx.HTTPError,ValueError,TypeError,AttributeError):
            pass
        if attempt < 2:
            time.sleep(delay)
    raise RuntimeError('Run receipt was not acknowledged')
