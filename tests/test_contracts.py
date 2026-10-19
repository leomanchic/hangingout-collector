import hashlib
import hmac
import importlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from event_collector.protocol import signature
from event_collector.source_http import SyncError
from event_collector.yandex_collect import publish


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


if __name__ == '__main__':
    unittest.main()
