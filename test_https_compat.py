import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from https_compat import Gateway, UPSTREAM_PREFIX

TOKEN = 'Bearer synthetic-fixture-token'
FIRST = b'data: first\n\n'
LAST = b'data: last\n\n'


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(('GET', self.path))
        self.send_response(304)
        self.send_header('ETag', 'synthetic-etag')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def do_POST(self):
        self.server.requests.append(('POST', self.path))
        body = b''
        if self.headers.get('Transfer-Encoding') == 'chunked':
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                self.rfile.read(2)
        else:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.body = body
        self.server.auth_preserved = self.headers.get('Authorization') == TOKEN
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        drop = 'drop=1' in self.path
        self.send_header('Content-Length', '100' if drop else str(len(FIRST + LAST)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(FIRST)
        self.wfile.flush()
        if not drop:
            self.server.release.wait(2)
            self.wfile.write(LAST)
            self.wfile.flush()
        self.server.finished.set()
        self.close_connection = True


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.requests = []
        self.upstream.body = None
        self.upstream.release = threading.Event()
        self.upstream.finished = threading.Event()
        self.gateway = Gateway(connection_factory=lambda: http.client.HTTPConnection(
            '127.0.0.1', self.upstream.server_port, timeout=2))
        for s in [self.upstream, self.gateway]:
            threading.Thread(target=s.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    def tearDown(self):
        self.upstream.release.set()
        for s in [self.gateway, self.upstream]:
            s.shutdown()
            s.server_close()

    def client(self):
        c = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        self.addCleanup(c.close)
        return c

    def test_websocket_fallback_does_not_contact_upstream(self):
        c = self.client()
        c.request('GET', '/responses', headers={'Upgrade': 'websocket'})
        r = c.getresponse()
        self.assertEqual(r.status, 426)
        r.read()
        self.assertEqual(self.upstream.requests, [])

    def test_requires_authorization_and_fixed_endpoints(self):
        for path, expected in [('/models', 401), ('/oauth/token', 404)]:
            c = self.client()
            c.request('GET', path)
            r = c.getresponse()
            self.assertEqual(r.status, expected)
            r.read()
        self.assertEqual(self.upstream.requests, [])

    def test_preserves_body_auth_and_streams_before_upstream_finishes(self):
        c = self.client()
        payload = b'opaque-compressed-fixture' * 25000
        c.request('POST', '/responses', body=payload,
                  headers={'Authorization': TOKEN, 'Content-Encoding': 'zstd'})
        r = c.getresponse()
        self.assertEqual(r.read1(len(FIRST)), FIRST)
        self.assertFalse(self.upstream.finished.is_set())
        self.assertEqual(self.upstream.body, payload)
        self.assertTrue(self.upstream.auth_preserved)
        self.upstream.release.set()
        self.assertEqual(r.read(), LAST)
        self.assertEqual(self.upstream.requests, [('POST', UPSTREAM_PREFIX + '/responses')])

    def test_chunked_request_is_forwarded_without_body_changes(self):
        self.upstream.release.set()
        c = self.client()
        c.request('POST', '/responses', body=iter([b'alpha', b'beta', b'gamma']),
                  headers={'Authorization': TOKEN}, encode_chunked=True)
        self.assertEqual(c.getresponse().read(), FIRST + LAST)
        self.assertEqual(self.upstream.body, b'alphabetagamma')

    def test_model_cache_304_has_no_response_body(self):
        c = self.client()
        c.request('GET', '/models?client_version=fixture', headers={'Authorization': TOKEN})
        r = c.getresponse()
        self.assertEqual(r.status, 304)
        self.assertEqual(r.read(), b'')
        self.assertEqual(r.getheader('ETag'), 'synthetic-etag')

    def test_remote_compaction_endpoint_is_preserved(self):
        self.upstream.release.set()
        c = self.client()
        c.request('POST', '/responses/compact', body=b'compact-fixture',
                  headers={'Authorization': TOKEN})
        self.assertEqual(c.getresponse().read(), FIRST + LAST)
        self.assertEqual(self.upstream.requests, [('POST', UPSTREAM_PREFIX + '/responses/compact')])

    def test_upstream_truncation_is_not_completed_or_replayed(self):
        c = self.client()
        c.request('POST', '/responses?drop=1', body=b'test', headers={'Authorization': TOKEN})
        r = c.getresponse()
        with self.assertRaises(http.client.IncompleteRead):
            r.read()
        self.assertEqual(len(self.upstream.requests), 1)


if __name__ == '__main__':
    unittest.main()
