"""CI entry point. The ingestion key is never committed, printed or put in arguments."""
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from event_collector.collector_report import report_run


def main():
    source = os.environ.get("COLLECTOR_SOURCE", "")
    secret = os.environ.pop("COLLECTOR_SECRET", "")
    limit = os.environ.get('KUDAGO_LIMIT', '300')
    if not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 600:
        print('KUDAGO_LIMIT must be 1..600.')
        return 1
    if source not in {"kudago", "yandex"} or not re.fullmatch(r"[a-f0-9]{64}", secret):
        print("Collector configuration missing or invalid. Set COLLECTOR_SECRET in Actions secrets.")
        return 1
    run_key = os.environ.get('GITHUB_RUN_ID','') + '.' + os.environ.get('GITHUB_RUN_ATTEMPT','')
    trigger = os.environ.get('GITHUB_EVENT_NAME','')
    if not re.fullmatch(r'[1-9][0-9]{0,19}\.[1-9][0-9]{0,3}',run_key) or trigger not in {'schedule','workflow_dispatch'}:
        print('Expected a GitHub Actions schedule or manual run.')
        return 1
    # TemporaryDirectory is outside the checkout and is removed even on failures.
    with tempfile.TemporaryDirectory(prefix="event-collector-") as directory:
        key_file = Path(directory) / "ingest.key"
        with key_file.open("x", encoding="ascii") as stream:
            os.chmod(key_file, 0o600)
            stream.write(secret)
        secret = None
        try:
            report_run(key_file,run_key,source,trigger,'running')
            extra = ['--limit', limit] if source == 'kudago' else []
            result = subprocess.run([sys.executable, "-m", f"event_collector.{source}_collect", *extra,
                                     "--channel", "chromium", "--publish", "--key-file", str(key_file)],
                                    timeout=960, check=False, capture_output=True,text=True,
                                    env={**os.environ,'COLLECTOR_RUN_KEY':run_key})
            print(result.stdout,flush=True)
            summary = {}
            for line in result.stdout.splitlines():
                try: value=json.loads(line)
                except ValueError: continue
                if isinstance(value,dict) and 'published' in value: summary=value
            receipt=summary.get('published',{})
            if result.returncode or summary.get('status')!='ok' or receipt.get('status')!='ok':
                report_run(key_file,run_key,source,trigger,'failed')
                print('Collector failed or complete publication receipt is missing. Existing catalogue preserved.')
                return 1
            metrics={k:receipt.get(k,0) for k in ('batches','imported','sessions','periods','added')}
            metrics.update({k:summary.get(k,summary.get('discovery',{}).get(k,0)) for k in ('discovered','fetched')})
            report_run(key_file,run_key,source,trigger,'success',metrics)
            print('Complete run and all published batches acknowledged.')
            return 0
        except (subprocess.TimeoutExpired,RuntimeError,OSError):
            try: report_run(key_file,run_key,source,trigger,'failed')
            except RuntimeError: pass
            print("Collector or run reporting failed. Check the watchdog; existing catalogue is preserved.")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
