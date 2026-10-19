import hashlib
import hmac
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from event_collector.protocol import signature
from event_collector.source_http import SyncError
from event_collector.yandex_collect import publish
import run_collector


class CollectorContracts(unittest.TestCase):
    def test_all_modules_import_without_server(self):
        for name in ('models','source_http','kudago_html','kudago_collect','yandex_afisha','yandex_collect'):
            importlib.import_module('event_collector.' + name)

    def test_signature_matches_server_wire_protocol(self):
        expected = hmac.new(b'a' * 64, b'hangingout-collector-v1\n1792411510\n{}', hashlib.sha256).hexdigest()
        self.assertEqual(signature('a' * 64, '1792411510', b'{}'), expected)

    def test_fixed_destination_receipt_and_no_secret_in_request(self):
        for source in ('kudago','yandex'):
            calls=[]
            def transport(request):
                calls.append(request)
                return httpx.Response(200,json={'status':'ok','source':source,'imported':1})
            with tempfile.TemporaryDirectory() as directory:
                key=Path(directory)/'key';key.write_text('a'*64,encoding='ascii')
                with httpx.Client(transport=httpx.MockTransport(transport)) as client:
                    self.assertEqual(publish([{'test':'event'}],key,source=source,client=client)['status'],'ok')
            self.assertEqual(str(calls[0].url),'https://hangingout.ru/api/internal/collector/'+source)
            self.assertNotIn('a'*64,str(calls[0].headers))
            self.assertNotIn('a'*64,calls[0].content.decode())

    def test_publish_does_not_follow_redirects(self):
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(302,headers={'Location':'https://example.invalid/'})
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/'key';key.write_text('a'*64,encoding='ascii')
            with httpx.Client(transport=httpx.MockTransport(transport),follow_redirects=True) as client:
                with self.assertRaises(SyncError):
                    publish([{}],key,source='kudago',client=client)
        self.assertEqual(len(calls),1)

    def test_runner_uses_private_temporary_key_and_cleans_it(self):
        paths=[]
        def child(arguments, **kwargs):
            import os
            from types import SimpleNamespace
            self.assertNotIn('COLLECTOR_SECRET',os.environ)
            self.assertNotIn('a'*64,' '.join(arguments))
            key=Path(arguments[-1]);paths.append(key)
            self.assertEqual(key.read_text(),'a'*64)
            if os.name!='nt':
                self.assertEqual(key.stat().st_mode & 0o777,0o600)
            self.assertEqual(kwargs['env']['COLLECTOR_RUN_KEY'],'12345.1')
            return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'ok','imported':1,'published':{'status':'ok','batches':1,'imported':1,'sessions':2,'periods':0,'added':1}}))
        with patch.dict('os.environ',{'COLLECTOR_SOURCE':'kudago','COLLECTOR_SECRET':'a'*64,'GITHUB_RUN_ID':'12345','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'schedule'}), patch('subprocess.run',side_effect=child), patch('run_collector.report_run') as report:
            self.assertEqual(run_collector.main(),0)
            self.assertEqual([call.args[4] for call in report.call_args_list],['running','success'])
        self.assertFalse(paths[0].exists())

    def test_failed_or_empty_run_does_not_report_success(self):
        from types import SimpleNamespace
        for result in (SimpleNamespace(returncode=1,stdout='',stderr='Collector stopped: yandex_captcha_required. Check receipts.'),SimpleNamespace(returncode=0,stdout='{}',stderr='')):
            with patch.dict('os.environ',{'COLLECTOR_SOURCE':'kudago','COLLECTOR_SECRET':'a'*64,'GITHUB_RUN_ID':'12345','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'schedule'}), patch('subprocess.run',return_value=result), patch('run_collector.report_run') as report, patch('builtins.print'):
                self.assertEqual(run_collector.main(),1)
                self.assertEqual([call.args[4] for call in report.call_args_list],['running','failed'])

    def test_missing_key_never_launches_browser(self):
        with patch.dict('os.environ',{'COLLECTOR_SOURCE':'kudago','COLLECTOR_SECRET':''}), patch('subprocess.run') as child, patch('builtins.print'):
            self.assertEqual(run_collector.main(),1)
            child.assert_not_called()


if __name__ == '__main__':
    unittest.main()
