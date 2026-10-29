"""Loopback-only Responses/SSE compatibility gateway.

No credentials or request/response bodies are cached or logged.
Production upstream is fixed to the official ChatGPT Codex backend with verified TLS.
"""
import argparse
from collections import Counter
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import threading
from urllib.parse import urlsplit

UPSTREAM_HOST = 'chatgpt.com'
UPSTREAM_PREFIX = '/backend-api/codex'
MAX_BODY = 256 * 1024 * 1024
HOP_HEADERS = {'host', 'connection', 'proxy-connection', 'proxy-authorization',
               'keep-alive', 'upgrade', 'transfer-encoding', 'trailer', 'te', 'expect'}


def official_connection():
    # Load the shared proxy reader explicitly: this script also runs with -I.
    spec = importlib.util.spec_from_file_location('cloudlink_relay', Path(__file__).with_name('relay.py'))
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    connection = http.client.HTTPSConnection(*relay.system_proxy(), timeout=12,
                                            context=ssl.create_default_context())
    connection.set_tunnel(UPSTREAM_HOST, 443)
    return connection


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, connection_factory=None):
        self.connection_factory = connection_factory or official_connection
        self.metrics = Counter()
        self.metrics_lock = threading.Lock()
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        super().__init__(('127.0.0.1', port), Handler)

    def bump(self, name, value=1):
        with self.metrics_lock:
            self.metrics[name] += value

    def handle_error(self, request, client_address):
        # The default traceback could contain an invalid incoming header value.
        self.bump('handler_errors')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CloudLink'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(300)

    def log_message(self, *args):
        pass

    def local_response(self, status, message):
        body = json.dumps({'error': message}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        self.close_connection = True
        target = urlsplit(self.path)
        if self.headers.get('Host') not in [f'127.0.0.1:{self.server.server_port}',
                                            f'localhost:{self.server.server_port}']:
            self.local_response(400, 'Invalid local host')
            return
        if self.command == 'GET' and target.path == '/healthz':
            with self.server.metrics_lock:
                body = json.dumps({'running': True, 'service': 'cloudlink-model-https',
                                   'source_sha256': self.server.source_sha256,
                                   'metrics': dict(self.server.metrics)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.command == 'GET' and target.path == '/responses' and self.headers.get('Upgrade', '').lower() == 'websocket':
            self.server.bump('websocket_fallbacks')
            self.local_response(426, 'Use HTTPS/SSE transport')
            return
        if (self.command, target.path) not in [('GET', '/models'), ('POST', '/responses'),
                                               ('POST', '/responses/compact')]:
            self.local_response(404, 'Unsupported endpoint')
            return
        if not self.headers.get('Authorization', '').startswith('Bearer '):
            self.local_response(401, 'Authorization required')
            return
        chunked = self.headers.get('Transfer-Encoding', '').lower() == 'chunked'
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.local_response(400, 'Invalid body length')
            return
        if length < 0 or length > MAX_BODY:
            self.local_response(413, 'Request body too large')
            return
        connection = None
        headers_sent = False
        try:
            # Retrying is safe only before any request headers/body are forwarded.
            for attempt in range(2):
                connection = self.server.connection_factory()
                try:
                    connection.connect()
                    break
                except OSError:
                    connection.close()
                    self.server.bump('upstream_connect_errors')
                    if attempt == 1:
                        raise
            connection.sock.settimeout(300)
            suffix = target.path + ('?' + target.query if target.query else '')
            connection.putrequest(self.command, UPSTREAM_PREFIX + suffix,
                                  skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', UPSTREAM_HOST)
            for key, value in self.headers.items():
                if key.lower() not in HOP_HEADERS and key.lower() != 'content-length':
                    connection.putheader(key, value)
            connection.putheader('Connection', 'close')
            if chunked:
                connection.putheader('Transfer-Encoding', 'chunked')
            elif self.command == 'POST' or length:
                connection.putheader('Content-Length', str(length))
            connection.endheaders()
            sent = 0
            while chunked or sent < length:
                if chunked:
                    line = self.rfile.readline(8192)
                    if not line.endswith(b'\n'):
                        raise ValueError('invalid chunk')
                    size = int(line.split(b';')[0].strip(), 16)
                    if size == 0:
                        while self.rfile.readline(8192) not in [b'\r\n', b'\n', b'']:
                            pass
                        connection.send(b'0\r\n\r\n')
                        break
                else:
                    size = length - sent
                if size < 0 or sent + size > MAX_BODY:
                    raise ValueError('body size limit')
                if chunked:
                    connection.send((hex(size)[2:] + '\r\n').encode())
                remaining = size
                while remaining:
                    data = self.rfile.read(min(65536, remaining))
                    if not data:
                        raise ConnectionError('client body ended')
                    connection.send(data)
                    remaining -= len(data)
                    sent += len(data)
                if chunked:
                    if self.rfile.read(2) != b'\r\n':
                        raise ValueError('invalid chunk ending')
                    connection.send(b'\r\n')
            self.server.bump('request_bytes', sent)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and key.lower() != 'content-length':
                    self.send_header(key, value)
            no_body = response.status in [204, 304]
            if not no_body:
                self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close')
            self.end_headers()
            headers_sent = True
            self.server.bump('http_responses')
            if no_body:
                return
            while data := response.read1(65536):
                self.wfile.write((hex(len(data))[2:] + '\r\n').encode() + data + b'\r\n')
                self.wfile.flush()
                self.server.bump('response_bytes', len(data))
            if response.length not in [None, 0]:
                raise http.client.IncompleteRead(b'', response.length)
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.server.bump('disconnected_streams')
        except Exception:
            self.server.bump('forward_errors')
            if not headers_sent:
                try:
                    self.local_response(502, 'Official upstream connection failed')
                except OSError:
                    pass
            # After streaming begins, close without a fake completion event.
        finally:
            if connection is not None:
                connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = Gateway(args.port)
    print(json.dumps({'running': True, 'listen': '127.0.0.1', 'port': server.server_port}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
